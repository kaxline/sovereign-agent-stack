#!/usr/bin/env python3
"""Checks for the IMAP source, against synthetic mail only. See docs/email.md.

Unit checks run everywhere. The end-to-end checks start a throwaway GreenMail
server in Docker, load the synthetic specs into it, and index it. They're
skipped (with a note) when Docker isn't running. No real account is used:
the server, its users and their passwords exist only for this run.
"""

from __future__ import annotations

import imaplib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import mail_imap  # noqa: E402
import mail_index as mi  # noqa: E402
import synthetic_mail as sm  # noqa: E402

IMAGE = "greenmail/standalone:2.1.3"
PASSWORD = "synthetic-pw"  # a throwaway user on a throwaway server
USERS = {"personal": sm.ME[1], "work": sm.WORK_ME[1]}
PASSWORD_ENV = "SYNTHETIC_IMAP_PASSWORD"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


# --------------------------------------------------------------------------- unit checks

def test_mutf7() -> None:
    for text in ("INBOX", "Café", "日本語/Notes", "R&D", "Ünïcödé & more", "~peter/mail/台北"):
        enc = mail_imap.encode_mutf7(text)
        expect(enc.isascii() and mail_imap.decode_mutf7(enc) == text, f"modified UTF-7 round trip: {text!r}")
    expect(mail_imap.decode_mutf7("Caf&AOk-") == "Café", "decodes a known name")
    expect(mail_imap.encode_mutf7("R&D") == "R&-D", "& is escaped as &-")


def test_parse_list() -> None:
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        b'(\\HasNoChildren \\Trash) "." "Papierkorb"',
        b'(\\HasNoChildren) "." "Projects.Alpha"',
        b'(\\HasNoChildren) "/" Archive',
        (b'(\\HasNoChildren) "/" {11}', b'Caf&AOk-/Q4'),
        b'(\\HasNoChildren) "/" "Quote \\"me\\""',
    ]
    boxes = {b.path: b for b in mail_imap.parse_list(data)}
    expect(set(boxes) == {"INBOX", "[Gmail]", "[Gmail]/All Mail", "[Gmail]/Spam", "Papierkorb",
                          "Projects/Alpha", "Archive", "Café/Q4", 'Quote "me"'}, f"LIST parsed: {sorted(boxes)}")
    expect(boxes["Projects/Alpha"].raw == "Projects.Alpha", "keeps the server's own name for EXAMINE")
    expect(not boxes["[Gmail]"].selectable, "\\Noselect isn't selectable")
    star = ["*", "[Gmail]/*"]
    picked = {p for p, b in boxes.items() if mail_imap.in_scope(b, star)}
    expect(picked == {"INBOX", "Archive", 'Quote "me"'},
           f"patterns skip junk, trash and virtual mailboxes by flag: {sorted(picked)}")
    expect(mail_imap.in_scope(boxes["Papierkorb"], ["Papierkorb"]), "naming trash includes it")
    expect(not mail_imap.in_scope(boxes["[Gmail]"], ["**"]), "unselectable mailboxes are never in scope")


def test_config() -> None:
    base = Path("/nonexistent")
    good = {"id": "work", "mailboxes": ["INBOX"], "source": "imap", "host": "imap.example.com",
            "user": "sam@example.com", "address": "sam@example.com"}
    s = mail_imap.account_settings(good, base, where="a")
    expect(s["port"] == 993 and s["security"] == "tls" and s["addresses"] == ["sam@example.com"], f"defaults: {s}")
    expect(mail_imap.account_settings(good | {"security": "starttls"}, base, where="a")["port"] == 143, "starttls port")
    bad = {
        "no host": {k: v for k, v in good.items() if k != "host"},
        "no user": {k: v for k, v in good.items() if k != "user"},
        "plain to a remote host": good | {"security": "plain"},
        "unknown security": good | {"security": "ssl3"},
        "password in config": good | {"password": "hunter2"},
        "bad port": good | {"port": 70000},
        "display name address": good | {"address": "Sam <sam@example.com>"},
        "bad env name": good | {"password_env": "lower case"},
    }
    for name, item in bad.items():
        try:
            mail_imap.account_settings(item, base, where="a")
        except mi.ConfigError:
            continue
        fail(f"imap account accepted: {name}")
    for host in ("127.0.0.1", "localhost", "::1"):
        mail_imap.account_settings(good | {"host": host, "security": "plain"}, base, where="a")


