#!/usr/bin/env python3
"""Stdlib checks for the synthetic Apple Mail store (scripts/lib/synthetic_mail.py)."""

from __future__ import annotations

import email
import email.policy
import hashlib
import plistlib
import sqlite3
import sys
import tempfile
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import synthetic_mail as sm  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def read_emlx(path: Path) -> tuple[email.message.EmailMessage, dict]:
    data = path.read_bytes()
    first, rest = data.split(b"\n", 1)
    length = int(first.strip())
    raw, trailer = rest[:length], rest[length:]
    expect(trailer.startswith(b"<?xml"), f"{path.name}: plist trailer must follow the counted bytes")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return msg, plistlib.loads(trailer)


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        if path.name != "Envelope Index":  # SQLite pages may differ run to run
            h.update(path.read_bytes())
    return h.hexdigest()


def test_refuses_unsafe_roots(tmp: Path) -> None:
    for bad in (tmp / "store", Path.home() / "Library" / "synthetic-mail-x"):
        try:
            sm.build(bad)
        except ValueError:
            continue
        fail(f"build() accepted unsafe root {bad}")
    expect(not (tmp / "store").exists(), "refused root must not be created")


def test_fixture_addresses_are_example_domains() -> None:
    for spec in sm.SPECS:
        for _, addr in [spec.sender, *spec.to, *spec.cc]:
            expect(addr.rsplit("@", 1)[1] in sm.ALLOWED_DOMAINS, f"{spec.rowid}: {addr}")
    bad = sm.Spec(rowid=9, account="personal", mailbox="INBOX", sender=("X", "x@gmail.com"),
                  to=[sm.ME], subject="x", date="Thu, 01 Oct 2026 12:00:00 +0000", text="x")
    try:
        sm._check_spec(bad)
    except ValueError:
        return
    fail("a non-example address was accepted")


def test_messages_match_manifest(root: Path, manifest: dict) -> None:
    expect(len(manifest["messages"]) == len(sm.SPECS), "one manifest entry per spec")
    for entry in manifest["messages"]:
        path = root / entry["path"]
        expect(path.is_file(), f"missing {entry['path']}")
        expect(path.name.endswith(".partial.emlx") == entry["partial"], f"{path.name}: partial suffix")
        msg, plist = read_emlx(path)
        expect(msg["Message-ID"] == entry["message_id"], f"{entry['rowid']}: Message-ID")
        expect(str(msg["Subject"]) == entry["subject"], f"{entry['rowid']}: subject {msg['Subject']!r}")
        expect(msg["X-Synthetic-Fixture"] == "yes", f"{entry['rowid']}: synthetic marker")
        expect(plist["date-received"] == entry["date_received"], f"{entry['rowid']}: plist date")
        rowid = entry["rowid"]
        expected_dir = "Data" if rowid < 1000 else "Data/" + "/".join(reversed(str(rowid // 1000)))
        expect(f"/{expected_dir}/Messages/" in "/" + entry["path"], f"{rowid}: data dir {entry['path']}")
        for att in entry["attachments"]:
            if entry["partial"]:
                stored = root / att["path"]
                expect(stored.is_file(), f"{rowid}: attachment file {att['path']}")
                expect(hashlib.sha256(stored.read_bytes()).hexdigest() == att["sha256"], f"{rowid}: attachment bytes")
                part = msg.get_payload()[int(att["part"]) - 1]
                expect(part.get_content() in ("", b""), f"{rowid}: partial attachment body must be empty")
                expect(part["X-Apple-Content-Length"] is not None, f"{rowid}: X-Apple-Content-Length")
            else:
                parts = [p for p in msg.iter_attachments()]
                expect(len(parts) == 1, f"{rowid}: inline attachment count")
                data = parts[0].get_content()
                expect(hashlib.sha256(data).hexdigest() == att["sha256"], f"{rowid}: inline attachment bytes")


def test_non_ascii_round_trips(root: Path, manifest: dict) -> None:
    entry = next(m for m in manifest["messages"] if m["rowid"] == 23457)
    msg, _ = read_emlx(root / entry["path"])
    expect(msg["From"].addresses[0].display_name == "Zoë Müller", "decoded display name")
    expect("schönen" in msg.get_content(), "decoded quoted-printable body")


def test_envelope_index_agrees(root: Path, manifest: dict) -> None:
    db = sqlite3.connect(f"file:{root / 'V10/MailData/Envelope Index'}?mode=ro", uri=True)
    rows = {r[0]: r for r in db.execute(
        "SELECT m.ROWID, mb.url, m.subject_prefix || s.subject, a.address, g.message_id_header,"
        " m.date_received, m.flags, m.read, m.flagged, m.deleted, m.conversation_id"
        " FROM messages m JOIN mailboxes mb ON mb.ROWID = m.mailbox"
        " JOIN subjects s ON s.ROWID = m.subject JOIN addresses a ON a.ROWID = m.sender"
        " JOIN message_global_data g ON g.ROWID = m.global_message_id")}
    expect(len(rows) == len(manifest["messages"]), "one messages row per file")
    for e in manifest["messages"]:
        r = rows[e["rowid"]]
        expect(r[1:7] == (e["mailbox_url"], e["subject"], e["from"], e["message_id"],
                          e["date_received"], e["flags"]), f"{e['rowid']}: row {r}")
        expect(r[7] == int(bool(e["flags"] & sm.FLAG_READ)), f"{e['rowid']}: read column")
        expect(r[8] == int(bool(e["flags"] & sm.FLAG_FLAGGED)), f"{e['rowid']}: flagged column")
        expect(r[9] == int(bool(e["flags"] & sm.FLAG_DELETED)), f"{e['rowid']}: deleted column")
        expect(r[10] == e["conversation_id"], f"{e['rowid']}: conversation")
        recips = [a for (a,) in db.execute(
            "SELECT a.address FROM recipients r JOIN addresses a ON a.ROWID = r.address"
            " WHERE r.message = ? ORDER BY r.type, r.position", (e["rowid"],))]
        expect(recips == e["to"] + e["cc"], f"{e['rowid']}: recipients {recips}")
    thread = {e["conversation_id"] for e in manifest["messages"] if e["rowid"] in (101, 102, 103)}
    expect(len(thread) == 1, "lunch thread shares one conversation id")
    total, unread = db.execute("SELECT total_count, unread_count FROM mailboxes WHERE url LIKE '%/INBOX'"
                               " AND url LIKE ?", (f"imap://{sm.ACCOUNTS['personal'].id}/%",)).fetchone()
    inbox = [e for e in manifest["messages"] if e["account"] == "personal" and e["mailbox"] == "INBOX"]
    expect(total == len(inbox), "INBOX total_count")
    expect(unread == sum(1 for e in inbox if not e["flags"] & sm.FLAG_READ), "INBOX unread_count")
    db.close()


def test_output_is_reproducible(tmp: Path, first: Path) -> None:
    second = tmp / "synthetic-mail-again"
    sm.build(second)
    expect(tree_digest(first) == tree_digest(second), "two builds should produce identical files")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_refuses_unsafe_roots(tmp)
        test_fixture_addresses_are_example_domains()
        root = tmp / "synthetic-mail"
        manifest = sm.build(root)
        test_messages_match_manifest(root, manifest)
        test_non_ascii_round_trips(root, manifest)
        test_envelope_index_agrees(root, manifest)
        test_output_is_reproducible(tmp, root)
    print("OK synthetic mail store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
