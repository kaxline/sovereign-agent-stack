"""Index a scoped slice of an Apple Mail store into a local SQLite file.

The indexer reads Mail's metadata database from a snapshot copy, keeps only
the accounts, mailboxes and date window named in the config, reads message
headers (and bodies, if enabled) from the message files, and writes a
standalone index with full-text search. It never writes to the store.

Nothing here has a default store path: the config must name it. Output from
run() is counts only, never message content. See docs/email.md.
"""

from __future__ import annotations

import email
import email.policy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

INDEX_SCHEMA_VERSION = 1
META_DB = "Envelope Index"
MESSAGE_SUFFIXES = (".partial.emlx", ".emlx")
# Left out of a "*" mailbox pattern; list them by name to include them.
EXCLUDED_BY_DEFAULT = {"junk", "junk e-mail", "junk email", "spam", "bulk mail",
                       "trash", "deleted messages", "deleted items", "bin"}

FLAG_READ = 1 << 0
FLAG_DELETED = 1 << 1
FLAG_FLAGGED = 1 << 4
ATTACHMENT_COUNT_SHIFT = 10


class ConfigError(ValueError):
    pass


class LayoutError(RuntimeError):
    """The store doesn't look like a Mail store this indexer understands."""


# --------------------------------------------------------------------------- config

@dataclass
class AccountScope:
    id: str
    mailboxes: list[str]


