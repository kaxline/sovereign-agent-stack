"""Access requests the agent files with ``POST /requests``, kept in ``requests.json``.

A request names an account, a site and the agent's reason. ``site`` must be a
hostname with at least one dot, or a URL whose host is taken; it is stored
lower-case in its ASCII (IDNA) form. A single label ("com", "localhost") is
refused: requests match subdomains, so it would cover every host under it. ``reason`` loses every Unicode category ``C*`` character and is
cut to 200 characters. At most 10 requests are pending; a repeat of a pending
(account, site) pair returns that request's id instead of adding one. Requests
older than 7 days are dropped by ``expire``; the server runs it before each
``add`` and from its sync loop.

The file is a JSON object, written atomically with mode 600. A file that
does not parse raises at load, so a damaged store never loads as empty.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

MAX_PENDING = 10
MAX_AGE_SECONDS = 7 * 86400
MAX_REASON = 200
RECENT_LIMIT = 20
ERRORS = ("invalid_site", "too_many", "unknown_account", "invalid_reason")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_FIELDS = {"request_id": str, "account": (str, type(None)), "site": str, "reason": str,
           "created": int, "status": str, "done_at": (int, type(None))}


class RequestError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_site(site) -> str | None:
    """The lower-case ASCII hostname in ``site``, or None if it is not one or has no dot."""
    if not isinstance(site, str):
        return None
    value = site.strip()
    if "://" in value:
        try:
            parts = urlsplit(value)
            if parts.username is not None or parts.password is not None:
                return None
            parts.port  # noqa: B018 - raises on a bad port
        except ValueError:
            return None
        value = parts.hostname or ""
    value = value.rstrip(".").lower()
    if not value or len(value) > 253:
        return None
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if "." not in value or not all(_LABEL.fullmatch(label) for label in value.split(".")):
        return None
    return value


def clean_reason(reason) -> str:
    if reason is None:
        return ""
    if not isinstance(reason, str):
        raise RequestError("invalid_reason")
    return "".join(c for c in reason if not unicodedata.category(c).startswith("C"))[:MAX_REASON]


def same_site(a: str, b: str) -> bool:
    """True when one host is the other or a subdomain of it."""
    return a == b or a.endswith("." + b) or b.endswith("." + a)


class Store:
    def __init__(self, path: Path, accounts=None):
        """``accounts``, when given, is the set of slugs ``add`` accepts; None is always accepted."""
        self.path = Path(path)
        self.accounts = None if accounts is None else frozenset(accounts)
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        if self.path.exists():
            self._requests = _load(self.path)

    def add(self, account, site, reason, now: float | None = None) -> str:
        now = time.time() if now is None else now
        host = normalize_site(site)
        if host is None:
            raise RequestError("invalid_site")
        if account is not None and (not isinstance(account, str)
                                    or (self.accounts is not None and account not in self.accounts)):
            raise RequestError("unknown_account")
        text = clean_reason(reason)
        with self._lock:
            kept = [dict(r) for r in self._requests]
            for r in kept:
                if r["status"] == "pending" and r["account"] == account and r["site"] == host:
                    return r["request_id"]
            if sum(r["status"] == "pending" for r in kept) >= MAX_PENDING:
                raise RequestError("too_many")
            request_id = secrets.token_hex(8)
            kept.append({"request_id": request_id, "account": account, "site": host, "reason": text,
                         "created": int(now), "status": "pending", "done_at": None})
            self._save(kept)
            return request_id

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._requests if r["status"] == "pending"]

    def recent(self) -> list[dict]:
        """Done requests, newest first."""
        with self._lock:
            done = [dict(r) for r in self._requests if r["status"] == "done"]
        done.sort(key=lambda r: r["done_at"] or 0, reverse=True)
        return done[:RECENT_LIMIT]

    def mark_done(self, site: str, now: float | None = None) -> int:
        """Mark pending requests for ``site`` (or a host it covers, either way) done; return how many."""
        host = normalize_site(site)
        if host is None:
            return 0
        now = int(time.time() if now is None else now)
        with self._lock:
            updated = [dict(r) for r in self._requests]
            count = 0
            for r in updated:
                if r["status"] == "pending" and same_site(r["site"], host):
                    r["status"], r["done_at"] = "done", now
                    count += 1
            if count:
                self._save(updated)
            return count

    def expire(self, now: float | None = None) -> None:
        with self._lock:
            kept = self._unexpired(time.time() if now is None else now)
            if len(kept) != len(self._requests):
                self._save(kept)

    def _unexpired(self, now: float) -> list[dict]:
        return [dict(r) for r in self._requests if r["created"] > now - MAX_AGE_SECONDS]

    def _save(self, requests: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".requests-", dir=self.path.parent)  # created mode 600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"requests": requests}, f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._requests = requests


def _load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    requests = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(requests, list):
        raise ValueError(f"{path}: expected an object with a requests list")
    for r in requests:
        if (not isinstance(r, dict) or set(r) != set(_FIELDS)
                or not all(isinstance(r[k], t) and not isinstance(r[k], bool) for k, t in _FIELDS.items())
                or r["status"] not in ("pending", "done")):
            raise ValueError(f"{path}: a request entry is malformed")
    return requests
