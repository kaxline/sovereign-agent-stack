"""Login items the broker offers, across accounts, as metadata only.

A handle is ``bwbroker:<slug>/<item id>``. An item is listed only when it is a
login with at least one http or https URI whose match type is not "Never"
(``match: 5``); those URIs, normalized by ``origins.normalize_origin``, are its
fill origins. Handles on the denylist are neither listed nor returned by
``get``. Items come from each account's cached list (``Bw.cached_items``), which
``unlock()`` and ``sync()`` refresh, so listing never runs bw. A locked account
contributes no items.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .bwcli import Bw
from .origins import normalize_origin

HANDLE_PREFIX = "bwbroker:"
MATCH_NEVER = 5


@dataclass(frozen=True)
class ItemMeta:
    handle: str
    label: str
    username: str | None
    origins: tuple[str, ...]
    has_totp: bool
    account: str
    item_id: str


def parse_handle(handle: str) -> tuple[str, str] | None:
    if not isinstance(handle, str) or not handle.startswith(HANDLE_PREFIX):
        return None
    slug, sep, item_id = handle[len(HANDLE_PREFIX):].partition("/")
    if not sep or not slug or not item_id:
        return None
    return slug, item_id


def fill_origins(uris: list[dict]) -> tuple[str, ...]:
    out: list[str] = []
    for uri in uris:
        if uri.get("match") == MATCH_NEVER:
            continue
        origin = normalize_origin(uri.get("uri"))
        if origin and origin.startswith(("http://", "https://")) and origin not in out:
            out.append(origin)
    return tuple(out)


class Denylist:
    """Handles the broker refuses to list or fill, kept in a JSON list at ``path`` (mode 600)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._handles: set[str] = set()
        if self.path.exists():
            # A corrupt file raises rather than loading as empty, which would release every entry.
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not all(isinstance(h, str) for h in data):
                raise ValueError(f"{self.path}: expected a JSON list of handles")
            self._handles = set(data)

    def __contains__(self, handle: object) -> bool:
        with self._lock:
            return handle in self._handles

    def handles(self) -> list[str]:
        with self._lock:
            return sorted(self._handles)

    def add(self, handle: str) -> None:
        with self._lock:
            if handle not in self._handles:
                self._save(self._handles | {handle})

    def remove(self, handle: str) -> None:
        with self._lock:
            if handle in self._handles:
                self._save(self._handles - {handle})

    def _save(self, handles: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".denylist-", dir=self.path.parent)  # created mode 600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sorted(handles), f)
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
        self._handles = handles


class Unreturned:
    """Logins the broker took out of the agent's use but Bitwarden would not move out of its scope.

    A JSON list of ``{"handle", "account", "label", "site"}`` at ``path`` (mode 600). The
    label and site are kept so the entry survives a lock, when the item can no longer be looked up.
    """

    FIELDS = ("handle", "account", "label", "site")

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        if self.path.exists():
            # A corrupt file raises rather than loading as empty, which would forget the records.
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ok = isinstance(data, list) and all(
                isinstance(r, dict) and all(isinstance(r.get(k), str) for k in self.FIELDS) for r in data)
            if not ok:
                raise ValueError(f"{self.path}: expected a JSON list of unreturned records")
            self._records = {r["handle"]: {k: r[k] for k in self.FIELDS} for r in data}

    def __contains__(self, handle: object) -> bool:
        with self._lock:
            return handle in self._records

    def add(self, handle: str, account: str, label: str, site: str) -> None:
        with self._lock:
            record = {"handle": handle, "account": account, "label": label, "site": site}
            if self._records.get(handle) != record:
                self._save({**self._records, handle: record})

    def remove(self, handle: str) -> None:
        with self._lock:
            if handle in self._records:
                self._save({h: r for h, r in self._records.items() if h != handle})

    def entries(self) -> list[dict]:
        """``{account, label, site}`` per record, sorted by account and then label; no handle."""
        with self._lock:
            rows = [{k: r[k] for k in ("account", "label", "site")} for r in self._records.values()]
        return sorted(rows, key=lambda r: (r["account"], r["label"], r["site"]))

    def _save(self, records: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".unreturned-", dir=self.path.parent)  # created mode 600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([records[h] for h in sorted(records)], f)
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
        self._records = records


class Vault:
    def __init__(self, bw_by_slug: dict[str, Bw], denylist: Denylist):
        self.bw_by_slug = bw_by_slug
        self.denylist = denylist

    def _account_items(self, slug: str) -> list[ItemMeta]:
        bw = self.bw_by_slug.get(slug)
        if bw is None:
            return []
        out = []
        for item in bw.cached_items():
            item_origins = fill_origins(item["uris"])
            if not item_origins:
                continue
            handle = f"{HANDLE_PREFIX}{slug}/{item['id']}"
            if handle in self.denylist:
                continue
            out.append(ItemMeta(
                handle=handle, label=item["name"] or item_origins[0], username=item["username"] or None,
                origins=item_origins, has_totp=item["has_totp"], account=slug, item_id=item["id"],
            ))
        return out

    def items(self) -> list[ItemMeta]:
        return [meta for slug in sorted(self.bw_by_slug) for meta in self._account_items(slug)]

    def get(self, handle: str) -> ItemMeta | None:
        parsed = parse_handle(handle)
        if parsed is None:
            return None
        return next((m for m in self._account_items(parsed[0]) if m.handle == handle), None)
