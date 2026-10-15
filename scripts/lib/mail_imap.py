"""Read an IMAP account into the mail index without changing it. See docs/email.md.

The `imap` source. For each in-scope mailbox it opens the mailbox with
EXAMINE (read-only for the session, enforced by the server), lists the
messages received since the config's `since` date, and fetches messages with
BODY.PEEK[] so nothing is marked read. ReadOnlyImap below is the only way this
module talks to the server, and it refuses every command that could change
the account.

The password comes from the Keychain through the mail apps' launcher, or, for
terminal runs, from the environment variable the account's `password_env`
names (see mail_credentials.py). It never comes from the config file.

Shared with the drafts outbox (mail_imap_drafts.py): account settings,
connecting, logging in, modified UTF-7 mailbox names and LIST parsing.
"""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterator

import mail_credentials
import mail_index as mi

SECURITY = ("tls", "starttls", "plain")
DEFAULT_PORT = {"tls": 993, "starttls": 143, "plain": 143}
ACCOUNT_KEYS = {"id", "mailboxes", "source", "host", "port", "security", "user", "address", "addresses",
                "password_env", "tls_ca", "timeout"}
SUMMARY_BATCH = 500   # messages per summary FETCH
DETAILS_BATCH = 25    # messages per full FETCH
DOCTOR_SAMPLE = 20
# Special-use mailboxes left out of patterns (name one to include it). \All
# and \Flagged are virtual: they hold copies of mail from other mailboxes.
EXCLUDED_SPECIAL_USE = {"\\junk", "\\trash", "\\all", "\\flagged"}
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


class ImapError(RuntimeError):
    """The server refused something or the connection failed."""


class AuthFailed(ImapError):
    """The server rejected the user name and password."""


class Refused(RuntimeError):
    """This code asked for something it must never do. Always a bug."""


# --------------------------------------------------------------------------- config

def is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _address(value, where: str) -> str:
    _, addr = parseaddr(value if isinstance(value, str) else "")
    if not addr or "@" not in addr or addr != value.strip():
        raise mi.ConfigError(f"{where}: {value!r} isn't a plain name@domain address")
    return addr


def account_settings(item: dict, base: Path, *, where: str) -> dict:
    """The IMAP keys of one [[accounts]] entry, checked. Raises ConfigError."""
    unknown = set(item) - ACCOUNT_KEYS
    if unknown:
        raise mi.ConfigError(f"{where}: unknown keys for an imap account: {', '.join(sorted(unknown))}")
    host = item.get("host")
    if not isinstance(host, str) or not host.strip() or any(c.isspace() for c in host.strip()):
        raise mi.ConfigError(f"{where}: an imap account needs `host`")
    host = host.strip()
    security = item.get("security", "tls")
    if security not in SECURITY:
        raise mi.ConfigError(f"{where}: `security` must be \"tls\" (the default) or \"starttls\"")
    if security == "plain" and not is_loopback(host):
        raise mi.ConfigError(f"{where}: security = \"plain\" is only allowed for a server on this machine"
                             " (localhost); use \"tls\" or \"starttls\"")
    port = item.get("port", DEFAULT_PORT[security])
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise mi.ConfigError(f"{where}: `port` must be a port number")
    user = item.get("user")
    if not isinstance(user, str) or not user.strip():
        raise mi.ConfigError(f"{where}: an imap account needs `user`")
    addresses = []
    if "address" in item:
        addresses.append(_address(item["address"], f"{where}.address"))
    extra = item.get("addresses", [])
    if not isinstance(extra, list):
        raise mi.ConfigError(f"{where}: `addresses` must be a list")
    addresses += [_address(a, f"{where}.addresses") for a in extra]
    password_env = item.get("password_env")
    if password_env is not None and (not isinstance(password_env, str) or not _ENV_NAME.fullmatch(password_env)):
        raise mi.ConfigError(f"{where}: `password_env` must be an environment variable name like WORK_IMAP_PASSWORD")
    tls_ca = mi._path(item["tls_ca"], f"{where}.tls_ca", base) if "tls_ca" in item else None
    timeout = item.get("timeout", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise mi.ConfigError(f"{where}: `timeout` must be a positive number of seconds")
    return {"host": host, "port": port, "security": security, "user": user.strip(),
            "addresses": list(dict.fromkeys(a.lower() for a in addresses)),
            "password_env": password_env, "tls_ca": str(tls_ca) if tls_ca else None, "timeout": timeout}


# --------------------------------------------------------------------------- mailbox names

def decode_mutf7(name: str) -> str:
    """IMAP's modified UTF-7 (RFC 3501 5.1.3) to text."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "&":
            end = name.find("-", i)
            if end == -1:
                raise ValueError("unterminated shift in mailbox name")
            chunk = name[i + 1:end]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = end + 1
        else:
            out.append(name[i])
            i += 1
    return "".join(out)


def encode_mutf7(text: str) -> str:
    out, pending = [], []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def quote(name: str) -> str:
    """A mailbox name as an IMAP quoted string."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Mailbox:
    path: str           # "/"-separated, decoded
    raw: str            # the server's encoded name
    flags: set[str]     # lowercased, e.g. {"\\hasnochildren", "\\junk"}

    @property
    def selectable(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.flags)

    @property
    def special(self) -> set[str]:
        return self.flags & {"\\junk", "\\trash", "\\all", "\\flagged", "\\drafts", "\\sent", "\\archive"}


_LIST = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.*)', re.S)


