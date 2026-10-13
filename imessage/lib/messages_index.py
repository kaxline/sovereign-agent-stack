"""Index a scoped slice of Messages.app history into a local SQLite file.

Reads the Messages database (chat.db) read-only and writes the chats named in
the config to an index that a read-only MCP can serve. Nothing is ever
written to the Messages folder: the store is opened with `mode=ro` and
`query_only`, each run reads inside one read transaction, and nothing
checkpoints its WAL. Reading chat.db needs Full Disk Access.

There is no default store path: the config must name it. run() and doctor()
return counts only, never message content. chats() lists chat ids and
participants for writing the config; its output is personal data, so only
the owner should run it. See README.md.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import pathname2url

import typedstream as ts

INDEX_SCHEMA_VERSION = 1
APPLE_EPOCH = 978307200
NS_THRESHOLD = 100_000_000_000  # dates above this are nanoseconds (macOS 10.13+), below are seconds
ATTACHMENT_PREFIX = "~/Library/Messages/Attachments/"
MENTION = "__kIMMentionConfirmedMention"
OBJECT_REPLACEMENT = "￼"

REACTION_KINDS = {0: "love", 1: "like", 2: "dislike", 3: "laugh", 4: "emphasize", 5: "question",
                  6: "emoji", 7: "sticker"}

REQUIRED = {
    "message": {"rowid", "guid", "date", "is_from_me", "handle_id", "text"},
    "chat": {"rowid", "guid", "chat_identifier"},
    "chat_message_join": {"chat_id", "message_id"},
    "handle": {"rowid", "id"},
}
OPTIONAL = {
    "message": {"attributedbody", "service", "is_read", "date_read", "date_edited", "date_retracted",
                "error", "item_type", "group_action_type", "other_handle", "group_title",
                "associated_message_guid", "associated_message_type", "associated_message_emoji",
                "thread_originator_guid", "reply_to_guid", "cache_has_attachments",
                "message_summary_info", "is_audio_message", "expressive_send_style_id"},
    "chat": {"display_name", "style", "service_name"},
    "chat_handle_join": {"chat_id", "handle_id"},
    "attachment": {"rowid", "filename", "mime_type", "transfer_name", "total_bytes"},
    "message_attachment_join": {"message_id", "attachment_id"},
    "chat_recoverable_message_join": {"chat_id", "message_id"},
}


class ConfigError(ValueError):
    """The config is missing, invalid or unsafe."""


class LayoutError(RuntimeError):
    """The Messages database doesn't look like one this reader understands."""


# --- Config ------------------------------------------------------------------------


@dataclass
class Config:
    store: Path  # the folder holding chat.db
    index: Path
    since: date
    chats: list[str]  # chat GUIDs or fnmatch patterns
    exclude: list[str] = field(default_factory=list)
    contacts: Path | None = None  # folder holding AddressBook-v22.abcddb files
    attachments: bool = False
    attachments_root: Path | None = None
    max_body_chars: int = 20_000

    @property
    def chat_db(self) -> Path:
        return self.store / "chat.db"

    def scope_fingerprint(self) -> str:
        scope = [str(self.since), sorted(self.chats), sorted(self.exclude), self.attachments,
                 self.max_body_chars, str(self.contacts or "")]
        return hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:16]


