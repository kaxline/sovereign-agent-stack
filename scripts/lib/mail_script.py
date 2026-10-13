"""Read mail through Mail.app scripting instead of Mail's files. See docs/email.md.

This is the indexer's default source. It runs mail_script.js (JXA) through
osascript, which needs the Automation permission for Mail but not Full Disk
Access. The script only reads; this module turns its output into the same
rows and parsed messages the store source produces, so mail_index.run()
treats both alike.

Tests swap the runner for one that executes the script under Node against a
fake Mail built from the synthetic specs (scripts/lib/fake_mail_app.js).
"""

from __future__ import annotations

import email
import email.policy
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Iterator

import mail_index as mi
from jxa import AutomationDenied, Runner, ScriptError, command_runner  # noqa: F401  (re-exported)
import jxa

SCRIPT = Path(__file__).with_name("mail_script.js")
BATCH = 50          # messages per details call
DOCTOR_SAMPLE = 20  # messages timed by doctor


def osascript_runner(timeout: int) -> Runner:
    return jxa.osascript_runner(SCRIPT, timeout)


def _sender(value: str | None) -> tuple[str | None, str | None]:
    name, addr = parseaddr(value or "")
    return (addr or None), (name or None)


def parsed_from_details(item: dict, *, bodies: bool, attachments: bool, max_chars: int) -> mi.ParsedMessage:
    msg = email.message_from_string((item.get("headers") or "").strip() + "\n\n",
                                    policy=email.policy.default)
    body = None
    if bodies:
        body = (item.get("content") or "").strip()[:max_chars]
    atts = []
    if attachments:
        for att in item.get("attachments") or []:
            size = att.get("size")
            atts.append({"name": att.get("name"), "mime": att.get("mime"),
                         "size": int(size) if isinstance(size, (int, float)) else None})
    return mi.parsed_from_message(msg, body=body, attachments=atts)


class ScriptSource:
    name = "script"

    def __init__(self, config: mi.Config, runner: Runner | None = None) -> None:
        self.config = config
        self.runner = runner or osascript_runner(config.script_timeout)

    def mailboxes(self, *, counts: bool, accounts: list[str] | None = None) -> list[dict]:
        request = {"command": "mailboxes", "counts": counts}
        if accounts is not None:
            request["accounts"] = accounts
        return self.runner(request)["accounts"]

    def scoped_boxes(self) -> tuple[list[dict], list[str]]:
        """Mailboxes to read, and accounts the listing didn't find.

        An account whose mailboxes are all exact names isn't listed at all:
        its names go straight to `list`, which looks each one up by path. Only
        accounts with a pattern pay for walking their whole mailbox tree.
        """
        boxes: list[dict] = []
        patterned: dict[str, list[str]] = {}
        for scope in self.config.accounts:
            if all(mi.is_literal(p) for p in scope.mailboxes):
                boxes += [{"account": scope.id, "path": p} for p in scope.mailboxes]
            else:
                patterned[scope.id.upper()] = scope.mailboxes
        missing: set[str] = set()
        if patterned:
            found = self.mailboxes(counts=False, accounts=list(patterned))
            missing = set(patterned) - {a["id"].upper() for a in found}
            boxes += [{"account": a["id"], "path": b["path"]}
                      for a in found for b in a["mailboxes"]
                      if mi.mailbox_selected(b["path"], patterned[a["id"].upper()])]
        return boxes, sorted(missing)

    def rows(self, scoped: tuple[list[dict], list[str]] | None = None) -> tuple[list[mi.MetaRow], dict]:
        boxes, missing = scoped or self.scoped_boxes()
        missing = set(missing)
        rows: list[mi.MetaRow] = []
        boxes_missing = 0
        boxes_found = 0
        if boxes:
            since = mi.since_epoch(self.config.since)
            listing = self.runner({"command": "list", "since": since, "boxes": boxes})
            missing |= {a.upper() for a in listing.get("accounts_missing", [])}
            for box in listing["boxes"]:
                if not box.get("found", True):
                    if box["account"].upper() not in missing:
                        boxes_missing += 1
                    continue
                boxes_found += 1
                for m in box["messages"]:
                    if m.get("deleted"):
                        continue
                    addr, name = _sender(m.get("sender"))
                    flags = (mi.FLAG_READ if m.get("read") else 0) | (mi.FLAG_FLAGGED if m.get("flagged") else 0)
                    rows.append(mi.MetaRow(
                        rowid=int(m["id"]), account=box["account"], mailbox=box["path"],
                        date_received=int(m["received"]),
                        date_sent=int(m["sent"]) if m.get("sent") is not None else None,
                        subject=m.get("subject"), sender=addr, sender_name=name, flags=flags,
                        read=bool(m.get("read")), flagged=bool(m.get("flagged")), deleted=False,
                        conversation_id=None,
                    ))
        return rows, {"accounts_missing": sorted(missing), "mailboxes_missing": boxes_missing,
                      "mailboxes_found": boxes_found}

    def fetch(self, rows: list[mi.MetaRow]) -> Iterator[tuple[mi.MetaRow, object]]:
        by_box: dict[tuple[str, str], list[mi.MetaRow]] = {}
        for row in rows:
            by_box.setdefault((row.account, row.mailbox), []).append(row)
        for (account, path), group in by_box.items():
            for start in range(0, len(group), BATCH):
                chunk = group[start:start + BATCH]
                reply = self.runner({
                    "command": "details", "account": account, "path": path,
                    "ids": [r.rowid for r in chunk], "body": self.config.bodies,
                    "attachments": self.config.attachments, "max_chars": self.config.max_body_chars,
                })
                items = {int(m["id"]): m for m in reply.get("messages", [])}
                for row in chunk:
                    item = items.get(row.rowid)
                    if item is None or item.get("error") == "missing":
                        yield row, mi.MISSING
                    elif item.get("error"):
                        yield row, mi.UNREADABLE
                    else:
                        try:
                            yield row, parsed_from_details(
                                item, bodies=self.config.bodies, attachments=self.config.attachments,
                                max_chars=self.config.max_body_chars)
                        except Exception:
                            yield row, mi.UNREADABLE

    def doctor(self) -> dict:
        """Counts and timings only, for the mailbox owner to run and report."""
        start = time.monotonic()
        boxes, missing = self.scoped_boxes()
        listed = time.monotonic()
        rows, info = self.rows((boxes, missing))
        summarised = time.monotonic()
        sample = rows[:DOCTOR_SAMPLE]
        results = list(self.fetch(sample))
        fetched = time.monotonic()
        ok = sum(1 for _, r in results if isinstance(r, mi.ParsedMessage))
        return {
            "source": "script",
            "accounts_missing": info["accounts_missing"],
            "mailboxes_in_scope": info["mailboxes_found"],
            "mailboxes_missing": info["mailboxes_missing"],
            "messages_in_scope": len(rows),
            "seconds_mailbox_listing": round(listed - start, 2),
            "seconds_message_summaries": round(summarised - listed, 2),
            "details_sampled": len(sample),
            "details_readable": ok,
            "seconds_per_message_details": round((fetched - summarised) / len(sample), 3) if sample else None,
        }