def _unquote(raw: bytes) -> bytes:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        return re.sub(rb"\\(.)", rb"\1", raw[1:-1])
    return raw


def parse_list(data: list) -> list[Mailbox]:
    """Mailboxes from imaplib's LIST response, with literal names joined back up."""
    boxes = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):  # {n} literal: (b'(\\Flags) "/" {n}', b'name')
            head, literal = item[0], item[1]
            m = _LIST.match(head)
            name_raw = literal
        else:
            m = _LIST.match(item)
            name_raw = _unquote(m.group("name")) if m else b""
        if not m:
            continue
        delim = _unquote(m.group("delim")).decode() if m.group("delim") != b"NIL" else ""
        raw = name_raw.decode("ascii", "replace")
        try:
            decoded = decode_mutf7(raw)
        except (ValueError, UnicodeDecodeError):
            decoded = raw
        path = "/".join(decoded.split(delim)) if delim and delim != "/" else decoded
        flags = {f.lower() for f in m.group("flags").decode("ascii", "replace").split()}
        boxes.append(Mailbox(path=path, raw=raw, flags=flags))
    return boxes


def in_scope(box: Mailbox, patterns: list[str]) -> bool:
    """The core's mailbox rules, plus the server's special-use flags: a pattern
    skips Junk, Trash and the virtual All Mail and Flagged mailboxes, whatever
    they're called. Naming one exactly includes it."""
    if not box.selectable:
        return False
    if any(p.lower() == box.path.lower() for p in patterns):
        return True
    if box.flags & EXCLUDED_SPECIAL_USE:
        return False
    return mi.mailbox_selected(box.path, patterns)


# --------------------------------------------------------------------------- connection

class Connection:
    """Connects and logs in. Subclasses decide which commands are allowed."""

    def __init__(self, account_id: str, settings: dict) -> None:
        self.account_id = account_id
        self.settings = settings
        self.capabilities: set[str] = set()
        self._imap: imaplib.IMAP4 | None = None

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.settings.get("tls_ca"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def open(self) -> "Connection":
        s = self.settings
        try:
            if s["security"] == "tls":
                imap = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=self._context(), timeout=s["timeout"])
            else:
                imap = imaplib.IMAP4(s["host"], s["port"], timeout=s["timeout"])
                if s["security"] == "starttls":
                    imap.starttls(ssl_context=self._context())
        except (OSError, socket.timeout, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise ImapError(f"{self.account_id}: can't connect to {s['host']}:{s['port']}"
                            f" ({type(exc).__name__}: {exc})") from None
        password = mail_credentials.password(self.account_id, s.get("password_env"))
        try:
            imap.login(s["user"], password)
        except imaplib.IMAP4.error:
            try:
                imap.shutdown()
            except OSError:
                pass
            raise AuthFailed(f"{self.account_id}: the server rejected the user name or password") from None
        finally:
            del password
        self._imap = imap
        self.capabilities = {c.upper() for c in imap.capabilities}
        return self

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            self._imap = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _check(self, typ: str, data, what: str):
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data and isinstance(data[0], bytes) else ""
            raise ImapError(f"{self.account_id}: {what} failed: {detail[:200]}")
        return data

    def list_mailboxes(self) -> list[Mailbox]:
        return parse_list(self._check(*self._imap.list(), "LIST"))


class ReadOnlyImap(Connection):
    """The reader's view of the server: LIST, STATUS, EXAMINE, UID SEARCH and
    UID FETCH with BODY.PEEK only. Anything else raises Refused."""

    _FETCH_OK = re.compile(r"\((?:UID|FLAGS|INTERNALDATE|RFC822\.SIZE|BODY\.PEEK\[[A-Z0-9.() -]*\]|\s)+\)")

    def examine(self, box: Mailbox) -> int | None:
        """Open box read-only. Returns its UIDVALIDITY, or None if it doesn't exist."""
        typ, data = self._imap.select(quote(box.raw), readonly=True)  # EXAMINE
        if typ != "OK":
            return None
        _, values = self._imap.response("UIDVALIDITY")
        try:
            return int(values[0])
        except (TypeError, ValueError, IndexError):
            raise ImapError(f"{self.account_id}: the server gave no UIDVALIDITY for a mailbox") from None

    def status_count(self, box: Mailbox) -> int | None:
        typ, data = self._imap.status(quote(box.raw), "(MESSAGES)")
        if typ != "OK" or not data or not data[0]:
            return None
        m = re.search(rb"MESSAGES (\d+)", data[0])
        return int(m.group(1)) if m else None

    def uid_search_since(self, day) -> list[int]:
        since = f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"
        data = self._check(*self._imap.uid("SEARCH", "SINCE", since), "UID SEARCH")
        return [int(x) for x in (data[0] or b"").split()]

    def uid_fetch(self, uids: list[int], items: str) -> list:
        if not self._FETCH_OK.fullmatch(items) or "BODY[" in items.replace("BODY.PEEK[", ""):
            raise Refused(f"FETCH items not allowed for the reader: {items}")
        return self._check(*self._imap.uid("FETCH", ",".join(map(str, uids)), items), "UID FETCH")


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_INTERNALDATE = re.compile(rb'INTERNALDATE "(\s?\d{1,2})-(\w{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)"')


def internaldate(meta: bytes) -> int | None:
    m = _INTERNALDATE.search(meta)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, tzh, tzm = m.groups()
    try:
        month = _MONTHS.index(mon.decode().title()) + 1
    except ValueError:
        return None
    dt = datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=timezone.utc)
    offset = (int(tzh) * 3600 + int(tzm) * 60) * (1 if sign == b"+" else -1)
    return int(dt.timestamp()) - offset


