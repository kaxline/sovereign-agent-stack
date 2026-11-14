"""The signer's HTTP API on a Unix socket, one sign-in session at a time.

The socket (``BITWARDEN_SIGNIN_SOCKET``, mode 600) is the only listener; the
broker is its one client. Request bodies are JSON objects of at most 64 KiB
with exactly the keys the route takes. Every error is ``{"error": "<code>"}``
with a fixed code. The log has one line per request: method, route, status and
code. Bodies, URLs, refs' targets and values are never logged.

Routes: ``GET /health``, and ``POST /session/{open,navigate,snapshot,click,
type,fill,export,end}``. ``open`` starts Chromium and refuses with ``busy``
while a session is open; ``navigate`` moves the session Chromium already has to
another allowed URL; the other session routes get ``no_session`` without one;
``end`` returns ``{"ended": false}`` then. An action that runs past
``BITWARDEN_SIGNIN_ACTION_SECONDS`` gets ``timeout`` and ends the session, and
a session with no action for ``BITWARDEN_SIGNIN_IDLE_MINUTES`` is ended by the
reaper thread. Ending a session kills its Chromium and deletes its profile.

At startup ``listen`` starts and stops Chromium once. If that fails (on a host
where the sandbox cannot start), ``/health`` reports ``sandbox_unavailable``
and ``open`` refuses with that code; there is no fallback without the sandbox.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socketserver
import stat
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from . import browser
from .session import Session, SessionError, url_allowed

LOG = logging.getLogger("signer")

DEFAULT_SOCKET = "/run/bitwarden-signin/signin.sock"
DEFAULT_IDLE_MINUTES = 5.0
DEFAULT_ACTION_SECONDS = 30.0
MAX_BODY = 64 * 1024
MAX_ORIGINS = 64
MAX_TEXT = 4096
MAX_URL = 8192
FIELDS = ("password", "totp")

STATUS = {
    "bad_request": 400, "origin_mismatch": 403, "not_found": 404, "method_not_allowed": 405,
    "busy": 409, "no_session": 409, "stale_ref": 409, "too_large": 413, "not_a_secret_field": 422,
    "internal": 500, "page_error": 502, "sandbox_unavailable": 503, "timeout": 504,
}

# Route -> (method, the body's keys and their checks).
_ORIGINS = "origins"
_REF = "ref"
_TEXT = "text"
_URL = "url"
_FIELD = "field"
ROUTES = {
    "/health": ("GET", None),
    "/session/open": ("POST", {"url": _URL, "allowed": _ORIGINS}),
    "/session/navigate": ("POST", {"url": _URL, "allowed": _ORIGINS}),
    "/session/snapshot": ("POST", {"allowed": _ORIGINS}),
    "/session/click": ("POST", {"ref": _REF, "allowed": _ORIGINS}),
    "/session/type": ("POST", {"ref": _REF, "text": _TEXT, "allowed": _ORIGINS}),
    "/session/fill": ("POST", {"ref": _REF, "field": _FIELD, "value": _TEXT, "allowed": _ORIGINS}),
    "/session/export": ("POST", {"allowed": _ORIGINS, "item_origins": _ORIGINS}),
    "/session/end": ("POST", {}),
}


class ConfigError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = STATUS[code]


@dataclass(frozen=True)
class Config:
    socket_path: Path
    idle_minutes: float
    action_seconds: float


def _positive(env, name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def load_config(env) -> Config:
    path = env.get("BITWARDEN_SIGNIN_SOCKET") or DEFAULT_SOCKET
    if not os.path.isabs(path):
        raise ConfigError("BITWARDEN_SIGNIN_SOCKET must be an absolute path")
    return Config(socket_path=Path(path),
                  idle_minutes=_positive(env, "BITWARDEN_SIGNIN_IDLE_MINUTES", DEFAULT_IDLE_MINUTES),
                  action_seconds=_positive(env, "BITWARDEN_SIGNIN_ACTION_SECONDS", DEFAULT_ACTION_SECONDS))


def _parse_json(raw: bytes) -> dict:
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
        raise HttpError("bad_request")
    return body


def _valid(kind: str, value) -> bool:
    if kind == _ORIGINS:
        return isinstance(value, list) and len(value) <= MAX_ORIGINS and all(
            isinstance(o, str) and 0 < len(o) <= MAX_URL for o in value)
    if kind == _REF:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if kind == _TEXT:
        return isinstance(value, str) and len(value) <= MAX_TEXT
    if kind == _URL:
        return isinstance(value, str) and 0 < len(value) <= MAX_URL
    if kind == _FIELD:
        return value in FIELDS
    return False


class Signer:
    def __init__(self, config: Config, start_browser=browser.Chromium.start) -> None:
        self.config = config
        self._start_browser = start_browser
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.session: Session | None = None
        self.sandbox_ok = False
        self._server: _UnixServer | None = None

    # --- lifecycle ---------------------------------------------------------

    def listen(self) -> None:
        """Probe the sandbox, then bind the socket."""
        self.sandbox_ok = self._probe()
        if not self.sandbox_ok:
            LOG.warning("sandbox_unavailable: Chromium did not start with its sandbox; sessions are refused")
        self._server = _unix_server(self.config.socket_path, self)

    def _probe(self) -> bool:
        try:
            chromium = self._start_browser()
        except Exception as exc:  # noqa: BLE001 - any failure to start means no session can run
            LOG.warning("sandbox probe failed (%s)", type(exc).__name__)
            return False
        chromium.stop()
        return True

    def serve(self) -> None:
        reaper = threading.Thread(target=self._reap, name="signer-reaper", daemon=True)
        reaper.start()
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            try:
                os.unlink(self.config.socket_path)
            except FileNotFoundError:
                pass
        with self._lock:
            self._end()

    def _reap(self) -> None:
        idle = self.config.idle_minutes * 60
        while not self._stop.wait(min(1.0, idle / 4)):
            if not self._lock.acquire(blocking=False):
                continue  # an action is running, so the session is not idle
            try:
                if self.session is not None and time.monotonic() - self.session.last_action >= idle:
                    LOG.info("session ended after %g idle minutes", self.config.idle_minutes)
                    self._end()
            finally:
                self._lock.release()

    def _end(self) -> bool:
        session, self.session = self.session, None
        if session is None:
            return False
        session.end()
        return True

    # --- routes ------------------------------------------------------------

    def handle(self, route: str, body: dict | None) -> dict:
        if route == "/health":
            if not self.sandbox_ok:
                return {"status": "sandbox_unavailable"}
            return {"status": "busy" if self.session is not None else "ready"}
        with self._lock:
            if route == "/session/end":
                return {"ended": self._end()}
            if route == "/session/open":
                return self._open(body["url"], body["allowed"])
            if self.session is None:
                raise HttpError("no_session")
            name = route.rsplit("/", 1)[1]
            return self._run(lambda s: getattr(s, name)(**body))

    def _open(self, url: str, allowed: list[str]) -> dict:
        if not self.sandbox_ok:
            raise HttpError("sandbox_unavailable")
        if self.session is not None:
            raise HttpError("busy")
        if not url_allowed(url, allowed):
            raise HttpError("origin_mismatch")
        try:
            chromium = self._start_browser()
        except Exception as exc:  # noqa: BLE001 - only the type is logged
            LOG.warning("chromium start failed (%s)", type(exc).__name__)
            raise HttpError("page_error") from None
        self.session = Session(chromium, self.config.action_seconds)
        return self._run(lambda s: s.open(url, allowed))

    def _run(self, action) -> dict:
        try:
            return action(self.session)
        except SessionError as exc:
            if exc.ends_session:
                self._end()
            raise HttpError(exc.code) from None


class _Handler(BaseHTTPRequestHandler):
    server_version = "bitwarden-signin"
    sys_version = ""
    timeout = 30  # seconds a client may take to send its request

    def _dispatch(self) -> None:
        self._route = None
        code = None
        try:
            path = urlsplit(self.path).path
        except ValueError:
            path = ""
        try:
            spec = ROUTES.get(path)
            if spec is None:
                raise HttpError("not_found")
            self._route = path
            method, keys = spec
            if self.command != method:
                raise HttpError("method_not_allowed")
            body = None
            if method == "POST":
                body = self._body()
                if set(body) != set(keys) or not all(_valid(kind, body[k]) for k, kind in keys.items()):
                    raise HttpError("bad_request")
            status, payload = 200, self.server.signer.handle(path, body)
        except HttpError as exc:
            status, payload, code = exc.status, {"error": exc.code}, exc.code
        except Exception as exc:  # noqa: BLE001 - the text could hold anything; only the type is logged
            LOG.warning("%s %s failed (%s)", self.command, self._route, type(exc).__name__)
            status, payload, code = 500, {"error": "internal"}, "internal"
        self._send(status, payload, code)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise HttpError("bad_request")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise HttpError("bad_request") from None
        if length < 0:
            raise HttpError("bad_request")
        if length > MAX_BODY:
            raise HttpError("too_large")
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise HttpError("bad_request")
        return _parse_json(raw) if raw else {}

    def _send(self, status: int, payload: dict, code: str | None) -> None:
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
        method = self.command if self.command in ("GET", "POST") else "-"
        LOG.info("%s %s %d %s", method, self._route or "-", status, code or "-")

    def send_error(self, code, message=None, explain=None) -> None:
        """Replace the stdlib's HTML error page, which repeats the request line."""
        self._route = getattr(self, "_route", None)
        name = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "too_large",
                414: "too_large", 431: "too_large", 501: "method_not_allowed"}.get(code, "bad_request")
        self._send(STATUS[name], {"error": name}, name)

    def log_message(self, format, *args) -> None:  # noqa: A002 - the stdlib's signature
        pass


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, signer: Signer) -> None:
        self.signer = signer
        super().__init__(path, _Handler)

    def handle_error(self, request, client_address) -> None:
        LOG.warning("socket connection error")


def _unix_server(path: Path, signer: Signer) -> _UnixServer:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise ConfigError(f"{path} exists and is not a socket")
        os.unlink(path)  # left by a signer that did not stop cleanly
    old = os.umask(0o177)  # the socket is created mode 600
    try:
        server = _UnixServer(str(path), signer)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server
