"""Draft requests: validation and the on-disk queue. See docs/email.md.

The mail-drafts MCP (container) writes requests into <dir>/queue. The host
helper (scripts/mail-drafts.py) validates them again, asks Mail.app to save
each one as a draft, writes <dir>/status/<id>.json and deletes the request.
Nothing here can send mail: a draft waits in Mail until the user sends it.

Standard library only; the host helper imports this file too.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

VERSION = 1
MAX_RECIPIENTS = 20      # to + cc + bcc
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 100_000
MAX_PENDING = 50         # queued requests before new ones are refused
MAX_FIELD_CHARS = 500    # account, mailbox

# A deliberately plain addr-spec: no display names, quotes, comments or
# whitespace, so nothing can smuggle extra headers or recipients.
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ID = re.compile(r"[0-9a-f]{32}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")  # Mail's id, or IMAP's UIDVALIDITY.UID


class BadRequest(ValueError):
    pass


class QueueFull(RuntimeError):
    pass


def _addresses(value, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise BadRequest(f"`{field}` must be a list of email addresses")
    out = []
    for raw in value:
        addr = raw.strip()
        if len(addr) > 254 or not _ADDRESS.fullmatch(addr):
            raise BadRequest(f"`{field}` has an address that isn't a plain name@domain: {addr[:80]!r}")
        out.append(addr)
    return out


def _text(value, field: str, limit: int, *, single_line: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest(f"`{field}` must be text")
    if single_line and _CONTROL.search(value):
        raise BadRequest(f"`{field}` must be one line")
    value = value.replace("\x00", "")
    if len(value) > limit:
        raise BadRequest(f"`{field}` is longer than {limit} characters")
    return value


def _reply_to(value) -> dict:
    """The message to reply to, as {account, mailbox, source_id}.

    Takes a result's `reply_ref` ({account, mailbox, source_id}), or the older
    {id, account, mailbox} form, where id is Mail.app's message id.
    """
    if not isinstance(value, dict) or set(value) not in ({"account", "mailbox", "source_id"},
                                                         {"id", "account", "mailbox"}):
        raise BadRequest("`reply_to` must be the `reply_ref` of a mail_search or mail_get result")
    if "id" in value:
        rid = value["id"]
        if not isinstance(rid, int) or isinstance(rid, bool) or rid <= 0:
            raise BadRequest("`reply_to.id` must be a positive integer")
        source_id = str(rid)
    else:
        source_id = value["source_id"]
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise BadRequest("`reply_to.source_id` must be the value from a result's reply_ref")
    clean = {"account": _text(value["account"], "reply_to.account", MAX_FIELD_CHARS, single_line=True),
             "mailbox": _text(value["mailbox"], "reply_to.mailbox", MAX_FIELD_CHARS, single_line=True),
             "source_id": source_id}
    if not clean["account"] or not clean["mailbox"]:
        raise BadRequest("`reply_to.account` and `reply_to.mailbox` can't be empty")
    return clean


def validate(req: dict) -> dict:
    """A clean copy of a draft request, or BadRequest. Unknown keys are refused."""
    if not isinstance(req, dict):
        raise BadRequest("request must be an object")
    allowed = {"version", "id", "created_at", "to", "cc", "bcc", "subject", "body", "from", "reply_to"}
    unknown = set(req) - allowed
    if unknown:
        raise BadRequest(f"unknown fields: {', '.join(sorted(unknown))}")
    to = _addresses(req.get("to"), "to")
    cc = _addresses(req.get("cc"), "cc")
    bcc = _addresses(req.get("bcc"), "bcc")
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise BadRequest(f"at most {MAX_RECIPIENTS} recipients in all")
    reply_to = req.get("reply_to")
    if reply_to is not None:
        reply_to = _reply_to(reply_to)
    elif not to:
        raise BadRequest("`to` needs at least one address (or use reply_to)")
    sender = req.get("from")
    if sender is not None:
        sender = _addresses(sender, "from")
        if len(sender) != 1:
            raise BadRequest("`from` is one address")
        sender = sender[0]
    clean = {
        "version": VERSION,
        "to": to, "cc": cc, "bcc": bcc,
        "subject": _text(req.get("subject"), "subject", MAX_SUBJECT_CHARS, single_line=True),
        "body": _text(req.get("body"), "body", MAX_BODY_CHARS, single_line=False),
        "from": sender,
        "reply_to": reply_to,
    }
    if not clean["subject"] and reply_to is None:
        raise BadRequest("`subject` can't be empty")
    if "id" in req:
        clean["id"] = check_id(req["id"])
    if "created_at" in req:
        if not isinstance(req["created_at"], int):
            raise BadRequest("`created_at` must be Unix seconds")
        clean["created_at"] = req["created_at"]
    return clean


def check_id(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BadRequest("request id must be 32 lowercase hex characters")
    return value


def dirs(root: Path) -> tuple[Path, Path]:
    queue, status = root / "queue", root / "status"
    for d in (root, queue, status):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except PermissionError:
            pass
    return queue, status


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def pending(root: Path) -> list[Path]:
    queue, _ = dirs(root)
    return sorted((p for p in queue.glob("*.json") if _ID.fullmatch(p.stem)),
                  key=lambda p: (p.stat().st_mtime, p.name))


def enqueue(root: Path, req: dict, *, now: float | None = None) -> str:
    clean = validate(req)
    queue, _ = dirs(root)
    if len(pending(root)) >= MAX_PENDING:
        raise QueueFull(f"{MAX_PENDING} drafts are already waiting for the host helper")
    clean["id"] = uuid.uuid4().hex
    clean["created_at"] = int(now if now is not None else time.time())
    _write(queue / f"{clean['id']}.json", clean)
    return clean["id"]


def write_status(root: Path, request_id: str, state: str, reason: str | None = None,
                 *, now: float | None = None) -> None:
    _, status = dirs(root)
    data = {"id": check_id(request_id), "state": state, "at": int(now if now is not None else time.time())}
    if reason:
        data["reason"] = reason
    _write(status / f"{request_id}.json", data)


def read_status(root: Path, request_id: str) -> dict:
    request_id = check_id(request_id)
    queue, status = dirs(root)
    path = status / f"{request_id}.json"
    if path.is_file():
        return json.loads(path.read_text())
    if (queue / f"{request_id}.json").is_file():
        return {"id": request_id, "state": "queued"}
    return {"id": request_id, "state": "unknown"}