@dataclass
class Config:
    store: Path
    index: Path
    since: date
    accounts: list[AccountScope]
    bodies: bool = False
    attachments: bool = False
    max_body_chars: int = 100_000

    def scope_fingerprint(self) -> str:
        data = {"store": str(self.store), "since": self.since.isoformat(),
                "accounts": [[a.id.upper(), sorted(a.mailboxes)] for a in self.accounts]}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _path(value, key: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"`{key}` is required and must be a path")
    p = Path(os.path.expandvars(value)).expanduser()
    return (base / p).resolve() if not p.is_absolute() else p.resolve()


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from None
    base = path.resolve().parent
    unknown = set(raw) - {"store", "index", "since", "bodies", "attachments",
                          "max_body_chars", "accounts"}
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    store = _path(raw.get("store"), "store", base)
    index = _path(raw.get("index"), "index", base)
    if index == store or store in index.parents:
        raise ConfigError("`index` must be outside the Mail store")

    since = raw.get("since")
    if isinstance(since, str):
        try:
            since = date.fromisoformat(since)
        except ValueError:
            raise ConfigError("`since` must be a date like 2026-01-01") from None
    if not isinstance(since, date) or isinstance(since, datetime):
        raise ConfigError("`since` is required, e.g. since = 2026-01-01")

    accounts = []
    for i, item in enumerate(raw.get("accounts") or []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ConfigError(f"accounts[{i}] needs an `id`")
        boxes = item.get("mailboxes")
        if not boxes or not all(isinstance(b, str) and b for b in boxes):
            raise ConfigError(f"accounts[{i}] needs a non-empty `mailboxes` list")
        accounts.append(AccountScope(item["id"].strip(), list(boxes)))
    if not accounts:
        raise ConfigError("list at least one [[accounts]] entry; nothing is indexed by default")

    def flag(key: str) -> bool:
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise ConfigError(f"`{key}` must be true or false")
        return value

    max_chars = raw.get("max_body_chars", 100_000)
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ConfigError("`max_body_chars` must be a positive integer")
    return Config(store, index, since, accounts, flag("bodies"), flag("attachments"), max_chars)


# --------------------------------------------------------------------------- store layout

def find_version_dir(store: Path) -> Path:
    """The V<n> folder holding MailData, or store itself if it is one."""
    if (store / "MailData" / META_DB).is_file():
        return store
    candidates = []
    for child in store.iterdir() if store.is_dir() else []:
        m = re.fullmatch(r"V(\d+)", child.name)
        if m and (child / "MailData" / META_DB).is_file():
            candidates.append((int(m.group(1)), child))
    if not candidates:
        raise LayoutError(f"no V<n>/MailData metadata database under {store}")
    return max(candidates)[1]


def parse_mailbox_url(url: str) -> tuple[str, str] | None:
    parts = urlsplit(url)
    if not parts.netloc:
        return None
    return parts.netloc, unquote(parts.path.lstrip("/"))


def mailbox_selected(path: str, patterns: list[str]) -> bool:
    lowered = path.lower()
    leaf = lowered.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.lower() == lowered:
            return True
    if leaf in EXCLUDED_BY_DEFAULT:
        return False
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


def message_files(version_dir: Path, account: str, mailbox: str) -> dict[int, Path]:
    """rowid -> message file for one mailbox, not descending into child mailboxes."""
    box = version_dir / account
    for part in mailbox.split("/"):
        box = box / f"{part}.mbox"
    found: dict[int, Path] = {}
    if not box.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(box):
        dirnames[:] = [d for d in dirnames if not d.endswith(".mbox")]
        for name in filenames:
            for suffix in MESSAGE_SUFFIXES:
                if name.endswith(suffix):
                    stem = name[: -len(suffix)]
                    if stem.isdigit():
                        rowid = int(stem)
                        # A full copy wins over a partial one.
                        if rowid not in found or suffix == ".emlx":
                            found[rowid] = Path(dirpath, name)
                    break
    return found


# --------------------------------------------------------------------------- metadata snapshot

REQUIRED = {
    "messages": {"rowid", "mailbox", "date_received"},
    "mailboxes": {"rowid", "url"},
}
OPTIONAL = {
    "messages": {"subject", "subject_prefix", "sender", "date_sent", "flags", "read",
                 "flagged", "deleted", "conversation_id"},
    "subjects": {"rowid", "subject"},
    "addresses": {"rowid", "address", "comment"},
}


@dataclass
class MetaRow:
    rowid: int
    account: str
    mailbox: str
    date_received: int
    date_sent: int | None
    subject: str | None
    sender: str | None
    sender_name: str | None
    flags: int
    read: bool
    flagged: bool
    deleted: bool
    conversation_id: int | None


def snapshot_metadata(version_dir: Path, dest: Path) -> Path:
    """Copy the metadata database (and its WAL) so Mail's live file is never opened."""
    src = version_dir / "MailData" / META_DB
    out = dest / "meta.sqlite"
    shutil.copyfile(src, out)
    for suffix in ("-wal", "-shm"):
        side = Path(f"{src}{suffix}")
        if side.exists():
            shutil.copyfile(side, Path(f"{out}{suffix}"))
    return out


def probe_columns(db: sqlite3.Connection) -> dict[str, set[str]]:
    tables = {r[0].lower() for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols: dict[str, set[str]] = {}
    for table in set(REQUIRED) | set(OPTIONAL):
        if table in tables:
            names = {r[1].lower() for r in db.execute(f'PRAGMA table_info("{table}")')}
            cols[table] = names | {"rowid"}
    missing = [f"{t}.{c}" for t, need in REQUIRED.items() for c in sorted(need - cols.get(t, set()))]
    if missing:
        raise LayoutError("metadata database layout not recognised; missing " + ", ".join(missing))
    return cols


def read_metadata(meta_path: Path, config: Config) -> tuple[list[MetaRow], dict]:
    db = sqlite3.connect(meta_path)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LayoutError("metadata snapshot failed an integrity check; try again")
        cols = probe_columns(db)
        m = cols["messages"]

        def col(name: str, expr: str | None = None) -> str:
            return expr or f"m.{name}" if name in m else "NULL"

        select = [
            "m.ROWID", "mb.url", "m.date_received", col("date_sent"),
            col("flags"), col("read"), col("flagged"), col("deleted"), col("conversation_id"),
        ]
        joins = ["JOIN mailboxes mb ON mb.ROWID = m.mailbox"]
        if "subject" in m and "subjects" in cols:
            prefix = "COALESCE(m.subject_prefix, '') || " if "subject_prefix" in m else ""
            select.append(f"{prefix}s.subject")
            joins.append("LEFT JOIN subjects s ON s.ROWID = m.subject")
        else:
            select.append("NULL")
        if "sender" in m and "addresses" in cols:
            comment = "a.comment" if "comment" in cols["addresses"] else "NULL"
            select += ["a.address", comment]
            joins.append("LEFT JOIN addresses a ON a.ROWID = m.sender")
        else:
            select += ["NULL", "NULL"]

        since = int(datetime.combine(config.since, datetime.min.time(), timezone.utc).timestamp())
        sql = (f"SELECT {', '.join(select)} FROM messages m {' '.join(joins)}"
               " WHERE m.date_received >= ?")
        wanted = {a.id.upper(): a.mailboxes for a in config.accounts}
        rows: list[MetaRow] = []
        seen_accounts: set[str] = set()
        for (rowid, url, received, sent, flags, read, flagged, deleted, conv,
             subject, sender, sender_name) in db.execute(sql, (since,)):
            parsed = parse_mailbox_url(url or "")
            if not parsed:
                continue
            account, mailbox = parsed
            seen_accounts.add(account.upper())
            patterns = wanted.get(account.upper())
            if not patterns or not mailbox_selected(mailbox, patterns):
                continue
            flags = int(flags or 0)
            is_deleted = bool(deleted) if deleted is not None else bool(flags & FLAG_DELETED)
            if is_deleted:
                continue
            rows.append(MetaRow(
                rowid=int(rowid), account=account, mailbox=mailbox, date_received=int(received),
                date_sent=int(sent) if sent is not None else None,
                subject=subject, sender=sender, sender_name=sender_name, flags=flags,
                read=bool(read) if read is not None else bool(flags & FLAG_READ),
                flagged=bool(flagged) if flagged is not None else bool(flags & FLAG_FLAGGED),
                deleted=False, conversation_id=int(conv) if conv is not None else None,
            ))
        info = {"accounts_missing": sorted(set(wanted) - seen_accounts)}
        return rows, info
    finally:
        db.close()


# --------------------------------------------------------------------------- message files

class _TextFromHTML(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextFromHTML()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


@dataclass
class ParsedMessage:
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    from_addr: str | None
    from_name: str | None
    to: list[str]
    cc: list[str]
    subject: str | None
    body: str | None
    attachments: list[dict] = field(default_factory=list)


def read_message_file(path: Path) -> EmailMessage:
    data = path.read_bytes()
    first, _, rest = data.partition(b"\n")
    length = int(first.strip())
    if length > len(rest):
        raise ValueError("byte count larger than file")
    return email.message_from_bytes(rest[:length], policy=email.policy.default)


def _addresses(msg: EmailMessage, header: str) -> list[tuple[str, str]]:
    try:
        value = msg[header]
    except Exception:
        return []
    if value is None:
        return []
    try:
        return [(a.addr_spec, a.display_name) for a in value.addresses]
    except Exception:
        return []


def _ids(value) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def parse_message(path: Path, *, bodies: bool, attachments: bool, max_chars: int) -> ParsedMessage:
    msg = read_message_file(path)
    sender = _addresses(msg, "From")
    body = None
    if bodies:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            try:
                content = part.get_content()
            except Exception:
                content = ""
            if part.get_content_subtype() == "html":
                content = html_to_text(content)
            body = content.strip()[:max_chars]
    atts = []
    if attachments:
        for part in msg.iter_attachments():
            size = part.get("X-Apple-Content-Length")
            if size is None:
                try:
                    size = len(part.get_payload(decode=True) or b"")
                except Exception:
                    size = None
            atts.append({"name": part.get_filename(), "mime": part.get_content_type(),
                         "size": int(size) if size is not None else None})
    ids = _ids(msg.get("Message-ID"))
    return ParsedMessage(
        message_id=ids[0] if ids else None,
        in_reply_to=(_ids(msg.get("In-Reply-To")) or [None])[0],
        references=_ids(msg.get("References")),
        from_addr=sender[0][0] if sender else None,
        from_name=sender[0][1] if sender else None,
        to=[a for a, _ in _addresses(msg, "To")],
        cc=[a for a, _ in _addresses(msg, "Cc")],
        subject=str(msg["Subject"]) if msg["Subject"] is not None else None,
        body=body, attachments=atts,
    )


# --------------------------------------------------------------------------- index

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    rowid INTEGER PRIMARY KEY,          -- the store's message ROWID
    account TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    message_id TEXT,
    in_reply_to TEXT,
    refs TEXT,                          -- JSON list
    thread_id TEXT,
    date_received INTEGER NOT NULL,
    date_sent INTEGER,
    from_addr TEXT,
    from_name TEXT,
    to_addrs TEXT,                      -- JSON list
    cc_addrs TEXT,                      -- JSON list
    subject TEXT,
    flags INTEGER NOT NULL,
    is_read INTEGER NOT NULL,
    is_flagged INTEGER NOT NULL,
    body TEXT,
    body_indexed INTEGER NOT NULL DEFAULT 0,
    attachments TEXT                    -- JSON list of {name, mime, size}, or NULL
);
CREATE INDEX IF NOT EXISTS messages_date ON messages(date_received);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS messages_message_id ON messages(message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, from_text, to_text, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""


@dataclass
class RunStats:
    in_scope: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    missing_files: int = 0
    unreadable: int = 0
    accounts_missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def open_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass
    new = not path.exists()
    db = sqlite3.connect(path)
    if new:
        os.chmod(path, 0o600)
    db.executescript(INDEX_SCHEMA)
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if version and int(version[0]) != INDEX_SCHEMA_VERSION:
        db.close()
        raise LayoutError(f"index schema {version[0]} is not {INDEX_SCHEMA_VERSION}; delete {path} and re-run")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(INDEX_SCHEMA_VERSION),))
    return db


def _fts_put(db: sqlite3.Connection, rowid: int, subject, from_addr, from_name, to, cc, body) -> None:
    db.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
    db.execute(
        "INSERT INTO messages_fts (rowid, subject, from_text, to_text, body) VALUES (?,?,?,?,?)",
        (rowid, subject or "", " ".join(x for x in (from_name, from_addr) if x),
         " ".join(to + cc), body or ""),
    )


def _thread_id(row: MetaRow, parsed: ParsedMessage | None) -> str | None:
    if row.conversation_id is not None:
        return f"c{row.conversation_id}"
    if parsed and (parsed.references or parsed.in_reply_to or parsed.message_id):
        root = (parsed.references or [parsed.in_reply_to or parsed.message_id])[0]
        return "m" + hashlib.sha1(root.encode()).hexdigest()[:16]
    return None


def run(config: Config, *, now: float | None = None) -> RunStats:
    stats = RunStats()
    version_dir = find_version_dir(config.store)
    with tempfile.TemporaryDirectory(prefix="mail-index-") as tmp:
        os.chmod(tmp, 0o700)
        meta_path = snapshot_metadata(version_dir, Path(tmp))
        rows, info = read_metadata(meta_path, config)
    stats.accounts_missing = info["accounts_missing"]
    stats.in_scope = len(rows)

    files: dict[tuple[str, str], dict[int, Path]] = {}
    db = open_index(config.index)
    try:
        fingerprint = config.scope_fingerprint()
        existing = {r[0]: (r[1], r[2]) for r in
                    db.execute("SELECT rowid, flags, body_indexed FROM messages")}
        keep = {r.rowid for r in rows}
        for rowid in set(existing) - keep:
            db.execute("DELETE FROM messages WHERE rowid = ?", (rowid,))
            db.execute("DELETE FROM messages_fts WHERE rowid = ?", (rowid,))
            stats.removed += 1

        for row in rows:
            prior = existing.get(row.rowid)
            needs_file = prior is None or (config.bodies and not prior[1]) or (not config.bodies and prior[1])
            if prior is not None and not needs_file:
                if prior[0] != row.flags:
                    db.execute("UPDATE messages SET flags=?, is_read=?, is_flagged=?, mailbox=? WHERE rowid=?",
                               (row.flags, int(row.read), int(row.flagged), row.mailbox, row.rowid))
                    stats.updated += 1
                continue

            key = (row.account, row.mailbox)
            if key not in files:
                files[key] = message_files(version_dir, row.account, row.mailbox)
            path = files[key].get(row.rowid)
            parsed = None
            if path is None:
                stats.missing_files += 1
            else:
                try:
                    parsed = parse_message(path, bodies=config.bodies, attachments=config.attachments,
                                           max_chars=config.max_body_chars)
                except Exception:
                    stats.unreadable += 1

            subject = (parsed.subject if parsed and parsed.subject is not None else row.subject)
            from_addr = (parsed.from_addr if parsed and parsed.from_addr else row.sender)
            from_name = (parsed.from_name if parsed and parsed.from_name else row.sender_name)
            to = parsed.to if parsed else []
            cc = parsed.cc if parsed else []
            body = parsed.body if parsed else None
            db.execute(
                "INSERT OR REPLACE INTO messages (rowid, account, mailbox, message_id, in_reply_to, refs,"
                " thread_id, date_received, date_sent, from_addr, from_name, to_addrs, cc_addrs, subject,"
                " flags, is_read, is_flagged, body, body_indexed, attachments)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row.rowid, row.account, row.mailbox,
                 parsed.message_id if parsed else None,
                 parsed.in_reply_to if parsed else None,
                 json.dumps(parsed.references if parsed else []),
                 _thread_id(row, parsed), row.date_received, row.date_sent,
                 from_addr, from_name, json.dumps(to), json.dumps(cc), subject,
                 row.flags, int(row.read), int(row.flagged), body,
                 int(config.bodies and parsed is not None),
                 json.dumps(parsed.attachments) if parsed and config.attachments else None),
            )
            _fts_put(db, row.rowid, subject, from_addr, from_name, to, cc, body)
            if prior is None:
                stats.added += 1
            else:
                stats.updated += 1

        db.execute("INSERT OR REPLACE INTO meta VALUES ('scope', ?)", (fingerprint,))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('bodies', ?)", (str(int(config.bodies)),))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)",
                   (str(int(now if now is not None else time.time())),))
        db.commit()
    finally:
        db.close()
    return stats


