#!/usr/bin/env python3
"""Stdlib checks for the synthetic Messages store (scripts/lib/synthetic_messages.py).

On macOS with swiftc available it also decodes every attributedBody with
Foundation's own NSUnarchiver. Set SYNTHETIC_MESSAGES_SKIP_FOUNDATION=1 to skip
that step.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import synthetic_messages as sm  # noqa: E402
import typedstream as ts  # noqa: E402

# Produced by Foundation's NSArchiver on macOS 26 from the same made-up inputs.
GOLDEN_HELLO = bytes.fromhex(
    "040b73747265616d747970656481e803840140848484124e5341747472696275746564537472696e67"
    "008484084e534f626a656374008592848484084e53537472696e67019484012b0568656c6c6f8684"
    "0269490105928484840c4e5344696374696f6e617279009484016901928496961d5f5f6b494d4d65"
    "7373616765506172744174747269627574654e616d658692848484084e534e756d62657200848407"
    "4e5356616c7565009484012a84999900868686")
GOLDEN_MENTION = bytes.fromhex(
    "040b73747265616d747970656481e803840140848484124e5341747472696275746564537472696e67"
    "008484084e534f626a656374008592848484084e53537472696e67019484012b15486920f09f918b"
    "20416c657820616e64206d6f726586840269490106928484840c4e5344696374696f6e6172790094"
    "84016901928496961d5f5f6b494d4d657373616765506172744174747269627574654e616d658692"
    "848484084e534e756d626572008484074e5356616c7565009484012a849999008686970204928498"
    "9902928496961c5f5f6b494d4d656e74696f6e436f6e6669726d65644d656e74696f6e8692849696"
    "10616c6578406578616d706c652e636f6d869299929a8697030992849899019299929a8686")

SWIFT_DECODER = r"""
import Foundation
// Usage: decode <dir>. Prints one JSON line per *.bin file: text and runs.
let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".bin") }.sorted()
for name in files {
    let data = try Data(contentsOf: dir.appendingPathComponent(name))
    guard let s = NSUnarchiver.unarchiveObject(with: data) as? NSAttributedString else {
        print("{\"file\": \"\(name)\", \"error\": \"not an NSAttributedString\"}"); continue
    }
    var runs: [[String: Any]] = []
    s.enumerateAttributes(in: NSRange(location: 0, length: s.length)) { attrs, range, _ in
        var a: [String: String] = [:]
        for (k, v) in attrs { a[k.rawValue] = "\(v)" }
        runs.append(["length": range.length, "attrs": a])
    }
    let obj: [String: Any] = ["file": name, "text": s.string, "runs": runs]
    let json = try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    print(String(data: json, encoding: .utf8)!)
}
"""


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_typedstream_golden() -> None:
    part = {sm.PART: ts.Int32(0)}
    expect(ts.archive_attributed_string("hello", [(5, part)]) == GOLDEN_HELLO,
           "single run must match Foundation's bytes")
    text = "Hi \U0001F44B Alex and more"
    mention = {sm.MENTION: "alex@example.com", **part}
    mine = ts.archive_attributed_string(text, [(6, part), (4, mention), (ts.utf16_len(text) - 10, part)])
    expect(mine == GOLDEN_MENTION, "three runs with a mention must match Foundation's bytes")
    expect(ts.read_attributed_string(GOLDEN_MENTION)[0] == text, "reader must decode Foundation's archive")
    for n in (127, 128, 32767, 32768):  # one-byte, two-byte and four-byte lengths
        s = "z" * n
        expect(ts.read_attributed_string(ts.archive_attributed_string(s, [(n, part)]))[0] == s,
               f"round trip at length {n}")


def test_refuses_unsafe_roots(tmp: Path) -> None:
    for bad in (tmp / "store", Path.home() / "Library" / "synthetic-messages-x"):
        try:
            sm.build(bad)
        except ValueError:
            continue
        fail(f"build() accepted unsafe root {bad}")
    expect(not (tmp / "store").exists(), "refused root must not be created")


def test_refuses_real_looking_fixtures() -> None:
    bad = sm.Spec("bad", "alex", "alex", "2026-10-01T00:00:00+00:00", "call me on +14155551234")
    try:
        sm._check_fixtures([bad])
    except ValueError:
        return
    fail("a phone number outside 555-0100..0199 must be refused")


def test_store(root: Path, manifest: dict) -> None:
    db_path = root / manifest["chat_db"]
    db = ro(db_path)
    expect(db.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "chat.db must be in WAL mode")
    count = db.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    expect(count == len(manifest["messages"]) == len(sm.SPECS), "message count must match manifest")
    expect(db.execute("SELECT COUNT(*) FROM chat").fetchone()[0] == len(sm.CHATS), "chat count")
    rowids = [m["rowid"] for m in manifest["messages"]]
    expect(rowids == sorted(rowids), "rowids must follow date order")

    by_key = {m["key"]: m for m in manifest["messages"]}
    attachment_root = root / manifest["attachments_root"]
    for m in manifest["messages"]:
        row = db.execute(
            "SELECT text, attributedBody, is_from_me, handle_id, associated_message_guid,"
            " associated_message_type, message_summary_info, date_edited, date_retracted,"
            " thread_originator_guid, is_read, error FROM message WHERE ROWID = ?", (m["rowid"],)).fetchone()
        text, blob, from_me, handle_id, assoc, assoc_type, summary, edited, retracted, thread, is_read, error = row
        key = m["key"]
        expect(bool(from_me) == (m["from"] == "me"), f"{key}: is_from_me")
        expect((blob is not None) == m["has_attributed_body"], f"{key}: attributedBody presence")
        expect(text == (m["text"] if m["body"] in ("both", "text") else None), f"{key}: text column")
        if blob is not None:
            decoded, runs = ts.read_attributed_string(blob)
            expect(decoded == m["text"], f"{key}: decoded attributedBody must equal the text")
            expect(sum(n for n, _ in runs) == ts.utf16_len(decoded), f"{key}: run lengths")
            if m["mention"]:
                expect(any(a.get(sm.MENTION) == m["mention"] for _, a in runs), f"{key}: mention run")
            att_guids = [a.get(sm.FILE_GUID) for _, a in runs if sm.FILE_GUID in a]
            expect(att_guids == [a["guid"] for a in m["attachments"]], f"{key}: attachment runs")
        joined = db.execute("SELECT COUNT(*) FROM chat_message_join WHERE message_id = ?",
                            (m["rowid"],)).fetchone()[0]
        recoverable = db.execute("SELECT COUNT(*) FROM chat_recoverable_message_join WHERE message_id = ?",
                                 (m["rowid"],)).fetchone()[0]
        expect(joined == int(m["joined"]) and recoverable == int(m["recently_deleted"]),
               f"{key}: chat joins")
        if m["reaction_target"]:
            target = by_key[m["reaction_target"]]
            expect(assoc == f"p:0/{target['guid']}", f"{key}: associated_message_guid")
            expect(assoc_type == m["reaction_type"], f"{key}: associated_message_type")
        if m["thread"]:
            expect(thread == by_key[m["thread"]]["guid"], f"{key}: thread originator")
        if m["edited"]:
            info = plistlib.loads(summary)
            versions = [ts.read_attributed_string(v["t"])[0] for v in info["ec"]["0"]]
            expect(versions[-1] == m["text"] and len(versions) >= 2 and edited > 0,
                   f"{key}: edit history must end at the current text")
        if m["unsent"]:
            expect(blob is None and text is None and retracted > 0, f"{key}: unsent body must be gone")
            expect(plistlib.loads(summary)["rp"] == [0], f"{key}: retracted part")
        if m["from"] != "me":
            expect(bool(is_read) == m["read"], f"{key}: is_read")
        for att in m["attachments"]:
            filename = db.execute("SELECT filename FROM attachment WHERE guid = ?", (att["guid"],)).fetchone()[0]
            expect(filename == f"{manifest['attachment_prefix']}/{att['relpath']}",
                   f"{key}: attachment filename keeps Messages' ~ prefix")
            expect((attachment_root / att["relpath"]).is_file() == att["on_disk"], f"{key}: attachment file")

    expect(any(m["recently_deleted"] for m in manifest["messages"]), "a recently deleted message")
    expect(any(m["error"] for m in manifest["messages"]), "a failed send")
    expect(any(m["item_type"] for m in manifest["messages"]), "group events")
    long_blob = db.execute("SELECT attributedBody FROM message WHERE guid = ?",
                           (by_key["priya-long"]["guid"],)).fetchone()[0]
    expect(len(long_blob) > 32767, "one body must need a four-byte length")
    for chat in manifest["chats"]:
        members = db.execute(
            "SELECT h.id FROM chat c JOIN chat_handle_join j ON j.chat_id = c.ROWID"
            " JOIN handle h ON h.ROWID = j.handle_id WHERE c.guid = ? ORDER BY h.id",
            (chat["guid"],)).fetchall()
        expect([r[0] for r in members] == sorted(chat["members"]), f"{chat['key']}: members")
    db.close()

    ab = ro(root / manifest["contacts_db"])
    phones = {"+1" + "".join(c for c in p if c.isdigit())[-10:]
              for (p,) in ab.execute("SELECT ZFULLNUMBER FROM ZABCDPHONENUMBER")}
    emails = {e for (e,) in ab.execute("SELECT ZADDRESSNORMALIZED FROM ZABCDEMAILADDRESS")}
    ab.close()
    for handle in manifest["handles"]:
        known = handle["id"] in phones or handle["id"] in emails
        expect(known == (handle["key"] != "courier"), f"{handle['key']}: contact lookup")


def logical_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = str(path.relative_to(root))
        h.update(rel.encode())
        if path.suffix in (".db", ".abcddb"):
            db = ro(path)
            for line in db.iterdump():
                h.update(line.encode())
            db.close()
        elif not rel.endswith(("-wal", "-shm")):
            h.update(path.read_bytes())
    return h.hexdigest()


def test_reproducible(tmp: Path) -> None:
    a, b = tmp / "synthetic-messages-a", tmp / "synthetic-messages-b"
    sm.build(a)
    sm.build(b)
    expect(logical_digest(a) == logical_digest(b), "two builds must have the same content")


def test_append(tmp: Path) -> None:
    root = tmp / "synthetic-messages-append"
    sm.build(root)
    last = max(m["rowid"] for m in json.loads((root / "manifest.json").read_text())["messages"])
    spec = sm.Spec("alex-new", "alex", "alex", "2026-10-09T10:00:00+00:00", "Running 5 min late",
                   read=False)
    row = sm.append(root, spec, join=False)
    expect(row["rowid"] > last, "appended rowid must be higher than any before")
    db = ro(root / "Messages" / "chat.db")
    pending = db.execute("SELECT COUNT(*) FROM chat_message_join WHERE message_id = ?", (row["rowid"],)).fetchone()[0]
    db.close()
    expect(pending == 0, "join=False must leave the message without a chat join")
    sm.add_join(root, row["rowid"], "alex")
    db = ro(root / "Messages" / "chat.db")
    joined, reply_to = db.execute(
        "SELECT (SELECT COUNT(*) FROM chat_message_join WHERE message_id = m.ROWID), m.reply_to_guid"
        " FROM message m WHERE m.ROWID = ?", (row["rowid"],)).fetchone()
    db.close()
    expect(joined == 1, "add_join must attach the message to its chat")
    expect(reply_to is not None, "appended message points at the chat's previous message")


def test_foundation(root: Path, manifest: dict, tmp: Path) -> None:
    if os.environ.get("SYNTHETIC_MESSAGES_SKIP_FOUNDATION") or sys.platform != "darwin" or not shutil.which("swiftc"):
        print("skip Foundation cross-check (needs macOS and swiftc)")
        return
    work = tmp / "foundation"
    work.mkdir()
    (work / "decode.swift").write_text(SWIFT_DECODER)
    build = subprocess.run(["swiftc", "-O", "-o", str(work / "decode"), str(work / "decode.swift")],
                           capture_output=True, text=True)
    if build.returncode != 0:
        fail(f"swiftc failed: {build.stderr.strip()[:300]}")
    blobs = work / "blobs"
    blobs.mkdir()
    expected = {}
    db = ro(root / manifest["chat_db"])
    for m in manifest["messages"]:
        blob = db.execute("SELECT attributedBody FROM message WHERE ROWID = ?", (m["rowid"],)).fetchone()[0]
        if blob is None:
            continue
        name = f"{m['rowid']:04d}.bin"
        (blobs / name).write_bytes(blob)
        expected[name] = ts.read_attributed_string(blob)
    db.close()
    out = subprocess.run([str(work / "decode"), str(blobs)], capture_output=True, text=True, check=True)
    seen = 0
    for line in out.stdout.splitlines():
        got = json.loads(line)
        name = got["file"]
        expect("error" not in got, f"Foundation could not decode {name}")
        text, runs = expected[name]
        expect(got["text"] == text, f"{name}: Foundation decodes different text")
        mine = [(n, {k: str(v.value if isinstance(v, ts.Int32) else v) for k, v in a.items()})
                for n, a in runs]
        theirs = [(r["length"], r["attrs"]) for r in got["runs"]]
        merged = []  # Foundation merges adjacent runs with equal attributes
        for n, a in mine:
            if merged and merged[-1][1] == a:
                merged[-1] = (merged[-1][0] + n, a)
            else:
                merged.append((n, a))
        expect(theirs == merged, f"{name}: Foundation sees different attribute runs")
        seen += 1
    expect(seen == len(expected), "Foundation must decode every body")
    print(f"ok Foundation decoded {seen} attributedBody archives")


def main() -> int:
    test_typedstream_golden()
    test_refuses_real_looking_fixtures()
    with tempfile.TemporaryDirectory(prefix="msgfixture-test-") as tmp_name:
        tmp = Path(tmp_name)
        test_refuses_unsafe_roots(tmp)
        root = tmp / "synthetic-messages" / "store"
        manifest = sm.build(root)
        test_store(root, manifest)
        test_reproducible(tmp)
        test_append(tmp)
        test_foundation(root, manifest, tmp)
    print(f"ok synthetic Messages store: {len(sm.SPECS)} messages in {len(sm.CHATS)} chats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
