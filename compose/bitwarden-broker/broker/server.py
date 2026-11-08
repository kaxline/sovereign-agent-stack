"""The broker's HTTP API: TCP on loopback for the agent side, a Unix socket for values.

The broker shares the ``hermes`` container's network namespace, so agent code
can reach the TCP port. No TCP route returns a password, TOTP code, seed or
session key: ``/fill`` writes the value into the page itself and returns only
digests of it (``digest_redaction.build_entry``). The Unix socket at
``BITWARDEN_SYNC_SOCKET`` (mode 600, never bound on TCP) carries the three
calls that handle values: ``GET /secrets`` for ``BW_SECRETS_ITEM_SCOPE`` items,
and ``POST /unlock`` and ``POST /lock`` for ``manual`` accounts
(``python3 -m broker.unlock``).

Request bodies are JSON objects of at most 64 KiB, with no duplicate keys and
no keys a route does not take. Every error body is ``{"error": "<code>"}`` with
a fixed code. The access log has one line per request: method, route, status
and the handle when the body names one. Bodies, item names, sites and bw's
output are never logged.

A sync thread runs ``bw sync`` per unlocked account every
``BITWARDEN_SYNC_MINUTES``, unlocks ``stored`` accounts again after a lock or a
failed start, and, when ``BITWARDEN_IDLE_LOCK_MINUTES`` is above 0, locks a
``manual`` account that has gone that long since its last fill or unlock.
After each sync, pending access requests for a host now in scope are marked
done, and denylist entries for items no longer in scope are dropped, so a later
grant of the same item lists it again.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import cdp, digest_redaction
from .accounts import load_dir
from .bwcli import Bw, BwError
from .requests_store import RequestError, Store
from .vault import HANDLE_PREFIX, Denylist, Vault, parse_handle

ACCESS_LOG = logging.getLogger("broker.access")
LOG = logging.getLogger("broker.server")

MAX_BODY = 64 * 1024
MAX_DRAIN = 1 << 20
FILL_TIMEOUT = 10.0
DEFAULT_LISTEN = "127.0.0.1:8099"
DEFAULT_SOCKET = "/run/bitwarden-sync/sync.sock"
# Fill errors for which no setter ran in the page, so no redaction entry is returned.
# Every other outcome returns one, write_unconfirmed and a timeout included. An entry
# carries its HMAC key, so whoever reads the response can brute-force a short value
# such as a TOTP code from it; the spec accepts that (Redaction). Leaving it out here
# keeps a bad target id from handing out entries for any in-scope item.
NOTHING_WRITTEN = (cdp.TARGET_NOT_FOUND, cdp.ORIGIN_MISMATCH, cdp.NO_FIELD)
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
_STATUS_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 408: "timeout",
                 413: "too_large", 414: "too_large", 431: "too_large", 501: "method_not_allowed"}


class ConfigError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Config:
    accounts_dir: Path
    data_dir: Path
    secrets_dir: Path
    sync_seconds: float
    idle_seconds: float
    listen: tuple[str, int]
    socket_path: Path
    bw_path: str

    @classmethod
    def from_env(cls, env) -> Config:
        dirs = {}
        for name in ("BITWARDEN_ACCOUNTS_DIR", "BITWARDEN_DATA_DIR", "BITWARDEN_SECRETS_DIR"):
            if not env.get(name):
                raise ConfigError(f"{name} is required")
            dirs[name] = Path(env[name])
        return cls(
            accounts_dir=dirs["BITWARDEN_ACCOUNTS_DIR"],
            data_dir=dirs["BITWARDEN_DATA_DIR"],
            secrets_dir=dirs["BITWARDEN_SECRETS_DIR"],
            sync_seconds=_minutes(env, "BITWARDEN_SYNC_MINUTES", "10", allow_zero=False),
            idle_seconds=_minutes(env, "BITWARDEN_IDLE_LOCK_MINUTES", "0", allow_zero=True),
            listen=_listen(env.get("BITWARDEN_LISTEN") or DEFAULT_LISTEN),
            socket_path=Path(env.get("BITWARDEN_SYNC_SOCKET") or DEFAULT_SOCKET),
            bw_path=env.get("BITWARDEN_BW") or "bw",
        )


def _minutes(env, name: str, default: str, allow_zero: bool) -> float:
    raw = (env.get(name) or "").strip() or default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} must be a number of minutes{'' if allow_zero else ' above 0'}")
    return value * 60


def _listen(raw: str) -> tuple[str, int]:
    host, sep, port = raw.strip().rpartition(":")
    host = host.strip("[]")
    try:
        loopback = bool(sep) and ipaddress.ip_address(host).is_loopback
        number = int(port)
    except ValueError:
        loopback, number = False, -1
    if not loopback or not 0 <= number <= 65535:
        raise ConfigError("BITWARDEN_LISTEN must be a loopback address and port, such as 127.0.0.1:8099")
    return host, number


def _fields(body, required=(), optional=()) -> dict:
    if not isinstance(body, dict):
        raise HttpError(400, "bad_request")
    keys = set(body)
    if not set(required) <= keys or keys - set(required) - set(optional):
        raise HttpError(400, "bad_request")
    return body


def _parse_json(raw: bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def constant(_name):
        raise ValueError("not JSON")

    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise HttpError(400, "bad_request")
    return body


class Broker:
    def __init__(self, config: Config):
        self.config = config
        accounts, self.account_errors = load_dir(config.accounts_dir)
        self.accounts = {a.slug: a for a in accounts}
        self.bws = {a.slug: Bw(a, config.data_dir / "bw", config.secrets_dir, bw_path=config.bw_path)
                    for a in accounts}
        store_dir = config.data_dir / "broker"
        # A store that does not load stops the broker: starting empty would drop requests or
        # serve released items again.
        try:
            self.denylist = Denylist(store_dir / "denylist.json")
        except (OSError, ValueError):
            raise ConfigError("denylist.json cannot be loaded; fix or move it") from None
        try:
            self.requests = Store(store_dir / "requests.json", accounts=self.accounts)
        except (OSError, ValueError):
            raise ConfigError("requests.json cannot be loaded; fix or move it") from None
        self.vault = Vault(self.bws, self.denylist)
        self._guard = threading.Lock()
        self._activity: dict[str, float] = {}
        self._last_sync: dict[str, float] = {}
        self._unlocking = {slug: threading.Lock() for slug in self.bws}

    # --- TCP routes ---------------------------------------------------------

    def get_accounts(self, _body) -> list:
        return [{"slug": slug, "mode": a.mode, "scope": a.scope_kind, "state": self.bws[slug].state,
                 "error_class": self.bws[slug].error_class} for slug, a in sorted(self.accounts.items())]

    def get_items(self, _body) -> list:
        return [{"handle": m.handle, "label": m.label, "username": m.username, "origins": list(m.origins),
                 "has_totp": m.has_totp} for m in self.vault.items()]

    def get_health(self, _body) -> dict:
        return {"status": "ok", "accounts": [{"slug": s, "state": self.bws[s].state} for s in sorted(self.bws)],
                "account_errors": list(self.account_errors)}

    def post_fill(self, body) -> dict:
        _fields(body, required=("handle", "target_id", "port"), optional=("field", "base"))
        field = body.get("field", "password")
        if field not in cdp.FIELDS:
            raise HttpError(400, "invalid_field")
        base = body.get("base")
        if not digest_redaction._valid_base(base):
            raise HttpError(400, "invalid_base")
        port, target_id, handle = body["port"], body["target_id"], body["handle"]
        if (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
                or not isinstance(target_id, str) or not isinstance(handle, str)):
            raise HttpError(400, "bad_request")
        meta = self.vault.get(handle)
        if meta is None:
            raise HttpError(404, "unknown_handle")
        if field == "totp" and not meta.has_totp:
            raise HttpError(400, "no_totp")
        bw = self.bws[meta.account]
        value = failure = None
        try:
            try:
                value = bw.get_totp(meta.item_id) if field == "totp" else bw.get_password(meta.item_id)
            except BwError as exc:
                failure = exc.error_class
            if failure is not None:
                if field == "totp" and failure == "unknown":
                    raise HttpError(400, "no_totp")
                raise HttpError(503, "account_locked" if failure in ("locked", "auth") else "unavailable")
            if not value:
                raise HttpError(400, "no_value")
            # A release can land while bw ran; it wins.
            if self.vault.get(handle) is None:
                raise HttpError(404, "unknown_handle")
            result = cdp.fill(port, target_id, meta.origins, value, timeout=FILL_TIMEOUT, field=field)
            self._touch(meta.account)
            redaction = None
            if result.filled or result.error not in NOTHING_WRITTEN:
                redaction = digest_redaction.build_entry(value, base)
            return {"filled": result.filled, "origin": result.origin, "error": result.error,
                    "redaction": redaction}
        finally:
            value = None

    def post_sync(self, body) -> dict:
        _fields(body)
        synced = [slug for slug in sorted(self.bws) if self.sync_account(slug)]
        self._mark_done()
        return {"synced": synced}

    def post_requests(self, body) -> dict:
        _fields(body, required=("site",), optional=("account", "reason"))
        account = body.get("account")
        if account is None and len(self.accounts) == 1:
            account = next(iter(self.accounts))
        self.requests.expire()
        try:
            request_id = self.requests.add(account, body["site"], body.get("reason"))
        except RequestError as exc:
            raise HttpError(429 if exc.code == "too_many" else 400, exc.code) from None
        return {"request_id": request_id}

    def get_requests(self, _body) -> dict:
        self.requests.expire()
        return {"pending": self.requests.pending(), "recent": self.requests.recent()}

    def post_release(self, body) -> dict:
        _fields(body, required=("handle",))
        handle = body["handle"]
        parsed = parse_handle(handle)
        if parsed is None or parsed[0] not in self.bws:
            raise HttpError(404, "unknown_handle")
        if self.vault.get(handle) is None and handle not in self.denylist:
            raise HttpError(404, "unknown_handle")
        # Denylisted before the move starts, so /items and /fill refuse it while bw runs.
        self.denylist.add(handle)
        returned = self.bws[parsed[0]].return_item(parsed[1])
        if returned:
            self._prune(parsed[0])
        return {"released": True, "returned": returned}

    # --- Unix socket routes -------------------------------------------------

    def get_secrets(self, _body) -> list:
        out = []
        for slug in sorted(self.bws):
            bw = self.bws[slug]
            if self.accounts[slug].secrets_item_scope is None or bw.state != "unlocked":
                continue
            try:
                out.extend({"name": name, "value": value} for name, value in bw.secret_items())
            except BwError:
                continue
        return out

    def _manual(self, slug):
        account = self.accounts.get(slug) if isinstance(slug, str) else None
        if account is None or account.unlock != "manual":
            raise HttpError(404, "unknown_account")
        return self.bws[slug]

    def post_unlock(self, body) -> dict:
        _fields(body, required=("slug", "password"))
        password = body.pop("password")
        try:
            if not isinstance(password, str) or not password:
                raise HttpError(400, "bad_request")
            bw = self._manual(body["slug"])
            stage = error_class = None
            try:
                bw.login()
            except BwError as exc:
                stage, error_class = "login", exc.error_class
            if stage is None:
                try:
                    bw.unlock(password)
                except BwError as exc:
                    stage, error_class = "unlock", exc.error_class
        finally:
            password = None
        if stage == "unlock" and error_class == "auth":
            bw.lock()  # a mistyped password leaves the account locked, not in error
            raise HttpError(401, "wrong_password")
        # unlock() can raise after the session is set, when the first listing fails;
        # the account is then unlocked with an empty or old list.
        if bw.state != "unlocked":
            raise HttpError(503, "login_failed" if stage == "login" else "unavailable")
        self._touch(bw.account.slug)
        self._prune(bw.account.slug)
        self._mark_done()
        return {"unlocked": True}

    def post_lock(self, body) -> dict:
        _fields(body, required=("slug",))
        self._manual(body["slug"]).lock()
        return {"locked": True}

    # --- Sync, unlock and idle lock -----------------------------------------

    def _touch(self, slug: str) -> None:
        with self._guard:
            self._activity[slug] = time.monotonic()

    def unlock_stored(self, slug: str) -> bool:
        """Unlock a ``stored`` account with its stored master password; True if it is unlocked."""
        bw = self.bws[slug]
        guard = self._unlocking[slug]
        if not guard.acquire(blocking=False):
            return bw.state == "unlocked"  # another thread is unlocking it now
        try:
            if bw.state != "unlocked":
                try:
                    bw.unlock()
                except BwError:
                    pass
            if bw.state != "unlocked":
                LOG.info("%s: unlock failed (%s)", slug, bw.error_class or "unknown")
                return False
            self._touch(slug)
            self._prune(slug)
            return True
        finally:
            guard.release()

    def sync_account(self, slug: str) -> bool:
        bw, account = self.bws[slug], self.accounts[slug]
        with self._guard:
            self._last_sync[slug] = time.monotonic()
        if bw.state != "unlocked":
            return account.unlock == "stored" and self.unlock_stored(slug)
        try:
            bw.sync()
            ok = True
        except BwError:
            ok = False
            LOG.info("%s: sync failed (%s)", slug, bw.error_class or "unknown")
        if bw.state != "unlocked" and account.unlock == "stored":
            return self.unlock_stored(slug)  # the session expired
        if ok:
            self._prune(slug)
        return ok

    def _prune(self, slug: str) -> None:
        """Drop denylist entries for this account's items that the last listing no longer has."""
        present = self.bws[slug].cached_ids()
        if present is None:
            return
        prefix = f"{HANDLE_PREFIX}{slug}/"
        for handle in self.denylist.handles():
            if handle.startswith(prefix) and handle[len(prefix):] not in present:
                self.denylist.remove(handle)

    def _mark_done(self) -> None:
        hosts = {urlsplit(origin).hostname for meta in self.vault.items() for origin in meta.origins}
        for host in sorted(h for h in hosts if h):
            self.requests.mark_done(host)

    def run_loop(self, stop: threading.Event) -> None:
        sync_s, idle_s = self.config.sync_seconds, self.config.idle_seconds
        tick = max(0.1, min(60.0, sync_s, idle_s if idle_s > 0 else 60.0))
        start = time.monotonic()
        while not stop.wait(tick):
            for slug in sorted(self.bws):
                if stop.is_set():
                    return
                try:
                    self._loop_account(slug, start, sync_s, idle_s)
                except Exception as exc:  # noqa: BLE001 - one account's failure must not stop the loop
                    LOG.warning("%s: sync loop error (%s)", slug, type(exc).__name__)
            try:
                self._mark_done()
                self.requests.expire()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("request upkeep error (%s)", type(exc).__name__)

    def _loop_account(self, slug: str, start: float, sync_s: float, idle_s: float) -> None:
        bw, account = self.bws[slug], self.accounts[slug]
        now = time.monotonic()
        with self._guard:
            last_used = self._activity.get(slug, now)
            last_sync = self._last_sync.get(slug, start)
        if account.unlock == "manual" and idle_s > 0 and bw.state == "unlocked" and now - last_used >= idle_s:
            bw.lock()
            LOG.info("%s: locked after %g idle minutes", slug, idle_s / 60)
        elif now - last_sync >= sync_s:
            self.sync_account(slug)

    # --- Serving ------------------------------------------------------------

    def start(self) -> Running:
        tcp_host, tcp_port = self.config.listen
        tcp = (_TcpServer6 if ":" in tcp_host else _TcpServer)((tcp_host, tcp_port), _TcpHandler)
        tcp.broker = self
        try:
            unix = _unix_server(self.config.socket_path)
        except BaseException:
            tcp.server_close()
            raise
        unix.broker = self
        stop = threading.Event()
        threads = [threading.Thread(target=tcp.serve_forever, name="tcp", daemon=True),
                   threading.Thread(target=unix.serve_forever, name="unix", daemon=True),
                   threading.Thread(target=self.run_loop, args=(stop,), name="sync", daemon=True)]
        threads += [threading.Thread(target=self.unlock_stored, args=(slug,), name=f"unlock-{slug}", daemon=True)
                    for slug, a in sorted(self.accounts.items()) if a.unlock == "stored"]
        for t in threads:
            t.start()
        LOG.info("listening on %s:%d with %d accounts", tcp_host, tcp.server_address[1], len(self.accounts))
        return Running(tcp, unix, self.config.socket_path, stop)


