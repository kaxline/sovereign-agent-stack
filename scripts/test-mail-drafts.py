#!/usr/bin/env python3
"""Checks for the drafts path: request validation, the queue, and the host
helper against a fake Mail run under Node. Nothing here talks to Mail."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import jxa  # noqa: E402
import mail_drafts as md  # noqa: E402
import synthetic_mail as sm  # noqa: E402

drafts = md.drafts
PERSONAL = sm.ACCOUNTS["personal"].id
FAKE = LIB / "fake_mail_app.js"
GOOD = {"to": ["alex.chen@example.com"], "subject": "Lunch next week", "body": "How about Tuesday?\n"}


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def rejects(req: dict, why: str) -> None:
    try:
        drafts.validate(req)
    except drafts.BadRequest:
        return
    fail(f"accepted: {why}")


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.n = 0

    def setup(self, **options) -> tuple[Path, Path, jxa.Runner]:
        """A drafts folder and a runner over a fake Mail; returns (root, drafts_out, runner)."""
        self.n += 1
        root = self.tmp / f"drafts-{self.n}"
        out = self.tmp / f"saved-{self.n}.jsonl"
        out.write_text("")
        fixture = sm.script_fixture() | {"drafts_out": str(out)} | options
        path = self.tmp / f"fixture-{self.n}.json"
        path.write_text(json.dumps(fixture))
        runner = jxa.command_runner(["node", str(FAKE), str(path), str(md.SCRIPT)], 60)
        return root, out, runner


def saved(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines() if line]


def status(root: Path, request_id: str) -> dict:
    return drafts.read_status(root, request_id)


def test_script_cannot_send_or_read() -> None:
    code = re.sub(r"//[^\n]*", "", md.SCRIPT.read_text())
    banned = re.findall(r"\b(send|delete|move|duplicate|forward|redirect|activate|quit|close|"
                        r"doShellScript|allHeaders|mailAttachments|source)\b"
                        r"|\.(content|subject|sender|dateReceived)\s*\(\s*\)", code)
    expect(not banned, f"mail_drafts.js may only make and save drafts, found: {banned}")


def test_validation() -> None:
    clean = drafts.validate(GOOD | {"cc": ["priya@example.org"], "from": "sam@example.com"})
    expect(clean["to"] == ["alex.chen@example.com"] and clean["from"] == "sam@example.com", f"clean: {clean}")
    expect(drafts.validate({"to": "alex.chen@example.com", "subject": "s"})["to"] == ["alex.chen@example.com"],
           "a single address string is accepted")
    rejects(GOOD | {"to": ["Alex <alex.chen@example.com>"]}, "display name")
    rejects(GOOD | {"to": ["alex.chen@example.com, mallory@example.net"]}, "two addresses in one string")
    rejects(GOOD | {"to": ["alex.chen@example.com\nBcc: mallory@example.net"]}, "header injection in address")
    rejects(GOOD | {"subject": "Hi\r\nBcc: mallory@example.net"}, "newline in subject")
    rejects(GOOD | {"to": [f"p{i}@example.com" for i in range(21)]}, "too many recipients")
    rejects(GOOD | {"cc": [f"p{i}@example.com" for i in range(10)], "bcc": [f"q{i}@example.com" for i in range(10)]},
            "too many recipients across fields")
    rejects(GOOD | {"attachments": ["/etc/passwd"]}, "attachments")
    rejects({"subject": "s", "body": "b"}, "no recipients and no reply_to")
    rejects(GOOD | {"subject": ""}, "empty subject on a new message")
    rejects(GOOD | {"reply_to": {"id": 1, "account": "A"}}, "reply_to missing mailbox")
    rejects(GOOD | {"reply_to": {"id": "1", "account": "A", "mailbox": "INBOX"}}, "reply_to id as text")
    rejects(GOOD | {"reply_to": {"id": 1, "account": "A", "mailbox": "IN\nBOX"}}, "newline in reply_to")
    rejects(GOOD | {"from": ["a@example.com", "b@example.com"]}, "two from addresses")
    rejects(GOOD | {"body": "x" * (drafts.MAX_BODY_CHARS + 1)}, "body too long")
    reply = drafts.validate({"reply_to": {"id": 101, "account": PERSONAL, "mailbox": "INBOX"}, "body": "Yes!"})
    expect(reply["to"] == [] and reply["subject"] == "", "a reply needs no recipients or subject")


def test_queue(env: Env) -> None:
    root = env.tmp / "queue-test"
    request_id = drafts.enqueue(root, GOOD)
    path = root / "queue" / f"{request_id}.json"
    expect(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600, "queued file is owner-only")
    expect(stat.S_IMODE((root / "queue").stat().st_mode) == 0o700, "queue folder is owner-only")
    expect(status(root, request_id)["state"] == "queued", "status of a queued request")
    expect(status(root, "0" * 32)["state"] == "unknown", "status of an unknown request")
    for bad in ("../../etc/passwd", "ABC", request_id + "x"):
        try:
            drafts.read_status(root, bad)
        except drafts.BadRequest:
            continue
        fail(f"status accepted a bad id: {bad!r}")
    for _ in range(drafts.MAX_PENDING - 1):
        drafts.enqueue(root, GOOD)
    try:
        drafts.enqueue(root, GOOD)
    except drafts.QueueFull:
        pass
    else:
        fail("queue should refuse past MAX_PENDING")
    expect(not list((root / "queue").glob(".*.tmp")), "no temp files left behind")


def test_new_drafts(env: Env) -> None:
    root, out, runner = env.setup()
    first = drafts.enqueue(root, GOOD | {"cc": ["priya@example.org"], "bcc": ["morgan.diaz@example.org"]})
    second = drafts.enqueue(root, GOOD | {"subject": "From work", "from": "sam.rivera@example.org"})
    stats = md.Helper(root, runner=runner).process()
    expect(stats.as_dict() == {"created": 2, "failed": 0, "rejected": 0, "deferred": 0}, f"stats {stats}")
    got = saved(out)
    expect(len(got) == 2, f"two drafts saved: {got}")
    expect(got[0] == {"subject": "Lunch next week", "content": "How about Tuesday?\n", "visible": False,
                           "sender": None, "to": ["alex.chen@example.com"], "cc": ["priya@example.org"],
                           "bcc": ["morgan.diaz@example.org"], "reply_to": None, "reply_all": None},
           f"first draft: {got[0]}")
    expect(got[1]["sender"] == "sam.rivera@example.org", "from picks one of the user's accounts")
    expect(status(root, first)["state"] == "created" and status(root, second)["state"] == "created", "statuses")
    expect(not drafts.pending(root), "processed requests leave the queue")
    text = (root / "status" / f"{first}.json").read_text()
    expect("Tuesday" not in text and "alex.chen" not in text, "status files hold no draft content")

    root, out, runner = env.setup()
    drafts.enqueue(root, GOOD)
    md.Helper(root, runner=runner, open_windows=True).process()
    expect(saved(out)[0]["visible"] is True, "--open shows a compose window")


def test_replies(env: Env) -> None:
    root, out, runner = env.setup()
    ok = drafts.enqueue(root, {"reply_to": {"id": 101, "account": PERSONAL, "mailbox": "INBOX"},
                               "body": "Thursday works.", "cc": ["priya@example.org"]})
    gone = drafts.enqueue(root, {"reply_to": {"id": 424242, "account": PERSONAL, "mailbox": "INBOX"}, "body": "x"})
    nobox = drafts.enqueue(root, {"reply_to": {"id": 101, "account": PERSONAL, "mailbox": "Nope"}, "body": "x"})
    stats = md.Helper(root, runner=runner).process()
    expect(stats.created == 1 and stats.failed == 2, f"reply stats {stats}")
    got = saved(out)
    expect(len(got) == 1 and got[0]["reply_to"] == 101 and got[0]["reply_all"] is False, f"reply saved: {got}")
    expect(got[0]["to"] == ["alex.chen@example.com"] and got[0]["cc"] == ["priya@example.org"],
           f"reply goes to the original sender, plus cc: {got[0]}")
    expect(got[0]["subject"] == "Re: Lunch on Thursday?" and got[0]["content"] == "Thursday works.",
           f"Mail sets the subject; body replaces the quote: {got[0]}")
    expect(status(root, ok)["state"] == "created", "reply status")
    expect(status(root, gone)["state"] == "failed" and status(root, gone)["reason"] == "reply_message_not_found",
           f"missing message: {status(root, gone)}")
    expect(status(root, nobox)["reason"] == "reply_mailbox_not_found", f"missing mailbox: {status(root, nobox)}")


def test_refusals(env: Env) -> None:
    root, out, runner = env.setup()
    stranger = drafts.enqueue(root, GOOD | {"from": "mallory@example.net"})
    # A request written straight into the queue, skipping the MCP's checks.
    queue, _ = drafts.dirs(root)
    sneaky = "a" * 32
    (queue / f"{sneaky}.json").write_text(json.dumps(GOOD | {"id": sneaky, "to": ["x@example.com\nBcc: y@example.net"]}))
    mismatch = "b" * 32
    (queue / f"{mismatch}.json").write_text(json.dumps(GOOD | {"id": "c" * 32}))
    garbage = "d" * 32
    (queue / f"{garbage}.json").write_text("{not json")
    (queue / "notes.json").write_text("{}")
    stats = md.Helper(root, runner=runner).process()
    expect(stats.rejected == 3 and stats.failed == 1 and stats.created == 0, f"refusal stats {stats}")
    expect(status(root, stranger)["reason"] == "from_not_an_account", "from must be the user's own account")
    for request_id in (sneaky, mismatch, garbage):
        expect(status(root, request_id)["state"] == "rejected", f"{request_id[:4]} rejected")
    expect(saved(out) == [], "nothing refused reaches Mail")
    expect((queue / "notes.json").exists(), "files that aren't requests are left alone")


def test_rate_limit(env: Env) -> None:
    root, out, runner = env.setup()
    ids = [drafts.enqueue(root, GOOD) for _ in range(3)]
    for i, request_id in enumerate(ids):  # keep queue order stable
        os.utime(root / "queue" / f"{request_id}.json", (1000 + i, 1000 + i))
    helper = md.Helper(root, runner=runner, max_per_hour=2)
    now = time.time()
    stats = helper.process(now=now)
    expect(stats.created == 2 and stats.deferred == 1, f"first pass {stats}")
    expect(status(root, ids[2])["state"] == "queued", "the third waits in the queue")
    stats = helper.process(now=now + 60)
    expect(stats.created == 0 and stats.deferred == 1, f"still within the hour {stats}")
    stats = helper.process(now=now + 3601)
    expect(stats.created == 1 and len(saved(out)) == 3, f"next hour {stats}")


def test_errors(env: Env) -> None:
    root, out, runner = env.setup(deny=True)
    request_id = drafts.enqueue(root, GOOD)
    try:
        md.Helper(root, runner=runner).process()
    except jxa.AutomationDenied:
        pass
    else:
        fail("a refused Automation permission should raise AutomationDenied")
    expect(status(root, request_id)["state"] == "queued", "the request waits for the permission")

    root, out, runner = env.setup(fail_save=True)
    request_id = drafts.enqueue(root, GOOD)
    stats = md.Helper(root, runner=runner).process()
    expect(stats.failed == 1 and "-10000" in status(root, request_id)["reason"], f"save error: {status(root, request_id)}")


def test_doctor_and_pruning(env: Env) -> None:
    root, _, runner = env.setup()
    drafts.enqueue(root, GOOD)
    report = md.Helper(root, runner=runner).doctor()
    expect(report["sender_addresses"] == 2 and report["pending"] == 1, f"doctor: {report}")
    expect("sam@example.com" not in json.dumps(report), "doctor prints counts, not addresses")

    old = "e" * 32
    drafts.write_status(root, old, "created", now=0)
    path = root / "status" / f"{old}.json"
    os.utime(path, (0, 0))
    md.Helper(root, runner=runner).process()
    expect(not path.exists(), "old status files are pruned")


def main() -> int:
    test_script_cannot_send_or_read()
    test_validation()
    with tempfile.TemporaryDirectory(prefix="synthetic-mail-drafts-") as tmp:
        env = Env(Path(tmp))
        test_queue(env)
        if shutil.which("node") is None:
            print("SKIP mail drafts helper (node not found)")
        else:
            test_new_drafts(env)
            test_replies(env)
            test_refusals(env)
            test_rate_limit(env)
            test_errors(env)
            test_doctor_and_pruning(env)
    print("OK mail drafts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