def test_reader_refuses_writes() -> None:
    conn = mail_imap.ReadOnlyImap("x", {})
    for items in ("(BODY[])", "(UID BODY[HEADER])", "(FLAGS) +FLAGS (\\Deleted)", "(UID FLAGS) STORE",
                  "(RFC822)", "(BODY.PEEK[]) (BODY[])"):
        try:
            conn.uid_fetch([1], items)
        except mail_imap.Refused:
            continue
        fail(f"reader allowed FETCH items {items!r}")
    for name in ("store", "append", "expunge", "copy", "move", "delete", "create", "rename", "uid"):
        expect(not hasattr(mail_imap.ReadOnlyImap, name), f"reader exposes {name}()")


def test_reader_source_reads_only() -> None:
    """A test, as for mail_script.js: the reader's code must not gain a write."""
    code = (LIB / "mail_imap.py").read_text()
    calls = r"\b_?imap\.(store|append|expunge|copy|move|delete|create|rename|subscribe|setacl|setquota|xatom)\("
    for pattern in (calls,
                    r"""['"](STORE|COPY|MOVE|EXPUNGE|APPEND|DELETE)['"]""", r"BODY\[\]"):
        hit = re.search(pattern, code)
        expect(hit is None, f"mail_imap.py gained a write: {hit.group(0) if hit else ''}")
    for line in code.splitlines():
        if ".select(" in line:
            expect("readonly=True" in line, f"SELECT without readonly=True: {line.strip()}")
    for m in re.finditer(r"\._imap\.uid\(\s*['\"](\w+)", code):
        expect(m.group(1) in ("SEARCH", "FETCH"), f"UID {m.group(1)} in the reader")


# --------------------------------------------------------------------------- GreenMail

def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@contextmanager
def greenmail():
    name = f"synthetic-mail-imap-{os.getpid()}"
    users = ",".join(f"{addr.split('@')[0]}:{PASSWORD}@{addr.split('@')[1]}" for addr in USERS.values())
    opts = (f"-Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users={users}"
            " -Dgreenmail.users.login=email")
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::3143",
                    "-e", f"GREENMAIL_OPTS={opts}", IMAGE], check=True, capture_output=True)
    try:
        port = int(subprocess.run(["docker", "port", name, "3143"], check=True, capture_output=True,
                                  text=True).stdout.strip().rsplit(":", 1)[1])
        deadline = time.time() + 60
        while True:
            try:
                imaplib.IMAP4("127.0.0.1", port, timeout=5).logout()
                break
            except (OSError, imaplib.IMAP4.error):
                if time.time() > deadline:
                    fail("GreenMail didn't start")
                time.sleep(0.5)
        yield port
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


def admin(port: int, account: str) -> imaplib.IMAP4:
    """A full-access test connection, for seeding and checking the server."""
    conn = imaplib.IMAP4("127.0.0.1", port, timeout=10)
    conn.login(USERS[account], PASSWORD)
    return conn


def server_name(path: str) -> str:
    return mail_imap.encode_mutf7(path.replace("/", "."))  # GreenMail separates levels with "."


def appended_at(epoch: int) -> str:
    """APPEND's date. GreenMail 2.1 reads a 12:xx hour as 00:xx, so write the
    same instant in a zone where the hour isn't 12."""
    for hours in (0, 1):
        when = datetime.fromtimestamp(epoch, timezone(timedelta(hours=hours)))
        if when.hour != 12:
            return imaplib.Time2Internaldate(when)
    raise AssertionError("unreachable")


def seed(port: int) -> None:
    for account in USERS:
        conn = admin(port, account)
        for spec in sm.SPECS:
            if spec.account != account:
                continue
            box = server_name(spec.mailbox)
            if box != "INBOX":
                conn.create(mail_imap.quote(box))
            flags = [f for bit, f in ((sm.FLAG_READ, "\\Seen"), (sm.FLAG_FLAGGED, "\\Flagged"),
                                      (sm.FLAG_ANSWERED, "\\Answered"), (sm.FLAG_DELETED, "\\Deleted"))
                     if spec.flags & bit]
            raw = sm.build_message(spec).as_bytes(policy=sm.POLICY.clone(linesep="\r\n"))
            typ, _ = conn.append(mail_imap.quote(box), f"({' '.join(flags)})",
                                 appended_at(spec.epoch), raw)
            expect(typ == "OK", f"seeding {spec.rowid}")
        conn.logout()


def server_state(port: int) -> dict:
    """Every message's flags in every mailbox (without \\Recent, which is per session)."""
    state = {}
    for account in USERS:
        conn = admin(port, account)
        for box in mail_imap.parse_list(conn.list()[1]):
            typ, _ = conn.select(mail_imap.quote(box.raw), readonly=True)
            if typ != "OK":
                expect(not box.selectable, f"test connection can't open {box.path}")
                continue
            typ, data = conn.uid("FETCH", "1:*", "(UID FLAGS)")
            for meta, _ in mail_imap.fetch_items(data or []):
                if meta and b"UID" in meta:
                    flags = mail_imap._flags(meta) - {"\\recent"}
                    state[(account, box.path, mail_imap._uid(meta))] = sorted(flags)
        conn.logout()
    return state


