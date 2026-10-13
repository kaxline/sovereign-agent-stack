#!/usr/bin/env python3
"""Checks for the Mail.app scripting source, against a fake Mail only.

mail_script.js runs under Node with fake_mail_app.js standing in for Mail,
fed from the synthetic specs. Nothing here runs osascript or talks to Mail.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import mail_index as mi  # noqa: E402
import mail_script as ms  # noqa: E402
import synthetic_mail as sm  # noqa: E402

PERSONAL = sm.ACCOUNTS["personal"].id
WORK = sm.ACCOUNTS["work"].id
FAKE = LIB / "fake_mail_app.js"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.n = 0

    def fake(self, fixture: dict | None = None) -> tuple[ms.Runner, dict, Path]:
        """A runner backed by a fixture file the test can edit between runs."""
        self.n += 1
        fixture = fixture if fixture is not None else sm.script_fixture()
        path = self.tmp / f"fixture-{self.n}.json"
        path.write_text(json.dumps(fixture))
        return ms.command_runner(["node", str(FAKE), str(path), str(ms.SCRIPT)], 60), fixture, path

    def config(self, accounts: dict[str, list[str]], *, since="2026-01-01", bodies=False,
               attachments=False, index: Path | None = None) -> mi.Config:
        self.n += 1
        index = index or self.tmp / f"index-{self.n}" / "mail.sqlite"
        lines = [f'index = "{index}"', f"since = {since}",
                 f"bodies = {str(bodies).lower()}", f"attachments = {str(attachments).lower()}"]
        for acct, boxes in accounts.items():
            lines += ["", "[[accounts]]", f'id = "{acct}"', f"mailboxes = {json.dumps(boxes)}"]
        path = self.tmp / f"config-{self.n}.toml"
        path.write_text("\n".join(lines) + "\n")
        return mi.load_config(path)


def run(config: mi.Config, runner: ms.Runner) -> mi.RunStats:
    return mi.run(config, source=ms.ScriptSource(config, runner))


def indexed(config: mi.Config) -> dict[int, sqlite3.Row]:
    db = sqlite3.connect(config.index)
    db.row_factory = sqlite3.Row
    rows = {r["rowid"]: r for r in db.execute("SELECT * FROM messages")}
    db.close()
    return rows


def search(config: mi.Config, query: str) -> set[int]:
    db = sqlite3.connect(config.index)
    hits = {r[0] for r in db.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (query,))}
    db.close()
    return hits


def find_message(fixture: dict, rowid: int) -> dict:
    def walk(nodes):
        for node in nodes:
            for m in node["messages"]:
                if m["id"] == rowid:
                    return node, m
            found = walk(node["children"])
            if found:
                return found
        return None
    for account in fixture["accounts"]:
        found = walk(account["mailboxes"])
        if found:
            return found
    raise KeyError(rowid)


def test_script_is_read_only() -> None:
    code = ms.SCRIPT.read_text()
    code = re.sub(r"//[^\n]*", "", code)  # comments may name what's absent
    banned = re.findall(r"\b(send|delete|move|duplicate|make|save|forward|reply|redirect|"
                        r"outgoingMessage|open|activate|quit|close|setProperty|doShellScript)\b"
                        r"|\.(set|delete)\s*\(", code, flags=re.IGNORECASE)
    expect(not banned, f"mail_script.js must only read, found: {banned}")


def test_mailboxes(env: Env) -> None:
    runner, _, _ = env.fake()
    cfg = env.config({PERSONAL: ["INBOX"]})
    accounts = ms.ScriptSource(cfg, runner).mailboxes(counts=True)
    by_id = {a["id"]: {b["path"]: b["count"] for b in a["mailboxes"]} for a in accounts}
    expect(set(by_id) == {PERSONAL, WORK}, f"accounts: {set(by_id)}")
    expect(by_id[WORK] == {"INBOX": 1, "Projects": 0, "Projects/Alpha": 1}, f"work mailboxes: {by_id[WORK]}")
    expect(by_id[PERSONAL]["INBOX"] == 7, f"personal INBOX count: {by_id[PERSONAL]}")

    fixture = sm.script_fixture()
    fixture["flatten"] = True
    runner, _, _ = env.fake(fixture)
    flat = ms.ScriptSource(cfg, runner).mailboxes(counts=False)
    paths = sorted(b["path"] for a in flat if a["id"] == WORK for b in a["mailboxes"])
    expect(paths == ["INBOX", "Projects", "Projects/Alpha"], f"flattened listing deduplicated: {paths}")


def test_scope(env: Env) -> None:
    runner, _, _ = env.fake()
    cfg = env.config({PERSONAL: ["INBOX", "Archive"]})
    stats = run(cfg, runner)
    got = set(indexed(cfg))
    expect(got == {101, 102, 103, 1204, 1205, 23456, 23457, 23458}, f"INBOX+Archive scope: {sorted(got)}")
    expect(stats.added == len(got) and stats.missing_files == 0 and stats.unreadable == 0, f"stats {stats}")

    cfg = env.config({PERSONAL: ["*"]})
    run(cfg, runner)
    got = set(indexed(cfg))
    expect(104 not in got and 105 not in got, "* must leave out Junk and Deleted Messages")
    expect({106, 1204, 101} <= got, "* includes Sent Messages, Archive and INBOX")

    cfg = env.config({PERSONAL: ["Deleted Messages"]})
    run(cfg, runner)
    expect(105 not in indexed(cfg), "messages marked deleted stay out even when their mailbox is named")

    cfg = env.config({WORK: ["Projects/Alpha"]})
    run(cfg, runner)
    expect(set(indexed(cfg)) == {202}, "nested mailbox only")

    cfg = env.config({WORK: ["INBOX"]}, since="2026-10-06")
    run(cfg, runner)
    expect(set(indexed(cfg)) == set(), "since leaves out older mail")

    cfg = env.config({"00000000-0000-0000-0000-000000000000": ["INBOX"]})
    stats = run(cfg, runner)
    expect(stats.in_scope == 0 and stats.accounts_missing == ["00000000-0000-0000-0000-000000000000"],
           f"unknown account reported: {stats}")


def test_lookups(env: Env) -> None:
    runner, _, _ = env.fake()
    calls: list[str] = []

    def recording(request: dict) -> dict:
        calls.append(request["command"])
        return runner(request)

    cfg = env.config({PERSONAL: ["inbox", "Nope"], WORK: ["projects/alpha", "Projects/Nope"]})
    stats = run(cfg, recording)
    expect("mailboxes" not in calls, f"exact names skip the full mailbox listing: {calls}")
    rows = indexed(cfg)
    expect(set(rows) >= {101, 202} and 1204 not in rows, f"looked up by path: {sorted(rows)}")
    expect(rows[101]["mailbox"] == "INBOX" and rows[202]["mailbox"] == "Projects/Alpha",
           "stored with Mail's own spelling")
    expect(stats.mailboxes_missing == 2, f"missing names counted: {stats}")

    calls.clear()
    stats = run(env.config({WORK: ["*"]}), recording)
    expect(calls.count("mailboxes") == 1, f"a pattern lists that account once: {calls}")

    stats = run(env.config({"00000000-0000-0000-0000-000000000000": ["INBOX"]}), runner)
    expect(stats.accounts_missing == ["00000000-0000-0000-0000-000000000000"] and stats.mailboxes_missing == 0,
           f"an unknown account isn't also counted as missing mailboxes: {stats}")

    fixture = sm.script_fixture()
    fixture["flatten"] = True
    flat, _, _ = env.fake(fixture)
    cfg = env.config({WORK: ["Projects/Alpha", "INBOX"]})
    run(cfg, flat)
    expect(set(indexed(cfg)) == {201, 202}, "path lookups work when Mail lists nested mailboxes at the top")


def test_content(env: Env) -> None:
    runner, _, _ = env.fake()
    cfg = env.config({PERSONAL: ["INBOX", "Archive"]}, bodies=True, attachments=True)
    run(cfg, runner)
    rows = indexed(cfg)
    expect(rows[23457]["subject"] == "Café recap ☕ — Grüße aus Köln", "RFC 2047 subject decoded")
    expect(rows[23457]["from_name"] == "Zoë Müller", f"sender name: {rows[23457]['from_name']}")
    expect(json.loads(rows[1205]["cc_addrs"]) == ["alex.chen@example.com"], "cc from headers")
    expect(rows[101]["message_id"] == "<synthetic-101@example.net>", "Message-ID from headers")
    threads = {rows[i]["thread_id"] for i in (101, 102, 103)}
    expect(len(threads) == 1 and None not in threads, f"thread from References: {threads}")
    expect(json.loads(rows[1205]["attachments"]) ==
           [{"name": "lease.pdf", "mime": "application/pdf", "size": len(sm.FAKE_PDF)}], "attachment names")
    expect(search(cfg, "garlic") == {1204}, "HTML-only body searchable")
    expect(search(cfg, "Grüße") == {23457}, "non-ASCII search")
    expect(rows[103]["is_flagged"] == 1 and rows[103]["is_read"] == 0, "flags from Mail")

    cfg = env.config({PERSONAL: ["INBOX"]}, bodies=False)
    run(cfg, runner)
    expect(all(r["body"] is None for r in indexed(cfg).values()), "no bodies unless enabled")

    small = env.config({PERSONAL: ["INBOX"]}, bodies=True)
    small.max_body_chars = 10
    run(small, runner)
    expect(all(len(r["body"] or "") <= 10 for r in indexed(small).values()), "max_body_chars")


def test_incremental(env: Env) -> None:
    runner, fixture, path = env.fake()
    cfg = env.config({PERSONAL: ["INBOX"]})
    first = run(cfg, runner)
    again = run(cfg, runner)
    expect(again.added == 0 and again.updated == 0 and again.removed == 0, f"no-op re-run: {again}")

    _, m = find_message(fixture, 103)
    m["read"] = True
    node, _ = find_message(fixture, 23458)
    node["messages"] = [x for x in node["messages"] if x["id"] != 23458]
    path.write_text(json.dumps(fixture))
    stats = run(cfg, runner)
    expect(stats.updated == 1 and stats.removed == 1 and stats.added == 0, f"flag change and removal: {stats}")
    expect(indexed(cfg)[103]["is_read"] == 1, "read flag updated")
    expect(first.in_scope - 1 == stats.in_scope, "scope shrank by one")

    _, m = find_message(fixture, 101)
    m["headers"] = None
    path.write_text(json.dumps(fixture))
    cfg2 = env.config({PERSONAL: ["INBOX"]})
    stats = run(cfg2, runner)
    expect(stats.missing_files == 1 and 101 in indexed(cfg2), f"message Mail can't produce is still indexed from its summary: {stats}")


def test_mail_changing_during_list(env: Env) -> None:
    fixture = sm.script_fixture()
    newcomer = sm.script_message(sm.Spec(
        rowid=999, account="work", mailbox="INBOX", sender=("Jordan Lee", "jordan.lee@example.org"),
        to=[sm.WORK_ME], subject="Arrived mid-read", date="Sat, 10 Oct 2026 09:00:00 +0000",
        text="Hello.\n"))
    fixture["arrive"] = {"account": WORK, "path": "INBOX", "message": newcomer}
    runner, _, _ = env.fake(fixture)
    cfg = env.config({WORK: ["INBOX"]})
    run(cfg, runner)
    rows = indexed(cfg)
    expect(set(rows) == {201, 999}, f"re-read after mail arrived: {sorted(rows)}")
    expect(rows[201]["subject"] == "Q4 planning doc" and rows[999]["subject"] == "Arrived mid-read",
           "summaries stay aligned with their ids")


def test_errors(env: Env) -> None:
    fixture = sm.script_fixture()
    fixture["deny"] = True
    runner, _, _ = env.fake(fixture)
    cfg = env.config({PERSONAL: ["INBOX"]})
    try:
        run(cfg, runner)
    except ms.AutomationDenied:
        pass
    else:
        fail("a refused Automation permission should raise AutomationDenied")

    broken = ms.command_runner(["node", "-e", "process.stderr.write('Error: Lunch on Thursday? (-1728)');"
                                " process.exit(1)"], 10)
    try:
        run(cfg, broken)
    except ms.ScriptError as exc:
        expect("-1728" in str(exc) and "Lunch" not in str(exc), f"error names the code only: {exc}")
    else:
        fail("a failing script should raise ScriptError")


def test_doctor(env: Env) -> None:
    runner, _, _ = env.fake()
    cfg = env.config({PERSONAL: ["INBOX"], "00000000-0000-0000-0000-000000000000": ["INBOX"]})
    report = mi.doctor(cfg, source=ms.ScriptSource(cfg, runner))
    expect(report["source"] == "script" and report["messages_in_scope"] == 7, f"doctor counts: {report}")
    expect(report["details_sampled"] == 7 and report["details_readable"] == 7, f"doctor sample: {report}")
    expect(report["accounts_missing"] == ["00000000-0000-0000-0000-000000000000"], "doctor missing account")
    text = json.dumps(report)
    for spec in sm.SPECS:
        expect(spec.subject not in text and spec.sender[1] not in text, "doctor must print no message content")


def test_switching_source(env: Env) -> None:
    store_root = env.tmp / "synthetic-mail-store"
    sm.build(store_root)
    index = env.tmp / "switch" / "mail.sqlite"
    store_cfg_path = env.tmp / "store.toml"
    store_cfg_path.write_text(f'source = "store"\nstore = "{store_root}"\nindex = "{index}"\n'
                              f'since = 2026-01-01\n[[accounts]]\nid = "{PERSONAL}"\nmailboxes = ["INBOX"]\n')
    store_cfg = mi.load_config(store_cfg_path)
    mi.run(store_cfg)
    from_store = indexed(store_cfg)

    runner, _, _ = env.fake()
    script_cfg = env.config({PERSONAL: ["INBOX"]}, index=index)
    stats = run(script_cfg, runner)
    expect(stats.removed == len(from_store) and stats.added == len(from_store), f"switch rebuilds: {stats}")
    from_script = indexed(script_cfg)
    expect(set(from_script) == set(from_store), "both sources index the same messages")
    for rowid, row in from_store.items():
        for col in ("subject", "from_addr", "message_id", "is_read", "is_flagged", "date_received"):
            expect(row[col] == from_script[rowid][col], f"{rowid} {col}: store and script disagree")
    again = mi.run(store_cfg)
    expect(again.removed == len(from_store), "switching back rebuilds too")


def main() -> int:
    if shutil.which("node") is None:
        print("SKIP mail script source (node not found)")
        return 0
    test_script_is_read_only()
    with tempfile.TemporaryDirectory(prefix="synthetic-mail-script-") as tmp:
        env = Env(Path(tmp))
        test_mailboxes(env)
        test_scope(env)
        test_lookups(env)
        test_content(env)
        test_incremental(env)
        test_mail_changing_during_list(env)
        test_errors(env)
        test_doctor(env)
        test_switching_source(env)
    print("OK mail script source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
