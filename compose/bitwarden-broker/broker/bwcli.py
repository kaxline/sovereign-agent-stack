"""The one place the broker runs the ``bw`` CLI.

One ``Bw`` per account. Each account has its own ``BITWARDENCLI_APPDATA_DIR``
under ``data_root/<slug>/`` and one lock, keyed on that directory, that every
call takes, so an account's bw commands never overlap even across ``Bw``
objects. Secrets reach bw only through the child environment built for that
call (``BW_CLIENTID``/``BW_CLIENTSECRET`` for login, ``BW_PW`` with
``--passwordenv BW_PW`` for unlock, ``BW_SESSION`` afterwards), never argv.
The rest of that environment is an allowlist. The client secret and master
password are read from ``secrets_root/<slug>/`` at the moment of use, and the
dicts that carry them are cleared once bw exits, so a traceback's frames do not
keep them. The session key lives on the instance and nowhere else.

bw's stdout and stderr stay inside ``_exec`` and ``_run``. A failure raises
``BwError(error_class)``, whose text is the class name alone, with no chained
exception (``__context__`` and ``__cause__`` are None).

``return_item`` sends the edited item JSON, which holds the password, to bw on
stdin rather than argv. ``secret_items`` is the one method that returns values;
the server calls it only for the Unix socket.

``unlock()`` and ``sync()`` refresh a cached list of login-item metadata.
``cached_items()`` serves it without running bw, and the list stays until a new
one replaces it or the account locks.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .accounts import ID_RE, Account

ERROR_CLASSES = ("auth", "locked", "network", "rate_limited", "unknown")
TIMEOUT_SECONDS = 120
DEFAULT_RETRY_AFTER = 60
MAX_RETRY_AFTER = 3600
SERVER_MARKER = ".broker-server"
ENV_ALLOW = frozenset({
    "PATH", "HOME", "LANG", "TZ", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
_RETRY_AFTER = re.compile(r"retry-after[\"']?\s*[:=]?\s*(\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Node prints deprecation and experimental warnings, with its PID, before bw's own message.
_NOISE = re.compile(r"^\s*(\(node:\d+\)|\(use `node|$)", re.IGNORECASE)

# Checked in order against bw's lower-cased stderr with the noise lines removed.
_PATTERNS = tuple((cls, re.compile("|".join(parts))) for cls, parts in (
    # bw 2026.10.1 prints "Rate limit exceeded. Try again later." for Vaultwarden's 429, with no Retry-After.
    ("rate_limited", (r"too many requests", r"rate limit exceeded", r"\b(?:status|http|code)\b\D{0,3}429\b",
                      r"\b429\b\s*too many")),
    ("auth", (r"invalid master password", r"username or password is incorrect",
              r"client_id or client_secret is incorrect", r"\bincorrect client_(?:id|secret)\b",
              r"\binvalid_client\b", r"\binvalid_grant\b",
              r"you are not logged in", r"\b401\b\s*unauthorized", r"\bunauthorized\b\s*\(?401\b",
              r"\b(?:status|http|code)\b\D{0,3}401\b")),
    ("locked", (r"vault is locked", r"session key is invalid")),
    ("network", (r"\b(?:econnrefused|econnreset|enotfound|etimedout|eai_again|ehostunreach|enetunreach)\b",
                 r"\bgetaddrinfo\b", r"fetch failed", r"socket hang up",
                 r"unable to verify the first certificate", r"self[- ]signed certificate",
                 r"certificate has expired")),
))

# bw 2026.10.1's unlock with a wrong password: "Cryptography error, The decryption operation failed".
# Any other command printing it has an item that does not decrypt, which is not an auth failure.
_UNLOCK_AUTH = re.compile(r"the decryption operation failed")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _account_lock(data_dir: Path) -> threading.Lock:
    key = os.path.realpath(data_dir)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class BwError(Exception):
    def __init__(self, error_class: str):
        if error_class not in ERROR_CLASSES:
            error_class = "unknown"
        super().__init__(error_class)
        self.error_class = error_class


def _clean(stderr: str) -> str:
    lines = _ANSI.sub("", stderr).lower().splitlines()
    return "\n".join(line for line in lines if not _NOISE.match(line))


def classify(stderr: str, unlock: bool = False) -> tuple[str, int | None]:
    """Return the error class for bw's stderr and, for a 429, the seconds to wait.

    ``unlock`` says the stderr is from ``bw unlock``, where a decryption failure means a wrong password.
    """
    text = _clean(stderr)
    for error_class, pattern in _PATTERNS:
        if pattern.search(text):
            if error_class != "rate_limited":
                return error_class, None
            match = _RETRY_AFTER.search(text)
            wait = int(match.group(1)) if match else DEFAULT_RETRY_AFTER
            return error_class, max(1, min(wait, MAX_RETRY_AFTER))
    if unlock and _UNLOCK_AUTH.search(text):
        return "auth", None
    return "unknown", None


def run_bw(bw_path: str, data_dir: Path | str, args: list[str], secret_env: dict[str, str],
           stdin: str | None = None, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run bw once with an allowlisted environment and return ``(code, stdout, stderr)``.

    ``secret_env`` is merged last, and the child's environment dict is cleared
    when bw exits. A timeout raises ``BwError("network")`` and a bw that cannot
    start raises ``BwError("unknown")``, neither with a chained exception.
    ``bitwarden-setup`` shares this runner for the user's temporary login.
    """
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith("LC_")}
    env.update(extra_env or {})
    env["BITWARDENCLI_APPDATA_DIR"] = str(data_dir)
    env["BW_NOINTERACTION"] = "true"
    env.update(secret_env)
    proc = None
    failure = None
    try:
        proc = subprocess.run(
            [bw_path, *args], env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_SECONDS,
            **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}),
        )
    except subprocess.TimeoutExpired:
        failure = "network"
    except OSError:
        failure = "unknown"
    finally:
        env.clear()
    # Raised here, outside the except blocks, so the BwError has no __context__
    # (a TimeoutExpired carries bw's partial stdout and stderr).
    if failure is not None:
        raise BwError(failure)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _sanitize(item: dict) -> dict | None:
    """Keep the metadata the broker lists; drop the password, TOTP seed, notes and fields."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    login = item.get("login") if isinstance(item.get("login"), dict) else {}
    uris = []
    for uri in login.get("uris") or []:
        if isinstance(uri, dict) and isinstance(uri.get("uri"), str):
            uris.append({"uri": uri["uri"], "match": uri.get("match")})
    collections = item.get("collectionIds")
    return {
        "id": item["id"],
        "type": item.get("type"),
        "name": item.get("name") if isinstance(item.get("name"), str) else "",
        "folderId": item.get("folderId"),
        "collectionIds": [c for c in collections if isinstance(c, str)] if isinstance(collections, list) else [],
        "username": login.get("username") if isinstance(login.get("username"), str) else None,
        "uris": uris,
        "has_totp": bool(login.get("totp")),
    }


sanitize_item = _sanitize  # bitwarden-setup lists the user's items through it


class Bw:
    def __init__(self, account: Account, data_root: Path, secrets_root: Path, bw_path: str = "bw",
                 extra_env: dict[str, str] | None = None):
        """``extra_env`` adds non-secret variables to bw's environment (tests pass FAKE_BW_*)."""
        extra_env = dict(extra_env or {})
        if any(k.startswith("BW_") for k in extra_env):
            raise ValueError("extra_env must not set BW_* variables")
        self.account = account
        self.data_dir = Path(data_root) / account.slug
        self.secrets_dir = Path(secrets_root) / account.slug
        self.bw_path = bw_path
        self.extra_env = extra_env
        self.retry_at: float | None = None  # time.monotonic() before which no bw call runs
        self._lock = _account_lock(self.data_dir)
        self._session: str | None = None
        self._logged_in = False
        self._items: list[dict] | None = None
        self._state = "locked"
        self._error_class = ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def error_class(self) -> str:
        return self._error_class

    def cached_items(self) -> list[dict]:
        """The last listed items, without running bw or waiting for the lock; [] unless unlocked."""
        items = self._items
        if self._state != "unlocked" or items is None:
            return []
        return [dict(i) for i in items]

    def cached_ids(self) -> set[str] | None:
        """Item ids in the last listing, or None when there is none (locked, or never listed).

        Unlike ``cached_items``, this tells an empty scope apart from a missing listing.
        """
        items = self._items  # read before the state: a lock() in between leaves a valid old list
        if self._state != "unlocked" or items is None:
            return None
        return {i["id"] for i in items}

    # Every helper below runs with self._lock held.

    def _read_secret(self, name: str) -> str:
        value = None
        try:
            value = (self.secrets_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
        if value is not None and value.endswith("\n"):
            value = value[:-1]
        if value is not None and value.endswith("\r"):
            value = value[:-1]
        if not value:
            raise BwError("auth")
        return value

    def _exec(self, args: list[str], secret_env: dict[str, str], stdin: str | None = None) -> tuple[int, str, str]:
        return run_bw(self.bw_path, self.data_dir, args, secret_env, stdin, self.extra_env)

    def _fail(self, error_class: str) -> BwError:
        # An "unknown" failure on an unlocked account is about one call (an item
        # with no TOTP, say), not the account, so it leaves error_class alone.
        if error_class != "unknown" or self._session is None:
            self._error_class = error_class
        if error_class in ("locked", "auth"):
            self._session = None
            self._items = None
            self._state = "error" if error_class == "auth" else "locked"
            if error_class == "auth":
                self._logged_in = False
        elif self._session is None:
            self._state = "offline" if error_class in ("network", "rate_limited") else "error"
        return BwError(error_class)

    def _run(self, args: list[str], secret_env: dict[str, str] | None = None, ok_if: tuple[str, ...] = (),
             stdin: str | None = None) -> str:
        secret_env = secret_env if secret_env is not None else {}
        try:
            if self.retry_at is not None:
                if time.monotonic() < self.retry_at:
                    raise self._fail("rate_limited")
                self.retry_at = None
            code, stdout, stderr = self._exec(args, secret_env, stdin)
        finally:
            # The callers' dicts hold the client secret or master password.
            secret_env.clear()
            stdin = None
        if code == 0 or (ok_if and any(p in stderr.lower() for p in ok_if)):
            return stdout
        error_class, wait = classify(stderr, unlock=args[:1] == ["unlock"])
        stdout = stderr = None  # keep bw's text out of this frame before raising
        if wait is not None:
            self.retry_at = time.monotonic() + wait
        raise self._fail(error_class)

    def _session_env(self) -> dict[str, str]:
        if self._session is None:
            raise self._fail("locked")
        return {"BW_SESSION": self._session}

    def _configure(self) -> None:
        marker = self.data_dir / SERVER_MARKER
        try:
            if marker.read_text(encoding="utf-8") == self.account.server:
                return
        except OSError:
            pass
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._run(["config", "server", self.account.server])
        marker.write_text(self.account.server, encoding="utf-8")

    def _login(self) -> None:
        self._configure()
        env = {"BW_CLIENTID": self.account.client_id, "BW_CLIENTSECRET": self._read_secret("client_secret")}
        self._run(["login", "--apikey"], env, ok_if=("already logged in",))
        self._logged_in = True

    def _unlock(self, password: str | None) -> None:
        try:
            if password is None and self.account.unlock != "stored":
                raise self._fail("locked")
            if not self._logged_in:
                self._login()
            env = {"BW_PW": password if password is not None else self._read_secret("master_password")}
            password = None
            key = self._run(["unlock", "--passwordenv", "BW_PW", "--raw"], env).strip()
        finally:
            password = None
        if not key:
            raise self._fail("unknown")
        self._session = key
        key = None
        self._state = "unlocked"
        self._error_class = ""
        self._refresh()

    def _list(self, what: str, extra: tuple[str, ...] = ()) -> list:
        out = self._run(["list", what, *extra], self._session_env())
        data = None
        try:
            data = json.loads(out or "[]")
        except ValueError:
            pass
        out = None
        if not isinstance(data, list):
            raise self._fail("unknown")
        return data

    def _scoped(self, kind: str, value: str | None) -> tuple[list, object]:
        """Full item JSON (passwords included) listed for a scope, and a test on sanitized items."""
        raw: list = []
        if kind == "folder":
            folder_ids = [f.get("id") for f in self._list("folders")
                          if isinstance(f, dict) and f.get("name") == value and isinstance(f.get("id"), str)]
            # Bitwarden allows several folders with one name; the scope covers all of them.
            for fid in folder_ids:
                raw.extend(self._list("items", ("--folderid", fid)))
            return raw, lambda i: i["folderId"] in folder_ids
        if kind == "collection":
            return self._list("items", ("--collectionid", value)), lambda i: value in i["collectionIds"]
        return self._list("items"), lambda i: True

    def _refresh(self) -> list[dict]:
        if self._session is None:
            raise self._fail("locked")
        raw: list = []
        try:
            raw, keep = self._scoped(self.account.scope_kind, self.account.scope_value)
            items = [i for i in map(_sanitize, raw) if i is not None and i["type"] == 1 and keep(i)]
        finally:
            raw = None  # full item JSON, passwords included
        self._items = items
        return [dict(i) for i in items]

    # Public API: each method takes the account's lock once.

    def login(self) -> None:
        with self._lock:
            self._login()

    def unlock(self, password: str | None = None) -> None:
        """Unlock with ``password``, or with the stored master password when it is None, then list items."""
        try:
            with self._lock:
                self._unlock(password)
        finally:
            password = None

    def lock(self) -> None:
        """Lock bw and forget the session key and items. The account is locked even if bw fails."""
        with self._lock:
            env = {"BW_SESSION": self._session} if self._session is not None else None
            self._session = None
            self._items = None
            if env is not None:
                try:
                    self._run(["lock"], env)
                except BwError:
                    pass
            self._state = "locked"

    def sync(self) -> None:
        """Fetch from the server, then refresh the item list. On failure the last list stays."""
        with self._lock:
            self._run(["sync"], self._session_env())
            self._error_class = ""
            self._refresh()

    def list_items(self) -> list[dict]:
        """List login-item metadata within the account's scope, without passwords or TOTP seeds.

        During a 429 backoff this returns the last list without running bw.
        """
        with self._lock:
            if self._session is None:
                raise self._fail("locked")
            if self._items is not None and self.retry_at is not None and time.monotonic() < self.retry_at:
                return [dict(i) for i in self._items]
            return self._refresh()

    def _get(self, what: str, item_id: str) -> str:
        if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
            raise BwError("unknown")
        return self._run(["get", what, item_id], self._session_env())

    def get_password(self, item_id: str) -> str:
        with self._lock:
            value = self._get("password", item_id)
        # bw ends its output with one newline; anything before it belongs to the password.
        return value[:-1] if value.endswith("\n") else value

    def get_totp(self, item_id: str) -> str:
        with self._lock:
            return self._get("totp", item_id).strip()

    def return_item(self, item_id: str) -> bool:
        """Take an item out of the account's scope; False if the scope is ``all`` or bw refuses.

        ``folder:``: the item's folder becomes none (``bw edit item`` with the item JSON on stdin).
        ``collection:``: the item leaves the scope collection and keeps its others
        (``bw edit item-collections`` with the remaining ids on stdin); it is unassigned only
        when the scope collection was its only one.
        The item is read first, and one already out of scope (moved by the user after a
        failed release, say) gets no edit and counts as returned. The item list is refreshed
        afterwards when that is possible.
        """
        kind = self.account.scope_kind
        if kind not in ("folder", "collection"):
            return False
        with self._lock:
            item = encoded = None
            try:
                raw = self._get("item", item_id)
                try:
                    item = json.loads(raw)
                except ValueError:
                    item = None
                raw = None
                if not isinstance(item, dict) or item.get("id") != item_id:
                    return False
                if kind == "folder":
                    scope_ids = {f.get("id") for f in self._list("folders")
                                 if isinstance(f, dict) and f.get("name") == self.account.scope_value}
                    if item.get("folderId") in scope_ids:
                        item["folderId"] = None
                        encoded = base64.b64encode(json.dumps(item).encode("utf-8")).decode("ascii")
                        self._run(["edit", "item", item_id], self._session_env(), stdin=encoded)
                else:
                    current = item.get("collectionIds")
                    if not isinstance(current, list) or not all(isinstance(c, str) for c in current):
                        return False
                    if self.account.scope_value in current:
                        org = item.get("organizationId")
                        if not isinstance(org, str) or not ID_RE.fullmatch(org):
                            return False
                        remaining = [c for c in current if c != self.account.scope_value]
                        encoded = base64.b64encode(json.dumps(remaining).encode("utf-8")).decode("ascii")
                        self._run(["edit", "item-collections", item_id, "--organizationid", org],
                                  self._session_env(), stdin=encoded)
            except BwError:
                return False
            finally:
                item = encoded = None
            try:
                self._refresh()
            except BwError:
                pass
            return True

    def secret_items(self) -> list[tuple[str, str]]:
        """``(name, password)`` for each login item in ``BW_SECRETS_ITEM_SCOPE``; [] when it is unset."""
        scope = self.account.secrets_item_scope
        if scope is None:
            return []
        with self._lock:
            if self._session is None:
                raise self._fail("locked")
            raw: list = []
            try:
                raw, keep = self._scoped(*scope)
                out = []
                for item in raw:
                    meta = _sanitize(item)
                    if meta is None or meta["type"] != 1 or not keep(meta):
                        continue
                    password = (item.get("login") or {}).get("password")
                    if isinstance(meta["name"], str) and meta["name"] and isinstance(password, str) and password:
                        out.append((meta["name"], password))
                return out
            finally:
                raw = None