def write_config(tmp: Path, port: int, accounts: dict[str, list[str]], *, index: Path, name: str,
                 bodies=True, attachments=True, store: Path | None = None) -> Path:
    lines = [f'index = "{index}"', "since = 2026-01-01", f"bodies = {str(bodies).lower()}",
             f"attachments = {str(attachments).lower()}"]
    if store:
        lines.append(f'store = "{store}"')
    for key, boxes in accounts.items():
        if key.startswith("store:"):
            lines += ["", "[[accounts]]", f'id = "{key[6:]}"', 'source = "store"', f"mailboxes = {json.dumps(boxes)}"]
            continue
        lines += ["", "[[accounts]]", f'id = "{key}"', 'source = "imap"', 'host = "127.0.0.1"', f"port = {port}",
                  'security = "plain"', f'user = "{USERS[key]}"', f'address = "{USERS[key]}"',
                  f'password_env = "{PASSWORD_ENV}"', f"mailboxes = {json.dumps(boxes)}"]
    path = tmp / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def rows(index: Path) -> dict[str, sqlite3.Row]:
    db = sqlite3.connect(index)
    db.row_factory = sqlite3.Row
    out = {r["message_id"]: r for r in db.execute("SELECT * FROM messages")}
    db.close()
    return out


def test_end_to_end(tmp: Path, port: int) -> None:
    os.environ[PASSWORD_ENV] = PASSWORD
    seed(port)
    before = server_state(port)

    # Same scope through the IMAP source and through the synthetic Mail store.
    store = tmp / "synthetic-mail-store"
    sm.build(store)
    scope = {"personal": ["*"], "work": ["**"]}
    imap_cfg = mi.load_config(write_config(tmp, port, scope, index=tmp / "imap" / "index.sqlite", name="imap"))
    stats = mi.run(imap_cfg)
    store_cfg_path = tmp / "store.toml"
    store_cfg_path.write_text(
        f'source = "store"\nstore = "{store}"\nindex = "{tmp / "store" / "index.sqlite"}"\nsince = 2026-01-01\n'
        "bodies = true\nattachments = true\n"
        f'[[accounts]]\nid = "{sm.ACCOUNTS["personal"].id}"\nmailboxes = ["*"]\n'
        f'[[accounts]]\nid = "{sm.ACCOUNTS["work"].id}"\nmailboxes = ["**"]\n')
    mi.run(mi.load_config(store_cfg_path))
    got, want = rows(imap_cfg.index), rows(tmp / "store" / "index.sqlite")
    expect(set(got) == set(want), f"IMAP and store index the same messages: {sorted(set(got) ^ set(want))}")
    expect(stats.added == len(want) and stats.missing_files == 0 and stats.unreadable == 0, f"stats {stats}")
    for mid, w in want.items():
        g = got[mid]
        for col in ("mailbox", "subject", "from_addr", "from_name", "to_addrs", "cc_addrs", "is_read",
                    "is_flagged", "date_received", "date_sent", "in_reply_to", "body"):
            expect(g[col] == w[col], f"{mid} {col}: imap {g[col]!r} != store {w[col]!r}")
        names = [[a["name"] for a in json.loads(r["attachments"] or "[]")] for r in (g, w)]
        expect(names[0] == names[1], f"{mid}: attachment names {names}")
        expect(re.fullmatch(r"\d+\.\d+", g["source_id"]), f"source id is UIDVALIDITY.UID: {g['source_id']}")
    expect(got["<synthetic-106@example.net>"]["thread_id"] == got["<synthetic-1205@example.net>"]["thread_id"],
           "a reply joins its parent's thread")
    expect("Projects/Alpha" in {r["mailbox"] for r in got.values()}, "nested mailbox path uses /")
    expect(server_state(port) == before, "indexing changed nothing on the server (no flags, no deletes)")

    # Incremental: nothing new, then one new, one re-flagged, one deleted.
    stats = mi.run(imap_cfg)
    expect((stats.added, stats.updated, stats.removed) == (0, 0, 0), f"second run is a no-op: {stats}")
    top = max(r["rowid"] for r in rows(imap_cfg.index).values())
    conn = admin(port, "personal")
    new = sm.build_message(sm.Spec(rowid=999, account="personal", mailbox="INBOX", sender=("Avery Kim", "avery@example.com"),
                                   to=[sm.ME], subject="New on the server", date="Fri, 09 Oct 2026 09:00:00 +0000",
                                   text="Brand new."))
    conn.append("INBOX", "()", imaplib.Time2Internaldate(time.time()), new.as_bytes(policy=sm.POLICY.clone(linesep="\r\n")))
    conn.select("INBOX")
    _, data = conn.uid("SEARCH", "HEADER", "Message-ID", "<synthetic-23456@example.net>")
    conn.uid("STORE", data[0].split()[0], "+FLAGS", "(\\Seen)")
    _, data = conn.uid("SEARCH", "HEADER", "Message-ID", "<synthetic-23458@example.net>")
    conn.uid("STORE", data[0].split()[0], "+FLAGS", "(\\Deleted)")
    conn.expunge()
    conn.logout()
    stats = mi.run(imap_cfg)
    expect((stats.added, stats.updated, stats.removed) == (1, 1, 1), f"incremental changes: {stats}")
    now = rows(imap_cfg.index)
    expect(now["<synthetic-999@example.net>"]["rowid"] > top, "new mail gets a higher id")
    expect(now["<synthetic-23456@example.net>"]["is_read"] == 1, "read flag picked up")
    expect("<synthetic-23458@example.net>" not in now, "expunged message removed")

    # A mailbox recreated on the server gets a new UIDVALIDITY: re-indexed, not confused.
    conn = admin(port, "personal")
    conn.delete("Archive")
    conn.create("Archive")
    spec = next(s for s in sm.SPECS if s.mailbox == "Archive")
    conn.append("Archive", "(\\Seen)", appended_at(spec.epoch),
                sm.build_message(spec).as_bytes(policy=sm.POLICY.clone(linesep="\r\n")))
    conn.logout()
    old_sid = now[spec.message_id]["source_id"]
    stats = mi.run(imap_cfg)
    later = rows(imap_cfg.index)
    expect(stats.added == 1 and stats.removed == 1 and later[spec.message_id]["source_id"] != old_sid,
           f"UIDVALIDITY change re-indexes the mailbox: {stats}")

    # Mixed sources in one index: personal from the Mail store, work over IMAP.
    mixed = mi.load_config(write_config(
        tmp, port, {f"store:{sm.ACCOUNTS['personal'].id}": ["INBOX"], "work": ["**"]},
        index=tmp / "mixed" / "index.sqlite", name="mixed", store=store))
    stats = mi.run(mixed)
    m = rows(mixed.index)
    sources = {r["account"]: r["source_id"] for r in m.values()}
    expect(stats.added == len(m) == 9, f"mixed index: {stats}")
    expect(sources["work"].count(".") == 1 and sources[sm.ACCOUNTS["personal"].id].isdigit(),
           f"each account keeps its own source ids: {sources}")
    expect(len({r["rowid"] for r in m.values()}) == len(m), "ids unique across sources")

    # Switching an account to another source re-indexes just that account.
    db = sqlite3.connect(mixed.index)
    db.execute("UPDATE meta SET value = ? WHERE key = 'sources'",
               (json.dumps({sm.ACCOUNTS["personal"].id.upper(): "store", "WORK": "script"}),))
    db.commit()
    db.close()
    stats = mi.run(mixed)
    expect(stats.removed == 2 and stats.added == 2, f"only the switched account is rebuilt: {stats}")

    # Doctor and the CLI print counts, never content.
    report = mi.doctor(imap_cfg)
    expect(report["source"] == "imap" and report["accounts"][0]["details_readable"] > 0, f"doctor: {report}")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "run"], capture_output=True, text=True,
                         env={**os.environ, PASSWORD_ENV: PASSWORD})
    expect(out.returncode == 0, f"CLI run: {out.stderr}")
    for spec in sm.SPECS:
        expect(spec.subject not in out.stdout + out.stderr, "CLI output leaked a subject")
    expect(PASSWORD not in out.stdout + out.stderr, "CLI output leaked the password")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "run"], capture_output=True, text=True,
                         env={**os.environ, PASSWORD_ENV: "wrong"})
    expect(out.returncode == 4 and "rejected" in out.stderr and "wrong" not in out.stderr,
           f"a wrong password exits 4 without echoing it: {out.returncode} {out.stderr}")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "mailboxes"], capture_output=True, text=True)
    listing = json.loads(out.stdout)
    expect({"account": "work", "mailbox": "Projects/Alpha", "messages": 1, "special_use": None} in listing,
           f"mailboxes lists IMAP mailboxes with counts: {listing}")


def main() -> int:
    test_mutf7()
    test_parse_list()
    test_config()
    test_reader_refuses_writes()
    test_reader_source_reads_only()
    if not docker_ok():
        print("OK mail IMAP (unit checks only: Docker isn't running, GreenMail checks skipped)")
        return 0
    with tempfile.TemporaryDirectory() as d, greenmail() as port:
        test_end_to_end(Path(d), port)
    print("OK mail IMAP source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
