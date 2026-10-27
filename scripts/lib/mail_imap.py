"""Read an IMAP account into the mail index without changing it. See docs/email.md.

The `imap` source. For each in-scope mailbox it opens the mailbox with
EXAMINE (read-only for the session, enforced by the server), lists the
messages received since the config's `since` date, and fetches messages with
BODY.PEEK[] so nothing is marked read. ReadOnlyImap below is the only way this
module talks to the server, and it refuses every command that could change
the account. scripts/test-mail-imap.py fails if this file gains a write.

Settings, connecting and mailbox names are shared with the drafts outbox in
mail_imap_conn.py.
"""

from __future__ import annotations

import email
import email.policy
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Iterator

import mail_index as mi
from mail_imap_conn import (AuthFailed, Connection, ImapError, Mailbox, Refused,  # noqa: F401  (re-exported)
                            decode_mutf7, encode_mutf7, fetch_items, flags_of, parse_list, quote, uid_of)
import mail_imap_conn

SUMMARY_BATCH = 500   # messages per summary FETCH
DETAILS_BATCH = 25    # messages per full FETCH
DOCTOR_SAMPLE = 20
# Special-use mailboxes left out of patterns (name one to include it). \All
# and \Flagged are virtual: they hold copies of mail from other mailboxes.
EXCLUDED_SPECIAL_USE = {"\\junk", "\\trash", "\\all", "\\flagged"}


def account_settings(item: dict, base: Path, *, where: str) -> dict:
    """For mail_index.load_config: the IMAP keys of one account, checked."""
    try:
        return mail_imap_conn.account_settings(item, base, where=where)
    except mail_imap_conn.ConfigError as exc:
        raise mi.ConfigError(str(exc)) from None


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
                uid, flags, received = uid_of(meta), flags_of(meta), internaldate(meta)
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
                            uid = uid_of(meta)
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