class Running:
    def __init__(self, tcp, unix, socket_path: Path, stop: threading.Event):
        self.tcp, self.unix, self.socket_path, self.stop_event = tcp, unix, socket_path, stop

    @property
    def port(self) -> int:
        return self.tcp.server_address[1]

    def stop(self) -> None:
        self.stop_event.set()
        for server in (self.tcp, self.unix):
            server.shutdown()
            server.server_close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bitwarden-broker"
    sys_version = ""
    timeout = 30  # seconds a client may take to send its request
    routes: dict = {}

    def _dispatch(self) -> None:
        self._route = self._handle = None
        try:
            path = urlsplit(self.path).path
        except ValueError:
            path = ""
        route = self.routes.get(path)
        if route is not None:
            self._route = path
        try:
            if route is None:
                raise HttpError(404, "not_found")
            name = route.get(self.command)
            if name is None:
                raise HttpError(405, "method_not_allowed")
            body = self._body() if self.command == "POST" else None
            if isinstance(body, dict) and isinstance(body.get("handle"), str):
                self._handle = body["handle"]
            status, payload = 200, getattr(self.server.broker, name)(body)
        except HttpError as exc:
            status, payload = exc.status, {"error": exc.code}
        except Exception as exc:  # noqa: BLE001 - the text could hold anything; only the type is logged
            LOG.warning("%s %s failed (%s)", self.command, self._route, type(exc).__name__)
            status, payload = 500, {"error": "internal"}
        self._send(status, payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(400, "bad_request")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise HttpError(400, "bad_request") from None
        if length < 0:
            raise HttpError(400, "bad_request")
        if length > MAX_BODY:
            if length <= MAX_DRAIN:
                self.rfile.read(length)  # read it so the close does not reset the connection
            raise HttpError(413, "too_large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise HttpError(400, "bad_request")
        return _parse_json(raw) if raw else {}

    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError:
            pass
        self._log(status)

    def send_error(self, code, message=None, explain=None) -> None:
        """Replace the stdlib's HTML error page, which repeats the request line."""
        self._route = getattr(self, "_route", None)
        self._handle = None
        self._send(code, {"error": _STATUS_CODES.get(code, "bad_request")})

    def _log(self, status: int) -> None:
        method = self.command if self.command in METHODS else "-"
        handle = self._handle
        shown = handle if handle and parse_handle(handle) and len(handle) <= 200 and handle.isprintable() \
            and " " not in handle else None
        ACCESS_LOG.info("%s %s %d%s", method, self._route or "-", status, f" {shown}" if shown else "")

    def log_message(self, format, *args) -> None:  # noqa: A002 - the stdlib's signature
        pass


class _TcpHandler(_Handler):
    routes = {
        "/accounts": {"GET": "get_accounts"},
        "/items": {"GET": "get_items"},
        "/fill": {"POST": "post_fill"},
        "/sync": {"POST": "post_sync"},
        "/requests": {"GET": "get_requests", "POST": "post_requests"},
        "/release": {"POST": "post_release"},
        "/health": {"GET": "get_health"},
    }


class _UnixHandler(_Handler):
    routes = {
        "/secrets": {"GET": "get_secrets"},
        "/unlock": {"POST": "post_unlock"},
        "/lock": {"POST": "post_lock"},
        "/health": {"GET": "get_health"},
    }


class _TcpServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        LOG.warning("tcp connection error")


class _TcpServer6(_TcpServer):
    address_family = socket.AF_INET6


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        LOG.warning("socket connection error")


def _unix_server(path: Path) -> _UnixServer:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise ConfigError(f"{path} exists and is not a socket")
        os.unlink(path)  # left by a broker that did not stop cleanly
    old = os.umask(0o177)  # the socket is created mode 600
    try:
        server = _UnixServer(str(path), _UnixHandler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        broker = Broker(Config.from_env(os.environ))
        for error in broker.account_errors:
            LOG.warning("account file refused: %s", error)
        running = broker.start()
    except ConfigError as exc:
        LOG.error("not starting: %s", exc)
        return 2
    except OSError as exc:
        LOG.error("not starting: cannot listen (%s)", type(exc).__name__)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.wait(1):
        pass
    running.stop()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