def fetch_items(data: list) -> Iterator[tuple[bytes, bytes | None]]:
    """(metadata, literal) pairs from a FETCH response. Items without a body
    literal come back as plain bytes."""
    for item in data:
        if isinstance(item, tuple):
            yield item[0], item[1]
        elif isinstance(item, bytes) and re.match(rb"\d+ \(", item):
            yield item, None


def _uid(meta: bytes) -> int | None:
    m = re.search(rb"\bUID (\d+)", meta)
    return int(m.group(1)) if m else None


def _flags(meta: bytes) -> set[str]:
    m = re.search(rb"FLAGS \(([^)]*)\)", meta)
    return {f.lower() for f in m.group(1).decode("ascii", "replace").split()} if m else set()


# --------------------------------------------------------------------------- source

class ImapSource:
    """One or more IMAP accounts. Opens one connection per account at a time."""

    name = "imap"

    def __init__(self, config: mi.Config, connect=None) -> None:
        self.config = config
        self.connect = connect or ReadOnlyImap  # tests may pass a factory

    def _accounts(self):
        for scope in self.config.accounts:
            yield scope, self.connect(scope.id, scope.settings)

    def _scoped(self, conn: ReadOnlyImap, scope: mi.AccountScope) -> tuple[list[Mailbox], int]:
        boxes = conn.list_mailboxes()
        chosen = [b for b in boxes if in_scope(b, scope.mailboxes)]
        known = {b.path.lower() for b in boxes if b.selectable}
        missing = sum(1 for p in scope.mailboxes if mi.is_literal(p) and p.lower() not in known)
        return chosen, missing

    def _summaries(self, conn: ReadOnlyImap, scope: mi.AccountScope, box: Mailbox) -> list[mi.MetaRow]:
        validity = conn.examine(box)
        if validity is None:
            return []
        since = mi.since_epoch(self.config.since)
        uids = conn.uid_search_since(self.config.since)
        rows = []
        for start in range(0, len(uids), SUMMARY_BATCH):
            chunk = uids[start:start + SUMMARY_BATCH]
            data = conn.uid_fetch(chunk, "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
            for meta, literal in fetch_items(data):
                uid, flags, received = _uid(meta), _flags(meta), internaldate(meta)
                if uid is None or received is None or "\\deleted" in flags:
                    continue
                # SEARCH SINCE works on whole days in the server's time zone; trim to the exact cutoff.
                if received < since:
                    continue
                head = email.message_from_bytes(literal or b"", policy=email.policy.default)
                try:
                    sent = int(parsedate_to_datetime(str(head["Date"])).timestamp()) if head["Date"] else None
                except (TypeError, ValueError, IndexError):
                    sent = None
                name, addr = parseaddr(str(head["From"] or ""))
                read, flagged = "\\seen" in flags, "\\flagged" in flags
                rows.append(mi.MetaRow(
                    source_id=f"{validity}.{uid}", account=scope.id, mailbox=box.path,
                    date_received=received, date_sent=sent,
                    subject=str(head["Subject"]) if head["Subject"] is not None else None,
                    sender=addr or None, sender_name=name or None,
                    flags=(mi.FLAG_READ if read else 0) | (mi.FLAG_FLAGGED if flagged else 0),
                    read=read, flagged=flagged, deleted=False, conversation_id=None,
                ))
        return rows

    def rows(self) -> tuple[list[mi.MetaRow], dict]:
        rows: list[mi.MetaRow] = []
        missing = found = 0
        for scope, conn in self._accounts():
            with conn:
                boxes, gone = self._scoped(conn, scope)
                missing += gone
                found += len(boxes)
                for box in boxes:
                    rows += self._summaries(conn, scope, box)
        return rows, {"accounts_missing": [], "mailboxes_missing": missing, "mailboxes_found": found}

    def fetch(self, rows: list[mi.MetaRow]) -> Iterator[tuple[mi.MetaRow, object]]:
        cfg = self.config
        items = "(UID BODY.PEEK[])" if cfg.bodies or cfg.attachments else "(UID BODY.PEEK[HEADER])"
        by_account: dict[str, dict[str, list[mi.MetaRow]]] = {}
        for row in rows:
            by_account.setdefault(row.account.upper(), {}).setdefault(row.mailbox, []).append(row)
        for scope, conn in self._accounts():
            boxes = by_account.get(scope.id.upper())
            if not boxes:
                continue
            with conn:
                listed = {b.path: b for b in conn.list_mailboxes()}
                for path, group in boxes.items():
                    box = listed.get(path)
                    validity = conn.examine(box) if box else None
                    by_uid: dict[int, mi.MetaRow] = {}
                    for row in group:
                        v, _, uid = row.source_id.partition(".")
                        if validity is None or str(validity) != v:
                            yield row, mi.MISSING  # mailbox gone, or renumbered since the listing
                        else:
                            by_uid[int(uid)] = row
                    uids = list(by_uid)
                    for start in range(0, len(uids), DETAILS_BATCH):
                        chunk = uids[start:start + DETAILS_BATCH]
                        got: dict[int, bytes] = {}
                        for meta, literal in fetch_items(conn.uid_fetch(chunk, items)):
                            uid = _uid(meta)
                            if uid is not None and literal is not None:
                                got[uid] = literal
                        for uid in chunk:
                            row = by_uid[uid]
                            if uid not in got:
                                yield row, mi.MISSING
                                continue
                            try:
                                yield row, mi.parse_raw(got[uid], bodies=cfg.bodies, attachments=cfg.attachments,
                                                        max_chars=cfg.max_body_chars)
                            except Exception:
                                yield row, mi.UNREADABLE

    def mailboxes(self) -> list[dict]:
        """Every account's mailboxes with message counts, for writing the config."""
        out = []
        for scope, conn in self._accounts():
            with conn:
                for box in conn.list_mailboxes():
                    if box.selectable:
                        out.append({"account": scope.id, "mailbox": box.path, "messages": conn.status_count(box),
                                    "special_use": sorted(box.special) or None})
        return out

    def doctor(self) -> dict:
        """Counts, timings and booleans only, for the mailbox owner to run and report."""
        accounts = []
        for scope, conn in self._accounts():
            start = time.monotonic()
            with conn:
                connected = time.monotonic()
                boxes, missing = self._scoped(conn, scope)
                listed = time.monotonic()
                rows = [r for box in boxes for r in self._summaries(conn, scope, box)]
                summarised = time.monotonic()
                caps = conn.capabilities
            sample = rows[:DOCTOR_SAMPLE]
            fetch_start = time.monotonic()
            results = list(self.fetch(sample))
            fetched = time.monotonic()
            accounts.append({
                "account": scope.id,
                "seconds_to_log_in": round(connected - start, 2),
                "mailboxes_in_scope": len(boxes),
                "mailboxes_missing": missing,
                "messages_in_scope": len(rows),
                "seconds_mailbox_listing": round(listed - connected, 2),
                "seconds_message_summaries": round(summarised - listed, 2),
                "details_sampled": len(sample),
                "details_readable": sum(1 for _, r in results if isinstance(r, mi.ParsedMessage)),
                "seconds_per_message_details": round((fetched - fetch_start) / len(sample), 3) if sample else None,
                "server_special_use": "SPECIAL-USE" in caps,
                "server_idle": "IDLE" in caps,
                "server_condstore": "CONDSTORE" in caps,
            })
        return {"source": "imap", "accounts": accounts}
