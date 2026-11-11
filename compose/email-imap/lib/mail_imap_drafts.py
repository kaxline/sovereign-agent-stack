"""Save drafts to an IMAP account's Drafts folder. See docs/email.md.

The drafts helper's outbox for `source = "imap"` accounts. It builds the
message itself and APPENDs it to the Drafts mailbox flagged \\Draft, so it
shows up in every client signed in to the account, where the user sends it.
Nothing here can send: there's no SMTP.

For a reply it reads five headers of the original (Message-ID, References,
From, Reply-To and Subject) with BODY.PEEK, to address and thread the reply.
It never reads a body. DraftsImap below is the only way this module talks
to the server, and scripts/test-mail-imap.py fails if it gains another
command.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

import mail_imap_conn as conn_lib

REPLY_HEADERS = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES FROM REPLY-TO SUBJECT)])"
POLICY = email.policy.SMTP  # CRLF line endings, as IMAP expects


class DraftError(RuntimeError):
    """A draft couldn't be saved; the message is a short reason for the status file."""


class DraftsImap(conn_lib.Connection):
    """LIST, EXAMINE, one header FETCH for replies, and APPEND to Drafts. Nothing else."""

    def drafts_box(self) -> conn_lib.Mailbox:
        boxes = [b for b in self.list_mailboxes() if b.selectable]
        named = self.settings.get("drafts_mailbox")
        if named:
            hit = next((b for b in boxes if b.path.lower() == named.lower()), None)
        else:
            hit = (next((b for b in boxes if "\\drafts" in b.flags), None)
                   or next((b for b in boxes if b.path.rsplit("/", 1)[-1].lower() == "drafts"), None))
        if hit is None:
            raise DraftError("drafts_mailbox_not_found")
        return hit

    def reply_headers(self, mailbox: str, source_id: str) -> EmailMessage:
        validity, _, uid = source_id.partition(".")
        if not (validity.isdigit() and uid.isdigit()):
            raise DraftError("reply_message_not_found")
        box = next((b for b in self.list_mailboxes() if b.path == mailbox and b.selectable), None)
        if box is None:
            raise DraftError("reply_mailbox_not_found")
        typ, _ = self._imap.select(conn_lib.quote(box.raw), readonly=True)  # EXAMINE
        if typ != "OK":
            raise DraftError("reply_mailbox_not_found")
        _, values = self._imap.response("UIDVALIDITY")
        if not values or values[0] is None or values[0].decode() != validity:
            raise DraftError("reply_message_not_found")
        typ, data = self._imap.uid("FETCH", uid, REPLY_HEADERS)
        for meta, literal in conn_lib.fetch_items(data if typ == "OK" else []):
            if conn_lib.uid_of(meta) == int(uid) and literal is not None:
                return email.message_from_bytes(literal, policy=email.policy.default)
        raise DraftError("reply_message_not_found")

    def append_draft(self, box: conn_lib.Mailbox, msg: EmailMessage) -> None:
        typ, data = self._imap.append(conn_lib.quote(box.raw), "(\\Draft \\Seen)",
                                      imaplib.Time2Internaldate(time.time()), msg.as_bytes(policy=POLICY))
        if typ != "OK":
            raise DraftError("append_refused")


def _ids(value) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def build_draft(request: dict, sender: str, original: EmailMessage | None) -> EmailMessage:
    """The draft as RFC 5322. `request` is already validated (drafts.validate)."""
    msg = EmailMessage(policy=POLICY)
    msg["From"] = sender
    to = list(request["to"])
    subject = request["subject"]
    if original is not None:
        reply_to = getaddresses([str(original.get("Reply-To") or original.get("From") or "")])
        to = [addr for _, addr in reply_to if addr] + [a for a in to if a not in {x for _, x in reply_to}]
        if not subject:
            subject = str(original.get("Subject") or "")
            if not re.match(r"(?i)re:", subject):
                subject = f"Re: {subject}".strip()
        parent = _ids(original.get("Message-ID"))
        if parent:
            msg["In-Reply-To"] = parent[0]
            msg["References"] = " ".join(_ids(original.get("References")) + parent)
    if to:
        msg["To"] = ", ".join(to)
    if request["cc"]:
        msg["Cc"] = ", ".join(request["cc"])
    if request["bcc"]:
        msg["Bcc"] = ", ".join(request["bcc"])
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(request["body"])
    return msg


def save_draft(account_id: str, settings: dict, request: dict, *, connect=DraftsImap) -> None:
    """Save one validated request as a draft in the account's Drafts. Raises DraftError."""
    addresses = settings.get("addresses") or []
    sender = request.get("from") or (addresses[0] if addresses else None)
    if not sender:
        raise DraftError("set address for this account in the config")
    if sender.lower() not in addresses:
        raise DraftError("from_not_an_account")
    with connect(account_id, settings) as conn:
        original = None
        reply = request.get("reply_to")
        if reply:
            original = conn.reply_headers(reply["mailbox"], reply["source_id"])
        if not request["to"] and original is None:
            raise DraftError("no_recipients")
        conn.append_draft(conn.drafts_box(), build_draft(request, sender, original))