def _path(value, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"`{key}` is required and has no default")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from None
    base = path.resolve().parent
    unknown = set(raw) - {"store", "index", "since", "chats", "exclude", "contacts", "attachments",
                          "attachments_root", "max_body_chars",
                          "watch", "notify"}  # tables read by messages_watch.py
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    store = _path(raw.get("store"), "store", base)
    index = _path(raw.get("index"), "index", base)
    if index == store or store in index.parents:
        raise ConfigError("`index` must be outside the Messages folder")

    since = raw.get("since")
    if isinstance(since, str):
        try:
            since = date.fromisoformat(since)
        except ValueError:
            raise ConfigError("`since` must be a date like 2026-01-01") from None
    if not isinstance(since, date) or isinstance(since, datetime):
        raise ConfigError("`since` is required, e.g. since = 2026-01-01")

    def strings(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ConfigError(f"`{key}` must be a list of chat ids or patterns")
        return [v.strip() for v in value]

    chats = strings("chats")
    if not chats:
        raise ConfigError('list the chats to index in `chats` (ids from the `chats` command, or'
                          ' patterns such as "*;-;*" for every 1:1 chat); nothing is indexed by default')

    contacts = _path(raw["contacts"], "contacts", base) if raw.get("contacts") else None
    attachments = raw.get("attachments", False)
    if not isinstance(attachments, bool):
        raise ConfigError("`attachments` must be true or false")
    root = (_path(raw["attachments_root"], "attachments_root", base) if raw.get("attachments_root")
            else store / "Attachments")
    max_chars = raw.get("max_body_chars", 20_000)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ConfigError("`max_body_chars` must be a positive integer")
    return Config(store, index, since, chats, strings("exclude"), contacts, attachments, root, max_chars)


# --- Reading the store ------------------------------------------------------------------


def to_unix(value: int | None) -> int | None:
    """Messages' dates (ns or s since 2001) to Unix seconds. 0 and NULL mean unset."""
    if not value:
        return None
    seconds = value / 1_000_000_000 if abs(value) > NS_THRESHOLD else value
    return int(seconds) + APPLE_EPOCH


def since_bounds(day: date) -> tuple[int, int]:
    """The `since` day as Messages dates: (nanoseconds, seconds) since 2001."""
    unix = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return (unix - APPLE_EPOCH) * 1_000_000_000, unix - APPLE_EPOCH


def open_store(config: Config) -> sqlite3.Connection:
    """Open chat.db read-only. Raises PermissionError when macOS denies access.

    While Messages runs, chat.db has -wal and -shm files beside it, and a
    read-only connection reads through them. When neither exists (Messages
    closed and checkpointed), all data is in chat.db, and a plain read-only
    connection would create both files in the Messages folder; opening it
    as immutable avoids writing anything there."""
    path = config.chat_db
    try:
        with open(path, "rb"):
            pass
    except FileNotFoundError:
        raise ConfigError(f"no chat.db in {config.store}") from None
    quiet = not Path(f"{path}-wal").exists() and not Path(f"{path}-shm").exists()
    mode = "mode=ro&immutable=1" if quiet else "mode=ro"
    db = sqlite3.connect(f"file:{pathname2url(str(path))}?{mode}", uri=True, timeout=5)
    db.execute("PRAGMA query_only = ON")
    return db


def probe_columns(db: sqlite3.Connection) -> dict[str, set[str]]:
    tables = {r[0].lower() for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols: dict[str, set[str]] = {}
    for table in set(REQUIRED) | set(OPTIONAL):
        if table in tables:
            cols[table] = {r[1].lower() for r in db.execute(f'PRAGMA table_info("{table}")')} | {"rowid"}
    missing = [f"{t}.{c}" for t, need in REQUIRED.items() for c in sorted(need - cols.get(t, set()))]
    if missing:
        raise LayoutError("Messages database layout not recognised; missing " + ", ".join(missing))
    return cols


def _col(cols: dict[str, set[str]], table: str, alias: str, name: str) -> str:
    return f"{alias}.{name}" if name.lower() in cols.get(table, set()) else f"NULL AS {name}"


def _phone_key(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:
        return None
    return digits[-10:]


def handle_key(handle: str) -> str | None:
    return handle.strip().lower() if "@" in handle else _phone_key(handle)


def load_contacts(folder: Path | None) -> dict[str, str]:
    """Map handle keys (lowercased email, last ten digits of a phone) to display names."""
    names: dict[str, str] = {}
    if folder is None:
        return names
    for path in sorted(folder.rglob("AddressBook-v22.abcddb")):
        db = sqlite3.connect(f"file:{pathname2url(str(path))}?mode=ro", uri=True, timeout=5)
        try:
            people = {}
            for pk, first, last, nick, org in db.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD"):
                name = " ".join(p for p in (first, last) if p) or nick or org
                if name:
                    people[pk] = name
            for owner, number in db.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                key = _phone_key(number or "")
                if key and owner in people:
                    names.setdefault(key, people[owner])
            for owner, address in db.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if address and owner in people:
                    names.setdefault(address.strip().lower(), people[owner])
        except sqlite3.DatabaseError:
            pass  # not a Contacts database we understand; names stay unresolved
        finally:
            db.close()
    return names


@dataclass
class ChatRow:
    rowid: int
    guid: str
    identifier: str
    display_name: str | None
    service: str | None
    is_group: bool
    participants: list[str]


def read_chats(db: sqlite3.Connection, cols: dict[str, set[str]]) -> list[ChatRow]:
    rows = db.execute(
        f"SELECT c.ROWID, c.guid, c.chat_identifier, {_col(cols, 'chat', 'c', 'display_name')},"
        f" {_col(cols, 'chat', 'c', 'service_name')}, {_col(cols, 'chat', 'c', 'style')} FROM chat c"
    ).fetchall()
    members: dict[int, list[str]] = {}
    if "chat_handle_join" in cols:
        for chat_id, handle in db.execute(
                "SELECT j.chat_id, h.id FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id"
                " ORDER BY h.id"):
            members.setdefault(chat_id, []).append(handle)
    out = []
    for rowid, guid, identifier, name, service, style in rows:
        parts = members.get(rowid, [])
        group = style == 43 if style is not None else (";+;" in guid or len(parts) > 1)
        out.append(ChatRow(rowid, guid, identifier or "", name or None, service, group, parts))
    return out


def chat_selected(guid: str, config: Config) -> bool:
    if any(fnmatch.fnmatchcase(guid, p) for p in config.exclude):
        return False
    return any(fnmatch.fnmatchcase(guid, p) for p in config.chats)


def reaction_target(guid: str | None) -> str | None:
    """'p:0/GUID' or 'bp:GUID' to the target message's GUID."""
    if not guid:
        return None
    if guid.startswith("bp:"):
        return guid[3:]
    if guid.startswith("p:") and "/" in guid:
        return guid.split("/", 1)[1]
    return guid


def edit_count(summary: bytes | None) -> int:
    """Number of edits recorded in message_summary_info (best effort)."""
    if not summary:
        return 0
    try:
        info = plistlib.loads(summary)
        history = info.get("ec", {})
        return max((len(v) - 1 for v in history.values() if isinstance(v, list)), default=0)
    except Exception:
        return 0


# --- The index -------------------------------------------------------------------------

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chats (
    guid TEXT PRIMARY KEY,
    display_name TEXT,
    service TEXT,
    is_group INTEGER NOT NULL,
    participants TEXT NOT NULL          -- JSON list of {handle, name}
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,             -- stable id for the MCP
    guid TEXT NOT NULL UNIQUE,
    source_rowid INTEGER NOT NULL,      -- message.ROWID in chat.db
    chat_guid TEXT NOT NULL,
    date INTEGER NOT NULL,              -- Unix seconds
    sender TEXT NOT NULL,               -- handle, or "me"
    sender_name TEXT,
    is_from_me INTEGER NOT NULL,
    service TEXT,
    kind TEXT NOT NULL,                 -- message, event or unsent
    body TEXT,
    body_truncated INTEGER NOT NULL DEFAULT 0,
    is_read INTEGER,
    date_read INTEGER,
    edited INTEGER NOT NULL DEFAULT 0,
    edit_count INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    is_audio INTEGER NOT NULL DEFAULT 0,
    effect TEXT,
    thread_guid TEXT,
    reply_to_guid TEXT,
    mentions TEXT,                      -- JSON list of handles, or NULL
    attachment_count INTEGER NOT NULL DEFAULT 0,
    attachments TEXT,                   -- JSON list of {name, mime, size, on_disk}, or NULL
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_date ON messages(chat_guid, date);
CREATE INDEX IF NOT EXISTS messages_date ON messages(date);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_guid);
CREATE TABLE IF NOT EXISTS reactions (
    guid TEXT PRIMARY KEY,
    target_guid TEXT NOT NULL,
    chat_guid TEXT NOT NULL,
    date INTEGER NOT NULL,
    sender TEXT NOT NULL,
    sender_name TEXT,
    is_from_me INTEGER NOT NULL,
    kind TEXT NOT NULL,
    emoji TEXT,
    removed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reactions_target ON reactions(target_guid);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body, sender_text, chat_text, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass
    new = not path.exists()
    # VACUUM writes a scratch copy of the index; keep it in the index's own 0700
    # folder rather than the system temp folder.
    os.environ["SQLITE_TMPDIR"] = str(path.parent)
    db = sqlite3.connect(path)
    if new:
        os.chmod(path, 0o600)
    # Overwrite deleted rows (unsent messages, rows that left the scope) with
    # zeros instead of leaving their text in free pages.
    db.execute("PRAGMA secure_delete = ON")
    db.executescript(INDEX_SCHEMA)
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version and int(version[0]) != INDEX_SCHEMA_VERSION:
        db.close()
        raise LayoutError(f"index schema {version[0]} is not {INDEX_SCHEMA_VERSION}; delete {path} and re-run")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(INDEX_SCHEMA_VERSION),))
    return db


def fts_secure_delete(db: sqlite3.Connection) -> bool:
    """Make FTS5 drop a deleted message's words from the search index at once
    (SQLite 3.42+). On older SQLite, compact() merges the index instead."""
    try:
        db.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('secure-delete', 1)")
        return True
    except sqlite3.OperationalError:
        return False


def compact(db: sqlite3.Connection, *, fts_secure: bool) -> None:
    """Merge the full-text index and rewrite the file without free pages. Day to
    day, secure delete already overwrites text as it goes; this runs only when
    that isn't enough (see run()). It can't reach copies an SSD or a backup
    already holds; FileVault and backup exclusions cover those."""
    if not fts_secure:
        db.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
        db.commit()
    db.execute("VACUUM")


COMMIT_EVERY = 500  # changed rows between commits, so an interrupted first run can resume


@dataclass
class RunStats:
    chats_in_scope: int = 0
    in_scope: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    reactions: int = 0
    events: int = 0
    unsent: int = 0
    edited: int = 0
    recently_deleted_skipped: int = 0
    attributed_body_decoded: int = 0
    attributes_incomplete: int = 0
    decode_failed_used_text: int = 0
    empty_bodies: int = 0
    max_rowid: int = 0  # highest message ROWID in the store when the run started
    added_ids: list[int] = field(default_factory=list)  # index ids of rows added (not printed)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "added_ids"}


def _message_select(cols: dict[str, set[str]], incremental: bool = False) -> str:
    """Rows of one chat. Blobs are left out (only their lengths), so unchanged
    rows cost little; _blobs() fetches them for rows that need decoding."""
    m = lambda name: _col(cols, "message", "m", name)  # noqa: E731
    length = lambda name: (f"length(m.{name}) AS {name}_len" if name.lower() in cols["message"]  # noqa: E731
                           else f"NULL AS {name}_len")
    fields = ["m.ROWID", "m.guid", "m.date", "m.is_from_me", "m.handle_id", "m.text",
              length("attributedBody"), m("service"), m("is_read"), m("date_read"), m("date_edited"),
              m("date_retracted"), m("error"), m("item_type"), m("group_action_type"),
              m("other_handle"), m("group_title"), m("associated_message_guid"),
              m("associated_message_type"), m("associated_message_emoji"),
              m("thread_originator_guid"), m("reply_to_guid"), m("cache_has_attachments"),
              length("message_summary_info"), m("is_audio_message"), m("expressive_send_style_id")]
    recoverable = ("EXISTS (SELECT 1 FROM chat_recoverable_message_join r WHERE r.message_id = m.ROWID)"
                   if "chat_recoverable_message_join" in cols else "0")
    # Incremental runs (the watcher) read only rows added since a ROWID cursor
    # plus a recent window by date, where edits, unsends and late joins happen.
    recent = " AND (m.ROWID > ? OR m.date >= ? OR (m.date < ? AND m.date >= ?))" if incremental else ""
    return (f"SELECT {', '.join(fields)}, j.chat_id, {recoverable} AS recently_deleted"
            " FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID"
            f" WHERE j.chat_id = ? AND (m.date >= ? OR (m.date < ? AND m.date >= ?)){recent}"
            " ORDER BY m.ROWID")


COLUMNS = ["rowid", "guid", "date", "is_from_me", "handle_id", "text", "attributedBody_len", "service",
           "is_read", "date_read", "date_edited", "date_retracted", "error", "item_type",
           "group_action_type", "other_handle", "group_title", "associated_message_guid",
           "associated_message_type", "associated_message_emoji", "thread_originator_guid",
           "reply_to_guid", "cache_has_attachments", "message_summary_info_len", "is_audio_message",
           "expressive_send_style_id", "chat_id", "recently_deleted"]


def _blobs(db: sqlite3.Connection, cols: dict[str, set[str]], rowid: int) -> tuple[bytes | None, bytes | None]:
    """attributedBody and message_summary_info for one row."""
    row = db.execute(f"SELECT {_col(cols, 'message', 'm', 'attributedBody')},"
                     f" {_col(cols, 'message', 'm', 'message_summary_info')} FROM message m WHERE m.ROWID = ?",
                     (rowid,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _fingerprint(r: dict, sender_name: str | None, scope: str) -> str:
    parts = [r["date_edited"], r["date_retracted"], r["is_read"], r["date_read"], r["error"],
             r["attributedBody_len"] or 0, len(r["text"] or ""), r["item_type"],
             r["associated_message_type"], sender_name, r["cache_has_attachments"], scope]
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:20]


def _event_text(r: dict, actor: str, other: str | None) -> str:
    item, action = r["item_type"] or 0, r["group_action_type"] or 0
    if item == 1:
        if other is None:
            return f"{actor} changed who is in the conversation"
        return f"{actor} {'added' if action == 0 else 'removed'} {other}"
    if item == 2:
        return f"{actor} named the conversation “{r['group_title'] or ''}”"
    if item == 3:
        return f"{actor} left the conversation" if action == 0 else f"{actor} changed the group photo"
    return f"conversation event (type {item})"


def _attachments(db: sqlite3.Connection, cols: dict[str, set[str]], rowid: int,
                 config: Config) -> tuple[int, list[dict] | None]:
    if "attachment" not in cols or "message_attachment_join" not in cols:
        return 0, None
    rows = db.execute(
        f"SELECT {_col(cols, 'attachment', 'a', 'transfer_name')}, {_col(cols, 'attachment', 'a', 'mime_type')},"
        f" {_col(cols, 'attachment', 'a', 'total_bytes')}, {_col(cols, 'attachment', 'a', 'filename')}"
        " FROM attachment a JOIN message_attachment_join j ON j.attachment_id = a.ROWID"
        " WHERE j.message_id = ? ORDER BY a.ROWID", (rowid,)).fetchall()
    if not config.attachments:
        return len(rows), None
    out = []
    for name, mime, size, filename in rows:
        out.append({"name": name or (Path(filename).name if filename else None), "mime": mime,
                    "size": size, "on_disk": attachment_on_disk(filename, config)})
    return len(rows), out


def attachment_on_disk(filename: str | None, config: Config) -> bool:
    """Whether the attachment file exists, mapping Messages' ~ prefix onto the
    configured attachment root. Never expands ~ (that would be the real store)."""
    if not filename or config.attachments_root is None:
        return False
    if filename.startswith(ATTACHMENT_PREFIX):
        rel = filename[len(ATTACHMENT_PREFIX):]
    elif filename.startswith(str(config.attachments_root) + "/"):
        rel = filename[len(str(config.attachments_root)) + 1:]
    else:
        return False
    target = (config.attachments_root / rel).resolve()
    return config.attachments_root in target.parents and target.is_file()


def run(config: Config, *, after_rowid: int | None = None, window_start: int | None = None) -> RunStats:
    """Build or update the index. Returns counts only.

    With after_rowid and window_start (Unix seconds) the run is incremental,
    as the watcher uses it: it reads rows added after that ROWID plus rows
    dated on or after window_start, and removes only indexed rows from that
    window. A full run (the default) reads and reconciles the whole scope."""
    incremental = after_rowid is not None
    if incremental and window_start is None:
        raise ValueError("an incremental run needs window_start")
    stats = RunStats()
    names = load_contacts(config.contacts)
    src = open_store(config)
    try:
        src.execute("BEGIN")  # one read transaction: a consistent snapshot, released at the end
        cols = probe_columns(src)
        handles = {rowid: hid for rowid, hid in src.execute("SELECT ROWID, id FROM handle")}
        chats = [c for c in read_chats(src, cols) if chat_selected(c.guid, config)]
        stats.chats_in_scope = len(chats)
        since_ns, since_s = since_bounds(config.since)
        scope = config.scope_fingerprint()
        select = _message_select(cols, incremental)
        stats.max_rowid = src.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()[0]
        extra: tuple = ()
        if incremental:
            win_ns = (window_start - APPLE_EPOCH) * 1_000_000_000
            extra = (after_rowid, win_ns, NS_THRESHOLD, window_start - APPLE_EPOCH)

        def name_of(handle: str | None) -> str | None:
            return names.get(handle_key(handle)) if handle else None

        idx = open_index(config.index)
        try:
            fts_secure = fts_secure_delete(idx)
            # Indexes written before secure delete was on may hold old text in free pages.
            first_scrub = idx.execute("SELECT 1 FROM meta WHERE key = 'scrubbed'").fetchone() is None
            changes = 0
            existing = {g: (i, fp, d, rid) for i, g, fp, d, rid in
                        idx.execute("SELECT id, guid, fingerprint, date, source_rowid FROM messages")}
            seen_messages: set[str] = set()
            seen_reactions: set[str] = set()
            idx.execute("DELETE FROM chats")
            for chat in chats:
                participants = [{"handle": h, "name": name_of(h)} for h in chat.participants]
                idx.execute("INSERT INTO chats VALUES (?,?,?,?,?)",
                            (chat.guid, chat.display_name, chat.service, int(chat.is_group),
                             json.dumps(participants, ensure_ascii=False)))
                chat_text = " ".join(filter(None, [chat.display_name] +
                                            [p["name"] or p["handle"] for p in participants]))
                if "chat_recoverable_message_join" in cols and not incremental:
                    # Recently Deleted rows usually lose their chat_message_join row, so the
                    # query below never sees them; count them here.
                    stats.recently_deleted_skipped += src.execute(
                        "SELECT COUNT(*) FROM chat_recoverable_message_join r JOIN message m ON m.ROWID = r.message_id"
                        " WHERE r.chat_id = ? AND NOT EXISTS (SELECT 1 FROM chat_message_join j"
                        " WHERE j.message_id = m.ROWID) AND (m.date >= ? OR (m.date < ? AND m.date >= ?))",
                        (chat.rowid, since_ns, NS_THRESHOLD, since_s)).fetchone()[0]
                for raw in src.execute(select, (chat.rowid, since_ns, NS_THRESHOLD, since_s) + extra).fetchall():
                    r = dict(zip(COLUMNS, raw))
                    if r["guid"] in seen_messages or r["guid"] in seen_reactions:
                        continue  # the same message joined to two chats
                    if r["recently_deleted"]:
                        stats.recently_deleted_skipped += 1
                        continue
                    from_me = bool(r["is_from_me"])
                    sender = "me" if from_me else (handles.get(r["handle_id"]) or "unknown")
                    sender_name = None if from_me else name_of(sender)
                    assoc = r["associated_message_type"] or 0
                    if 2000 <= assoc < 4000:
                        seen_reactions.add(r["guid"])
                        stats.reactions += 1
                        idx.execute(
                            "INSERT OR REPLACE INTO reactions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (r["guid"], reaction_target(r["associated_message_guid"]) or "", chat.guid,
                             to_unix(r["date"]), sender, sender_name, int(from_me),
                             REACTION_KINDS.get(assoc % 1000, f"type {assoc % 1000}"),
                             r["associated_message_emoji"], int(assoc >= 3000)))
                        continue
                    stats.in_scope += 1
                    seen_messages.add(r["guid"])
                    fingerprint = _fingerprint(r, sender_name, scope)
                    known = existing.get(r["guid"])
                    if known and known[1] == fingerprint:
                        continue
                    row = _build_row(src, cols, r, chat, sender, sender_name, handles, name_of, config, stats)
                    values = (r["guid"], r["rowid"], chat.guid, to_unix(r["date"]) or 0, sender, sender_name,
                              int(from_me), r["service"] or chat.service, row["kind"], row["body"],
                              row["truncated"], r["is_read"], to_unix(r["date_read"]), row["edited"],
                              row["edit_count"], int(bool(r["error"])), int(bool(r["is_audio_message"])),
                              r["expressive_send_style_id"], r["thread_originator_guid"], r["reply_to_guid"],
                              row["mentions"], row["attachment_count"], row["attachments"], fingerprint)
                    if known:
                        idx.execute(
                            "UPDATE messages SET guid=?, source_rowid=?, chat_guid=?, date=?, sender=?,"
                            " sender_name=?, is_from_me=?, service=?, kind=?, body=?, body_truncated=?,"
                            " is_read=?, date_read=?, edited=?, edit_count=?, failed=?, is_audio=?, effect=?,"
                            " thread_guid=?, reply_to_guid=?, mentions=?, attachment_count=?, attachments=?,"
                            " fingerprint=? WHERE id=?", values + (known[0],))
                        message_id = known[0]
                        idx.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
                        stats.updated += 1
                    else:
                        message_id = idx.execute(
                            "INSERT INTO messages (guid, source_rowid, chat_guid, date, sender, sender_name,"
                            " is_from_me, service, kind, body, body_truncated, is_read, date_read, edited,"
                            " edit_count, failed, is_audio, effect, thread_guid, reply_to_guid, mentions,"
                            " attachment_count, attachments, fingerprint)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values).lastrowid
                        stats.added += 1
                        stats.added_ids.append(message_id)
                    idx.execute("INSERT INTO messages_fts (rowid, body, sender_text, chat_text) VALUES (?,?,?,?)",
                                (message_id, row["body"] or "",
                                 "me" if from_me else " ".join(filter(None, [sender_name, sender])), chat_text))
                    changes += 1
                    if changes % COMMIT_EVERY == 0:
                        idx.commit()
            for guid, (message_id, _, msg_date, source_rowid) in existing.items():
                if guid in seen_messages:
                    continue
                # An incremental run only saw new rows and the recent window.
                if not incremental or msg_date >= window_start or source_rowid > after_rowid:
                    idx.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                    idx.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
                    stats.removed += 1
            stale = [g for g, d in idx.execute("SELECT guid, date FROM reactions")
                     if g not in seen_reactions and (not incremental or d >= window_start)]
            idx.executemany("DELETE FROM reactions WHERE guid = ?", [(g,) for g in stale])
            idx.execute("INSERT OR REPLACE INTO meta VALUES ('scope', ?)", (scope,))
            idx.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
            idx.execute("INSERT OR REPLACE INTO meta VALUES ('attachments_listed', ?)", (str(int(config.attachments)),))
            idx.commit()
            erased = bool(stats.removed or stats.updated)
            free, pages = (idx.execute(f"PRAGMA {p}").fetchone()[0] for p in ("freelist_count", "page_count"))
            # Without FTS5 secure-delete, removed words stay in the search index until
            # it's merged. Past a quarter free, give the space back.
            if first_scrub or (erased and not fts_secure) or (pages and free / pages > 0.25):
                compact(idx, fts_secure=fts_secure)
                idx.execute("INSERT OR REPLACE INTO meta VALUES ('scrubbed', ?)",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
                idx.commit()
        finally:
            idx.close()
    finally:
        src.rollback()
        src.close()
    return stats


def _build_row(src, cols, r: dict, chat: ChatRow, sender: str, sender_name: str | None, handles,
               name_of, config: Config, stats: RunStats) -> dict:
    row = {"kind": "message", "body": None, "truncated": 0, "edited": 0, "edit_count": 0,
           "mentions": None, "attachment_count": 0, "attachments": None}
    actor = "You" if r["is_from_me"] else (sender_name or sender)
    if r["item_type"]:
        other = handles.get(r["other_handle"]) if r["other_handle"] else None
        row["kind"] = "event"
        row["body"] = _event_text(r, actor, (name_of(other) or other) if other else None)
        stats.events += 1
        return row
    if r["date_retracted"]:
        row["kind"] = "unsent"
        stats.unsent += 1
        return row
    text = None
    blob, summary = (_blobs(src, cols, r["rowid"]) if r["attributedBody_len"] or r["message_summary_info_len"]
                     else (None, None))
    if blob:
        try:
            body = ts.decode_body(blob)
            text = body.text
            stats.attributed_body_decoded += 1
            if not body.complete:
                stats.attributes_incomplete += 1
            mentions = [m for m in body.attribute_values(MENTION) if isinstance(m, str)]
            if mentions:
                row["mentions"] = json.dumps(sorted(set(mentions)))
        except ts.UnsupportedArchive:
            if r["text"]:
                stats.decode_failed_used_text += 1
    if text is None:
        text = r["text"]
    if text is not None:
        text = text.replace(OBJECT_REPLACEMENT, "").strip()
        if len(text) > config.max_body_chars:
            text, row["truncated"] = text[:config.max_body_chars], 1
    if r["cache_has_attachments"]:
        row["attachment_count"], listed = _attachments(src, cols, r["rowid"], config)
        if listed is not None:
            row["attachments"] = json.dumps(listed, ensure_ascii=False)
    if not text and not row["attachment_count"]:
        stats.empty_bodies += 1
    row["body"] = text or None
    if r["date_edited"]:
        row["edited"], row["edit_count"] = 1, max(1, edit_count(summary))
        stats.edited += 1
    return row


# --- Doctor and chat listing ----------------------------------------------------------


def doctor(config: Config) -> dict:
    """Check the store and the scope. Counts and layout facts only."""
    src = open_store(config)
    try:
        src.execute("BEGIN")
        cols = probe_columns(src)
        with open(config.chat_db, "rb") as f:
            header = f.read(20)  # bytes 18-19 are the file format versions; 2 means WAL
        report: dict = {"store": str(config.chat_db),
                        "journal_mode": "wal" if header[18:20] == b"\x02\x02" else "rollback",
                        "wal_file_present": Path(f"{config.chat_db}-wal").exists()}
        report["optional_columns_missing"] = sorted(
            f"{t}.{c}" for t, want in OPTIONAL.items() if t in cols for c in want - cols[t])
        report["optional_tables_missing"] = sorted(t for t in OPTIONAL if t not in cols)
        max_date = src.execute("SELECT MAX(date) FROM message").fetchone()[0] or 0
        report["dates_look_like_nanoseconds"] = max_date > NS_THRESHOLD
        report["messages_total"] = src.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        report["messages_without_chat"] = src.execute(
            "SELECT COUNT(*) FROM message m WHERE NOT EXISTS"
            " (SELECT 1 FROM chat_message_join j WHERE j.message_id = m.ROWID)").fetchone()[0]
        chats = read_chats(src, cols)
        selected = [c for c in chats if chat_selected(c.guid, config)]
        report["chats_total"] = len(chats)
        report["chats_in_scope"] = len(selected)
        since_ns, since_s = since_bounds(config.since)
        select = _message_select(cols)
        in_scope = attributed_only = decoded = failed = 0
        for chat in selected:
            for raw in src.execute(select, (chat.rowid, since_ns, NS_THRESHOLD, since_s)):
                r = dict(zip(COLUMNS, raw))
                in_scope += 1
                if r["attributedBody_len"]:
                    if not r["text"]:
                        attributed_only += 1
                    try:
                        ts.decode_body(_blobs(src, cols, r["rowid"])[0] or b"")
                        decoded += 1
                    except ts.UnsupportedArchive:
                        failed += 1
        report.update(messages_in_scope=in_scope, attributed_body_only=attributed_only,
                      attributed_body_decoded=decoded, attributed_body_failed=failed)
    finally:
        src.rollback()
        src.close()
    names = load_contacts(config.contacts)
    report["contacts_configured"] = config.contacts is not None
    report["contact_handles"] = len(names)
    return report


def list_chats(config: Config) -> list[dict]:
    """Chat ids, names, participants and message counts, for writing the config.
    This is personal data (who you talk to): print it for the owner only."""
    names = load_contacts(config.contacts)
    src = open_store(config)
    try:
        src.execute("BEGIN")
        cols = probe_columns(src)
        counts = dict(src.execute("SELECT chat_id, COUNT(*) FROM chat_message_join GROUP BY chat_id"))
        out = []
        for chat in read_chats(src, cols):
            out.append({"id": chat.guid, "name": chat.display_name, "group": chat.is_group,
                        "participants": [names.get(handle_key(h)) or h for h in chat.participants],
                        "messages": counts.get(chat.rowid, 0),
                        "in_scope": chat_selected(chat.guid, config)})
    finally:
        src.rollback()
        src.close()
    return sorted(out, key=lambda c: -c["messages"])
