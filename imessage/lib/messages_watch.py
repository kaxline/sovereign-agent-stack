"""Keep the Messages index current and raise events when new messages arrive.

The watcher follows the design of openclaw/imsg's MessageWatcher (MIT): kqueue
on chat.db, its -wal and -shm files and the Messages folder; a short debounce;
a fallback poll in case an event is missed; and re-attaching when a file is
replaced (the WAL is deleted and recreated as Messages opens and closes it).
Each wake runs an incremental index pass (rows after a ROWID cursor plus a
recent window by date, where edits, unsends and late chat joins happen). A
full pass runs at start, when the scope changes, and on a timer.

Events say that new messages arrived in a chat, never what they say: the chat
id and name, how many, who sent them, and index ids the agent can look up
through the MCP. They go to a folder of JSON files, to an HTTP endpoint
signed like Hermes' generic webhook (HMAC-SHA256 of "<timestamp>.<body>" in
X-Webhook-Signature-V2, with X-Webhook-Timestamp and X-Request-ID), or both.

Nothing here writes to the Messages folder. Logs are counts only.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import select
import sqlite3
import threading
import time
import tomllib
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import messages_index as mi
from webhook import post_event, sign  # noqa: F401  (re-exported for callers)

STATE_VERSION = 1
EVENT_TYPE = "messages.new"
TEST_EVENT_TYPE = "messages.test"
MAX_PENDING = 200
PENDING_TTL = 3600  # seconds a failed webhook delivery is retried before it's dropped


# --- Config ------------------------------------------------------------------------


@dataclass
class WatchConfig:
    window_hours: float = 2.0
    full_every_minutes: float = 60.0
    poll_seconds: float = 5.0
    debounce_ms: int = 250
    state: Path | None = None


@dataclass
class NotifyConfig:
    chats: list[str]
    url: str | None = None
    secret: bytes | None = None
    outbox: Path | None = None
    max_age_minutes: float = 10.0
    quiet: tuple[int, int] | None = None  # minutes after local midnight: (start, end)


def _number(table: dict, key: str, default: float, minimum: float) -> float:
    value = table.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
        raise mi.ConfigError(f"`{key}` must be a number of at least {minimum}")
    return float(value)


def _quiet(value) -> tuple[int, int] | None:
    if value in (None, ""):
        return None
    try:
        start, end = (datetime.strptime(part.strip(), "%H:%M") for part in str(value).split("-"))
    except ValueError:
        raise mi.ConfigError('`quiet_hours` must look like "22:00-07:00"') from None
    return start.hour * 60 + start.minute, end.hour * 60 + end.minute


def _check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise mi.ConfigError("`notify.url` must be an http(s) URL")
    if parts.scheme == "http":
        host = parts.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost" or host.endswith(".localhost")
        if not local:
            raise mi.ConfigError("`notify.url` must use https unless it points at this machine")
    return url


def load(path: Path) -> tuple[mi.Config, WatchConfig, NotifyConfig | None]:
    """The indexer config plus its [watch] and [notify] tables."""
    config = mi.load_config(path)
    raw = tomllib.loads(Path(path).read_text())
    base = Path(path).resolve().parent

    w = raw.get("watch", {})
    if not isinstance(w, dict):
        raise mi.ConfigError("`watch` must be a table")
    unknown = set(w) - {"window_hours", "full_every_minutes", "poll_seconds", "debounce_ms", "state"}
    if unknown:
        raise mi.ConfigError(f"unknown [watch] keys: {', '.join(sorted(unknown))}")
    watch = WatchConfig(
        window_hours=_number(w, "window_hours", 2.0, 0.25),
        full_every_minutes=_number(w, "full_every_minutes", 60.0, 5.0),
        poll_seconds=_number(w, "poll_seconds", 5.0, 0.5),
        debounce_ms=int(_number(w, "debounce_ms", 250, 0)),
        state=mi._path(w["state"], "watch.state", base) if w.get("state") else None,
    )
    if watch.state is not None and (watch.state == config.store or config.store in watch.state.parents):
        raise mi.ConfigError("`watch.state` must be outside the Messages folder")

    n = raw.get("notify")
    if n is None:
        return config, watch, None
    if not isinstance(n, dict):
        raise mi.ConfigError("`notify` must be a table")
    unknown = set(n) - {"chats", "url", "secret_file", "outbox", "max_age_minutes", "quiet_hours"}
    if unknown:
        raise mi.ConfigError(f"unknown [notify] keys: {', '.join(sorted(unknown))}")
    chats = n.get("chats", [])
    if not isinstance(chats, list) or not chats or not all(isinstance(c, str) and c for c in chats):
        raise mi.ConfigError("`notify.chats` must list the chats (ids or patterns) that raise events")
    url = _check_url(n["url"]) if n.get("url") else None
    secret = None
    if url:
        if not n.get("secret_file"):
            raise mi.ConfigError("`notify.url` needs `notify.secret_file` (the shared HMAC secret)")
        secret_path = mi._path(n["secret_file"], "notify.secret_file", base)
        try:
            secret = secret_path.read_bytes().strip()
        except FileNotFoundError:
            raise mi.ConfigError(f"secret file not found: {secret_path}") from None
        if len(secret) < 16:
            raise mi.ConfigError("the notify secret must be at least 16 bytes")
    outbox = mi._path(n["outbox"], "notify.outbox", base) if n.get("outbox") else None
    if outbox is not None and (outbox == config.store or config.store in outbox.parents):
        raise mi.ConfigError("`notify.outbox` must be outside the Messages folder")
    if not url and not outbox:
        raise mi.ConfigError("[notify] needs `url`, `outbox` or both")
    notify = NotifyConfig(chats=list(chats), url=url, secret=secret, outbox=outbox,
                          max_age_minutes=_number(n, "max_age_minutes", 10.0, 1.0),
                          quiet=_quiet(n.get("quiet_hours")))
    return config, watch, notify


# --- Events ------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def new_message_events(index: Path, ids: list[int], notify: NotifyConfig, now: float) -> list[dict]:
    """One event per chat for newly indexed messages that should notify."""
    if not ids:
        return []
    db = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(ids))
        rows = db.execute(
            "SELECT m.id, m.chat_guid, m.date, m.sender, m.sender_name, c.display_name, c.is_group,"
            f" c.participants FROM messages m JOIN chats c ON c.guid = m.chat_guid WHERE m.id IN ({marks})"
            " AND m.is_from_me = 0 AND m.kind = 'message' ORDER BY m.date, m.id", ids).fetchall()
    finally:
        db.close()
    oldest = now - notify.max_age_minutes * 60
    by_chat: dict[str, dict] = {}
    for mid, chat, date, sender, sender_name, name, is_group, participants in rows:
        if date < oldest or not any(fnmatch.fnmatchcase(chat, p) for p in notify.chats):
            continue
        event = by_chat.get(chat)
        if event is None:
            people = json.loads(participants or "[]")
            title = name or ", ".join(p["name"] or p["handle"] for p in people) or chat
            event = by_chat[chat] = {"type": EVENT_TYPE, "id": uuid.uuid4().hex, "created_at": _iso(now),
                                     "chat": {"id": chat, "name": title, "is_group": bool(is_group)},
                                     "count": 0, "senders": [], "message_ids": [], "latest_at": None}
        event["count"] += 1
        who = sender_name or sender
        if who not in event["senders"]:
            event["senders"].append(who)
        event["message_ids"].append(mid)
        event["latest_at"] = _iso(date)
    return list(by_chat.values())


def merge_held(held: list[dict], events: list[dict]) -> list[dict]:
    """Fold events into the quiet-hours queue, one entry per chat."""
    by_chat = {e["chat"]["id"]: e for e in held}
    for e in events:
        prev = by_chat.get(e["chat"]["id"])
        if prev is None:
            by_chat[e["chat"]["id"]] = dict(e)
            continue
        prev["count"] += e["count"]
        prev["senders"] += [s for s in e["senders"] if s not in prev["senders"]]
        prev["message_ids"] += e["message_ids"]
        prev["latest_at"] = e["latest_at"]
    return list(by_chat.values())


def in_quiet_hours(quiet: tuple[int, int] | None, now: float) -> bool:
    if quiet is None:
        return False
    local = datetime.fromtimestamp(now)
    minute = local.hour * 60 + local.minute
    start, end = quiet
    return start <= minute < end if start <= end else minute >= start or minute < end


# --- Sinks -------------------------------------------------------------------------


def write_outbox(outbox: Path, event: dict) -> None:
    outbox.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(outbox, 0o700)
    except PermissionError:
        pass
    name = f"{int(time.time() * 1000)}-{event['id']}.json"
    tmp = outbox / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(event, f, ensure_ascii=False)
    os.replace(tmp, outbox / name)


# --- Files -------------------------------------------------------------------------


class FileWatch:
    """kqueue on a few paths, re-attached when a file is replaced. Without
    kqueue (not macOS), wait() just sleeps and the fallback poll does the work."""

    FFLAGS = 0

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.kq = select.kqueue() if hasattr(select, "kqueue") else None
        self.fds: dict[Path, tuple[int, tuple[int, int]]] = {}
        if self.kq is not None:
            self.FFLAGS = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE
                           | select.KQ_NOTE_RENAME | select.KQ_NOTE_ATTRIB)

    def refresh(self) -> None:
        if self.kq is None:
            return
        for path in self.paths:
            try:
                st = os.stat(path)
            except OSError:
                self._close(path)
                continue
            current = self.fds.get(path)
            if current and current[1] == (st.st_dev, st.st_ino):
                continue
            self._close(path)
            try:
                fd = os.open(path, getattr(os, "O_EVTONLY", os.O_RDONLY))
            except OSError:
                continue
            self.kq.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                           flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                           fflags=self.FFLAGS)], 0, 0)
            self.fds[path] = (fd, (st.st_dev, st.st_ino))

    def wait(self, timeout: float) -> bool:
        if self.kq is None:
            time.sleep(max(0.0, timeout))
            return False
        return bool(self.kq.control(None, 32, max(0.0, timeout)))

    def _close(self, path: Path) -> None:
        entry = self.fds.pop(path, None)
        if entry:
            try:
                os.close(entry[0])
            except OSError:
                pass

    def close(self) -> None:
        for path in list(self.fds):
            self._close(path)
        if self.kq is not None:
            self.kq.close()


# --- Watcher -----------------------------------------------------------------------


@dataclass
class SyncResult:
    mode: str
    added: int = 0
    updated: int = 0
    removed: int = 0
    events: int = 0
    delivered: int = 0
    held: int = 0
    pending: int = 0
    dropped: int = 0

    def changed(self) -> bool:
        return any((self.added, self.updated, self.removed, self.events, self.delivered, self.dropped))

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Watcher:
    config: mi.Config
    watch: WatchConfig
    notify: NotifyConfig | None = None
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = print
    post: Callable[[str, bytes, dict], bool] = post_event
    state: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state_path = self.watch.state or Path(f"{self.config.index}.watch.json")
        self.state = self._load_state()

    # State: cursor, last full run, scope, quiet-hours queue, failed deliveries.
    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
            if state.get("version") == STATE_VERSION:
                return state
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {"version": STATE_VERSION, "cursor": None, "last_full": 0, "scope": None,
                "held": [], "pending": []}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def _deliver(self, event: dict) -> bool:
        ok = True
        if self.notify.outbox is not None:
            write_outbox(self.notify.outbox, event)
        if self.notify.url:
            ok = self.post(self.notify.url, self.notify.secret, event)
        return ok

    def sync(self, force_full: bool = False) -> SyncResult:
        now = self.clock()
        st = self.state
        scope = self.config.scope_fingerprint()
        full = (force_full or st["cursor"] is None or st["scope"] != scope
                or now - st["last_full"] >= self.watch.full_every_minutes * 60)
        baseline = st["cursor"] is None
        if full:
            stats = mi.run(self.config)
            st["last_full"] = now
        else:
            stats = mi.run(self.config, after_rowid=st["cursor"],
                           window_start=int(now - self.watch.window_hours * 3600))
        st["cursor"], st["scope"] = stats.max_rowid, scope
        result = SyncResult("full" if full else "incremental", stats.added, stats.updated, stats.removed)
        if full:
            # Readers (the MCP's triage batch) cover the same chats that raise events.
            mi.set_meta(self.config.index, "notify_chats", json.dumps(self.notify.chats if self.notify else []))

        if self.notify is not None:
            # The first run indexes history; only messages that arrive after it notify.
            events = [] if baseline else new_message_events(self.config.index, stats.added_ids, self.notify, now)
            result.events = len(events)
            self._retry_pending(now, result)  # earlier failures go first
            if in_quiet_hours(self.notify.quiet, now):
                st["held"] = merge_held(st["held"], events)
            else:
                outgoing, st["held"] = st["held"] + events, []
                for event in outgoing:
                    if self._deliver(event):
                        result.delivered += 1
                    else:
                        st["pending"].append({"event": event, "first_try": now})
            if len(st["pending"]) > MAX_PENDING:
                result.dropped += len(st["pending"]) - MAX_PENDING
                st["pending"] = st["pending"][-MAX_PENDING:]
            result.held, result.pending = len(st["held"]), len(st["pending"])
        self._save_state()
        if result.changed():
            self.log(json.dumps({"at": _iso(now), **result.as_dict()}))
        return result

    def _retry_pending(self, now: float, result: SyncResult) -> None:
        """Resend failed HTTP deliveries (the outbox copy was written on the first try)."""
        keep = []
        for item in self.state["pending"]:
            if now - item["first_try"] > PENDING_TTL:
                result.dropped += 1
            elif self.notify.url and self.post(self.notify.url, self.notify.secret, item["event"]):
                result.delivered += 1
            else:
                keep.append(item)
        self.state["pending"] = keep

    def _store_signature(self) -> tuple:
        sig = []
        for suffix in ("", "-wal"):
            try:
                s = os.stat(f"{self.config.chat_db}{suffix}")
                sig.append((s.st_ino, s.st_size, s.st_mtime_ns))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def run(self, stop: threading.Event | None = None) -> None:
        """Watch until stop is set (or forever)."""
        stop = stop or threading.Event()
        store = self.config.chat_db
        files = FileWatch([store, Path(f"{store}-wal"), Path(f"{store}-shm"), store.parent])
        debounce = self.watch.debounce_ms / 1000
        backoff = 0.0
        try:
            self._safe_sync(force_full=True)
            seen = self._store_signature()
            next_poll = self.clock() + self.watch.poll_seconds
            while not stop.is_set():
                files.refresh()
                changed = files.wait(min(1.0, max(0.0, next_poll - self.clock())))
                if changed:
                    quiet_until = time.monotonic() + debounce
                    give_up = time.monotonic() + max(2.0, debounce * 8)
                    while time.monotonic() < min(quiet_until, give_up) and not stop.is_set():
                        if files.wait(min(quiet_until, give_up) - time.monotonic()):
                            quiet_until = time.monotonic() + debounce
                if stop.is_set():
                    break
                due = self.clock() >= next_poll
                if not changed and not due:
                    continue
                if backoff and not changed and self.clock() < backoff:
                    continue
                current = self._store_signature()
                if changed or current != seen or self.state["pending"] or self.state["held"] \
                        or self.clock() - self.state["last_full"] >= self.watch.full_every_minutes * 60:
                    backoff = 0.0 if self._safe_sync() else self.clock() + 60
                    seen = current
                next_poll = self.clock() + self.watch.poll_seconds
        finally:
            files.close()

    def _safe_sync(self, force_full: bool = False) -> bool:
        try:
            self.sync(force_full=force_full)
            return True
        except PermissionError:
            self.log(json.dumps({"at": _iso(self.clock()), "error": "permission denied: Full Disk Access needed"}))
        except (mi.LayoutError, mi.ConfigError) as exc:
            self.log(json.dumps({"at": _iso(self.clock()), "error": str(exc)}))
        except sqlite3.Error as exc:
            self.log(json.dumps({"at": _iso(self.clock()), "error": f"sqlite: {type(exc).__name__}"}))
        except OSError as exc:
            self.log(json.dumps({"at": _iso(self.clock()), "error": f"{type(exc).__name__}: {exc.strerror}"}))
        return False


def test_event(notify: NotifyConfig, now: float | None = None) -> dict:
    """A test event with no message data, for checking the wiring."""
    now = time.time() if now is None else now
    return {"type": TEST_EVENT_TYPE, "id": uuid.uuid4().hex, "created_at": _iso(now),
            "chat": {"id": "test", "name": "Test", "is_group": False}, "count": 0, "senders": [],
            "message_ids": [], "latest_at": None}
