"""The one place the broker runs the ``bw`` CLI.

One ``Bw`` per account. Each has its own ``BITWARDENCLI_APPDATA_DIR`` under
``data_root/<slug>/`` and one lock that every call takes, so an account's bw
commands never overlap. Secrets reach bw only through the child environment
built for that call (``BW_CLIENTID``/``BW_CLIENTSECRET`` for login, ``BW_PW``
with ``--passwordenv BW_PW`` for unlock, ``BW_SESSION`` afterwards), never
argv. The client secret and master password are read from
``secrets_root/<slug>/`` at the moment of use and held only in locals. The
session key lives on the instance and nowhere else.

bw's stdout and stderr stay inside ``_exec``. A failure raises
``BwError(error_class)``, whose text is the class name alone.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .accounts import Account

ERROR_CLASSES = ("auth", "locked", "network", "rate_limited", "unknown")
TIMEOUT_SECONDS = 120
DEFAULT_RETRY_AFTER = 60
MAX_RETRY_AFTER = 3600
SERVER_MARKER = ".broker-server"
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_RETRY_AFTER = re.compile(r"retry-after[\"']?\s*[:=]?\s*(\d+)")

# Checked in order against bw's lower-cased stderr.
_PATTERNS = (
    ("rate_limited", ("429", "too many requests", "rate limit")),
    ("auth", ("invalid master password", "username or password is incorrect", "client_id or client_secret",
              "invalid_client", "invalid_grant", "you are not logged in", "not logged in", "unauthorized",
              "api key", "401")),
    ("locked", ("vault is locked", "session key is invalid", "locked")),
    ("network", ("econnrefused", "econnreset", "enotfound", "etimedout", "eai_again", "getaddrinfo",
                 "fetch failed", "socket hang up", "network", "certificate", "self-signed", "self signed",
                 "unable to verify")),
)


class BwError(Exception):
    def __init__(self, error_class: str):
        if error_class not in ERROR_CLASSES:
            error_class = "unknown"
        super().__init__(error_class)
        self.error_class = error_class


def _classify(stderr: str) -> str:
    text = stderr.lower()
    for error_class, needles in _PATTERNS:
        if any(n in text for n in needles):
            return error_class
    return "unknown"


def _retry_after(stderr: str) -> int:
    match = _RETRY_AFTER.search(stderr.lower())
    if not match:
        return DEFAULT_RETRY_AFTER
    return max(1, min(int(match.group(1)), MAX_RETRY_AFTER))


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


class Bw:
    def __init__(self, account: Account, data_root: Path, secrets_root: Path, bw_path: str = "bw"):
        self.account = account
        self.data_dir = Path(data_root) / account.slug
        self.secrets_dir = Path(secrets_root) / account.slug
        self.bw_path = bw_path
        self.retry_at: float | None = None  # time.monotonic() before which no bw call runs
        self._lock = threading.Lock()
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

    # Every helper below runs with self._lock held.

    def _read_secret(self, name: str) -> str:
        try:
            value = (self.secrets_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise BwError("auth") from None
        value = value[:-1] if value.endswith("\n") else value
        value = value[:-1] if value.endswith("\r") else value
        if not value:
            raise BwError("auth")
        return value

    def _exec(self, args: list[str], secret_env: dict[str, str]) -> tuple[int, str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BW_")}
        env["BITWARDENCLI_APPDATA_DIR"] = str(self.data_dir)
        env["BW_NOINTERACTION"] = "true"
        env.update(secret_env)
        try:
            proc = subprocess.run(
                [self.bw_path, *args], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise BwError("network") from None
        except OSError:
            raise BwError("unknown") from None
        return proc.returncode, proc.stdout or "", proc.stderr or ""

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

    def _run(self, args: list[str], secret_env: dict[str, str] | None = None, ok_if: tuple[str, ...] = ()) -> str:
        if self.retry_at is not None:
            if time.monotonic() < self.retry_at:
                raise self._fail("rate_limited")
            self.retry_at = None
        code, stdout, stderr = self._exec(args, secret_env or {})
        if code == 0:
            return stdout
        lowered = stderr.lower()
        if any(p in lowered for p in ok_if):
            return stdout
        error_class = _classify(stderr)
        if error_class == "rate_limited":
            self.retry_at = time.monotonic() + _retry_after(stderr)
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
        client_secret = self._read_secret("client_secret")
        env = {"BW_CLIENTID": self.account.client_id, "BW_CLIENTSECRET": client_secret}
        del client_secret
        self._run(["login", "--apikey"], env, ok_if=("already logged in",))
        self._logged_in = True

    def _unlock(self, password: str | None) -> None:
        if password is None:
            if self.account.unlock != "stored":
                raise self._fail("locked")
            password = self._read_secret("master_password")
        if not self._logged_in:
            self._login()
        env = {"BW_PW": password}
        del password
        key = self._run(["unlock", "--passwordenv", "BW_PW", "--raw"], env).strip()
        if not key:
            raise self._fail("unknown")
        self._session = key
        self._items = None
        self._state = "unlocked"
        self._error_class = ""

    # Public API: each method takes the account's lock once.

    def login(self) -> None:
        with self._lock:
            self._login()

    def unlock(self, password: str | None = None) -> None:
        """Unlock with ``password``, or with the stored master password when it is None."""
        with self._lock:
            self._unlock(password)

    def lock(self) -> None:
        """Lock bw and forget the session key. The local state is locked even if bw fails."""
        with self._lock:
            session = self._session
            self._session = None
            self._items = None
            self._state = "locked"
            if session is not None:
                try:
                    self._run(["lock"], {"BW_SESSION": session})
                except BwError:
                    pass
            self._state = "locked"

    def sync(self) -> None:
        with self._lock:
            self._run(["sync"], self._session_env())
            self._items = None
            self._error_class = ""

    def _list(self, what: str, extra: tuple[str, ...] = ()) -> list:
        out = self._run(["list", what, *extra], self._session_env())
        try:
            data = json.loads(out or "[]")
        except ValueError:
            raise self._fail("unknown") from None
        if not isinstance(data, list):
            raise self._fail("unknown")
        return data

    def list_items(self) -> list[dict]:
        """Login-item metadata within the account's scope, without passwords or TOTP seeds.

        During a 429 backoff this returns the last list without running bw.
        """
        with self._lock:
            if self._session is None:
                raise self._fail("locked")
            if self._items is not None and self.retry_at is not None and time.monotonic() < self.retry_at:
                return [dict(i) for i in self._items]
            kind, value = self.account.scope_kind, self.account.scope_value
            if kind == "folder":
                folder_ids = [f.get("id") for f in self._list("folders")
                              if isinstance(f, dict) and f.get("name") == value and isinstance(f.get("id"), str)]
                if not folder_ids:
                    self._items = []
                    return []
                # Bitwarden allows several folders with one name; the scope covers all of them.
                raw = [i for fid in folder_ids for i in self._list("items", ("--folderid", fid))]
                keep = lambda i: i["folderId"] in folder_ids  # noqa: E731
            elif kind == "collection":
                raw = self._list("items", ("--collectionid", value))
                keep = lambda i: value in i["collectionIds"]  # noqa: E731
            else:
                raw = self._list("items")
                keep = lambda i: True  # noqa: E731
            items = [i for i in map(_sanitize, raw) if i is not None and i["type"] == 1 and keep(i)]
            del raw
            self._items = items
            return [dict(i) for i in items]

    def _get(self, what: str, item_id: str) -> str:
        if not isinstance(item_id, str) or not _ITEM_ID.fullmatch(item_id):
            raise BwError("unknown")
        return self._run(["get", what, item_id], self._session_env())

    def get_password(self, item_id: str) -> str:
        with self._lock:
            return self._get("password", item_id).rstrip("\r\n")

    def get_totp(self, item_id: str) -> str:
        with self._lock:
            return self._get("totp", item_id).strip()