def doctor(config: Config) -> dict:
    """Check that the store and config line up. Returns counts and booleans only."""
    version_dir = find_version_dir(config.store)
    with tempfile.TemporaryDirectory(prefix="mail-index-") as tmp:
        os.chmod(tmp, 0o700)
        meta_path = snapshot_metadata(version_dir, Path(tmp))
        db = sqlite3.connect(meta_path)
        try:
            cols = probe_columns(db)
            lo, hi = db.execute("SELECT MIN(date_received), MAX(date_received) FROM messages").fetchone()
        finally:
            db.close()
        rows, info = read_metadata(meta_path, config)
    by_box: dict[tuple[str, str], set[int]] = {}
    for r in rows:
        by_box.setdefault((r.account, r.mailbox), set()).add(r.rowid)
    found = sum(len(ids & message_files(version_dir, acct, box).keys())
                for (acct, box), ids in by_box.items())
    plausible = lo is not None and 946684800 <= lo <= hi <= time.time() + 86400 * 2
    return {
        "version_dir": version_dir.name,
        "optional_columns": {t: sorted(c) for t, c in cols.items() if t in OPTIONAL},
        "dates_look_like_unix_seconds": plausible,
        "messages_in_scope": len(rows),
        "mailboxes_in_scope": len(by_box),
        "message_files_found": found,
        "accounts_missing": info["accounts_missing"],
    }
