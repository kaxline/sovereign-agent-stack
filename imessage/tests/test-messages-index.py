#!/usr/bin/env python3
"""Checks for the Messages indexer (lib/messages_index.py) against the synthetic store."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import messages_index as mi  # noqa: E402
import synthetic_messages as sm  # noqa: E402
import typedstream as ts  # noqa: E402

CLI = HERE.parent / "messages-index.py"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def write_config(path: Path, store: Path, index: Path, *, chats=("*",), exclude=(), since="2026-01-01",
                 contacts: Path | None = None, attachments=True, extra="") -> Path:
    lines = [f'store = "{store}"', f'index = "{index}"', f"since = {since}",
             f"chats = {json.dumps(list(chats))}", f"exclude = {json.dumps(list(exclude))}",
             f"attachments = {'true' if attachments else 'false'}"]
    if contacts:
        lines.append(f'contacts = "{contacts}"')
    path.write_text("\n".join(lines) + "\n" + extra)
    return path


def store_state(store: Path) -> dict[str, str]:
    """File names and hashes in the Messages folder (excluding attachments)."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(store.iterdir()) if p.is_file()}


def rows(index: Path, sql: str, args=()) -> list[sqlite3.Row]:
    db = sqlite3.connect(index)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def test_config_errors(tmp: Path, store: Path) -> None:
    cases = {
        "no store": 'index = "x.sqlite"\nsince = 2026-01-01\nchats = ["*"]\n',
        "empty store": 'store = ""\nindex = "x.sqlite"\nsince = 2026-01-01\nchats = ["*"]\n',
        "no chats": f'store = "{store}"\nindex = "x.sqlite"\nsince = 2026-01-01\n',
        "no since": f'store = "{store}"\nindex = "x.sqlite"\nchats = ["*"]\n',
        "index in store": f'store = "{store}"\nindex = "{store}/index.sqlite"\nsince = 2026-01-01\nchats = ["*"]\n',
        "unknown key": f'store = "{store}"\nindex = "x.sqlite"\nsince = 2026-01-01\nchats = ["*"]\nsend = true\n',
    }
    for name, text in cases.items():
        path = tmp / f"bad-{name.replace(' ', '-')}.toml"
        path.write_text(text)
        try:
            mi.load_config(path)
        except mi.ConfigError:
            continue
        fail(f"config with {name} must be refused")
    example = HERE.parent / "messages-index.toml.example"
    try:
        mi.load_config(example)
    except mi.ConfigError as exc:
        expect("store" in str(exc), "the example config must fail on its empty store first")
    else:
        fail("the example config must not load as-is")


def expected_body(m: dict, max_chars: int) -> str | None:
    if m["item_type"] or m["unsent"] or m["text"] is None:
        return None
    text = m["text"].replace(sm.OBJECT_REPLACEMENT, "").strip()[:max_chars]
    return text or None


def test_full_run(tmp: Path, root: Path, manifest: dict) -> Path:
    store = root / "Messages"
    index = tmp / "index-full" / "index.sqlite"
    cfg = mi.load_config(write_config(tmp / "full.toml", store, index, contacts=root / "AddressBook"))
    before = store_state(store)
    stats = mi.run(cfg).as_dict()
    expect(store_state(store) == before, "indexing must not change or add files in the Messages folder")

    in_window = [m for m in manifest["messages"] if m["date"] >= mi.since_bounds(cfg.since)[0]]
    reactions = [m for m in in_window if m["reaction_type"]]
    deleted = [m for m in in_window if m["recently_deleted"]]
    messages = [m for m in in_window if not m["reaction_type"] and not m["recently_deleted"]]
    expect(stats["in_scope"] == stats["added"] == len(messages), f"in_scope/added: {stats}")
    expect(stats["reactions"] == len(reactions) == 3, f"reactions: {stats}")
    expect(stats["recently_deleted_skipped"] == len(deleted) == 1, f"recently deleted: {stats}")
    expect(stats["events"] == 3 and stats["unsent"] == 1 and stats["edited"] == 1, f"kinds: {stats}")
    expect(stats["decode_failed_used_text"] == 0 and stats["attributes_incomplete"] == 0, f"decoding: {stats}")
    expect(stats["chats_in_scope"] == len(sm.CHATS), "every chat in scope")
    expect(not any(m["key"] == "alex-legacy" for m in in_window), "since must exclude the 2024 message")

    mode = stat.S_IMODE(index.stat().st_mode)
    expect(mode == 0o600 and stat.S_IMODE(index.parent.stat().st_mode) == 0o700, "index permissions")

    indexed = {r["guid"]: r for r in rows(index, "SELECT * FROM messages")}
    expect(set(indexed) == {m["guid"] for m in messages}, "indexed messages must match the scope")
    for m in messages:
        r = indexed[m["guid"]]
        key = m["key"]
        expect(r["source_rowid"] == m["rowid"], f"{key}: source rowid")
        expect(r["is_from_me"] == (m["from"] == "me"), f"{key}: is_from_me")
        if m["item_type"]:
            expect(r["kind"] == "event" and r["body"], f"{key}: event with a description")
        elif m["unsent"]:
            expect(r["kind"] == "unsent" and r["body"] is None, f"{key}: unsent has no body")
        else:
            expect(r["kind"] == "message" and r["body"] == expected_body(m, cfg.max_body_chars), f"{key}: body")
        expect(r["attachment_count"] == len(m["attachments"]), f"{key}: attachment count")
        if m["attachments"]:
            listed = json.loads(r["attachments"])
            expect([a["name"] for a in listed] == [a["name"] for a in m["attachments"]], f"{key}: attachment names")
            expect([a["on_disk"] for a in listed] == [a["on_disk"] for a in m["attachments"]], f"{key}: on_disk")
        expect(bool(r["failed"]) == bool(m["error"]), f"{key}: failed send")
        if m["mention"]:
            expect(json.loads(r["mentions"]) == [m["mention"]], f"{key}: mentions")
        if m["edited"]:
            expect(r["edited"] == 1 and r["edit_count"] == 1, f"{key}: edit count")

    by_key = {m["key"]: indexed.get(m["guid"]) for m in messages}
    expect(by_key["alex-sure"]["sender_name"] == "Alex Chen", "phone handle resolves to a contact name")
    expect(by_key["priya-congrats"]["sender"] == "me", "from-me sender")
    expect(by_key["priya-injection"]["sender_name"] == "Priya Natarajan", "email handle resolves")
    expect(by_key["courier-package"]["sender_name"] is None, "unknown number has no name")
    expect(by_key["garden-rtl"]["sender_name"] == "María Gómez", "a number typed without formatting resolves")
    expect(by_key["garden-added"]["body"] == "Jordan Lee added María Gómez", "membership event text")
    expect(by_key["garden-removed"]["body"] == "You removed Jordan Lee", "removal event text")
    expect(by_key["garden-named"]["body"] == "You named the conversation “Garden club”", "rename text")
    expect(by_key["priya-long"]["body_truncated"] == 1 and len(by_key["priya-long"]["body"]) == cfg.max_body_chars,
           "long bodies are cut at max_body_chars")
    expect(by_key["alex-thread"]["thread_guid"] == next(m["guid"] for m in messages if m["key"] == "alex-sure"),
           "thread originator kept")

    reacts = {r["kind"]: r for r in rows(index, "SELECT * FROM reactions")}
    lunch = next(m["guid"] for m in messages if m["key"] == "alex-lunch")
    expect(reacts["love"]["target_guid"] == lunch, "reaction points at its target")
    expect(sorted(r["removed"] for r in rows(index, "SELECT removed FROM reactions WHERE kind='love'")) == [0, 1],
           "a heart and its removal")
    expect(reacts["emoji"]["emoji"] == "\U0001F525", "emoji reaction keeps its emoji")

    chats = {r["guid"]: r for r in rows(index, "SELECT * FROM chats")}
    garden = chats[sm.CHATS["garden"].guid]
    expect(garden["display_name"] == "Garden club" and garden["is_group"] == 1, "group chat row")
    names = [p["name"] for p in json.loads(garden["participants"])]
    expect(names == ["Alex Chen", "Jordan Lee", "María Gómez"], f"participant names: {names}")

    hits = rows(index, "SELECT m.guid FROM messages_fts f JOIN messages m ON m.id = f.rowid"
                       " WHERE messages_fts MATCH 'ramen'")
    expect([h["guid"] for h in hits] == [by_key["alex-thread"]["guid"]], "full-text search on bodies")
    hits = rows(index, "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH 'sender_text:priya'")
    expect(hits[0]["n"] >= 3, "full-text search on sender names")
    hits = rows(index, "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH 'chat_text:garden'")
    expect(hits[0]["n"] >= 5, "full-text search on chat names")

    again = mi.run(cfg).as_dict()
    expect(again["added"] == again["updated"] == again["removed"] == 0, f"second run must change nothing: {again}")
    return index


def test_reader_never_writes_store(tmp: Path) -> None:
    """While Messages runs, new rows can sit in the -wal file. The indexer must
    read them but never change chat.db or the WAL (no checkpoint, no writes)."""
    root = tmp / "synthetic-messages-wal" / "store"
    sm.build(root)
    store = root / "Messages"
    writer = sqlite3.connect(store / "chat.db")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    spec = sm.Spec("alex-wal2", "alex", "alex", "2026-10-09T09:30:00+00:00", "Fresh off the WAL")
    sm._insert_message(writer, root, spec, None, {s.key: s.guid for s in sm.SPECS} | {spec.key: spec.guid})
    writer.commit()
    main = hashlib.sha256((store / "chat.db").read_bytes()).hexdigest()
    wal = hashlib.sha256((store / "chat.db-wal").read_bytes()).hexdigest()
    cfg = mi.load_config(write_config(tmp / "wal.toml", store, tmp / "index-wal" / "index.sqlite"))
    mi.run(cfg)
    mi.doctor(cfg)
    expect(hashlib.sha256((store / "chat.db").read_bytes()).hexdigest() == main, "chat.db unchanged")
    expect(hashlib.sha256((store / "chat.db-wal").read_bytes()).hexdigest() == wal, "WAL unchanged (no checkpoint)")
    found = rows(cfg.index, "SELECT body FROM messages WHERE guid = ?", (spec.guid,))
    expect(found and found[0]["body"] == "Fresh off the WAL", "the WAL row is indexed")
    writer.close()


def test_incremental(tmp: Path) -> None:
    root = tmp / "synthetic-messages-incr" / "store"
    sm.build(root)
    index = tmp / "index-incr" / "index.sqlite"
    cfg = mi.load_config(write_config(tmp / "incr.toml", root / "Messages", index))
    mi.run(cfg)

    pending = sm.append(root, sm.Spec("alex-new", "alex", "alex", "2026-10-09T10:00:00+00:00",
                                      "Running 5 min late", read=False), join=False)
    stats = mi.run(cfg).as_dict()
    expect(stats["added"] == 0, "a message with no chat yet must wait")
    sm.add_join(root, pending["rowid"], "alex")
    stats = mi.run(cfg).as_dict()
    expect(stats["added"] == 1, f"the message is indexed once it has a chat: {stats}")

    # Messages edits a row in place: new body, date_edited set.
    db = sqlite3.connect(root / "Messages" / "chat.db")
    new_blob = ts.archive_attributed_string("Running 10 min late", [(19, {sm.PART: ts.Int32(0)})])
    db.execute("UPDATE message SET attributedBody = ?, date_edited = date + 30000000000 WHERE ROWID = ?",
               (new_blob, pending["rowid"]))
    db.commit()
    db.close()
    stats = mi.run(cfg).as_dict()
    expect(stats["updated"] == 1, f"an edit updates the row: {stats}")
    row = rows(index, "SELECT body, edited FROM messages WHERE guid = ?", (pending["guid"],))[0]
    expect(row["body"] == "Running 10 min late" and row["edited"] == 1, "edited body and flag")
    hits = rows(index, "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH '\"10 min\"'")
    expect(hits[0]["n"] == 1, "full-text index follows the edit")
    raw = index.read_bytes()
    expect(b"Running 5 min late" not in raw, "the pre-edit text is overwritten in the index file")
    expect(rows(index, "SELECT value FROM meta WHERE key = 'scrubbed'"), "the index records its first compaction")

    # Narrow the scope: 1:1 chats only, minus the SMS one.
    narrow = mi.load_config(write_config(tmp / "narrow.toml", root / "Messages", index,
                                         chats=["*;-;*"], exclude=[sm.CHATS["courier"].guid]))
    stats = mi.run(narrow).as_dict()
    expect(stats["removed"] > 0 and stats["chats_in_scope"] == 2, f"narrower scope removes rows: {stats}")
    chats = {r["guid"] for r in rows(index, "SELECT guid FROM chats")}
    expect(chats == {sm.CHATS["alex"].guid, sm.CHATS["priya"].guid}, "only scoped chats remain")
    left = {r["chat_guid"] for r in rows(index, "SELECT DISTINCT chat_guid FROM messages")}
    expect(left <= chats, "no messages from chats out of scope")
    reaction_chats = {r["chat_guid"] for r in rows(index, "SELECT DISTINCT chat_guid FROM reactions")}
    expect(reaction_chats <= chats, "no reactions from chats out of scope")


def test_doctor_and_chats(tmp: Path, root: Path, manifest: dict) -> None:
    cfg = mi.load_config(write_config(tmp / "doc.toml", root / "Messages", tmp / "doc" / "index.sqlite",
                                      chats=["*;-;*"], contacts=root / "AddressBook"))
    report = mi.doctor(cfg)
    expect(report["dates_look_like_nanoseconds"] is True, "dates check")
    expect(report["journal_mode"] == "wal", "journal mode reported")
    expect(report["attributed_body_failed"] == 0 and report["attributed_body_decoded"] > 0, "decode counts")
    expect(report["messages_total"] == len(manifest["messages"]), "total count")
    expect(report["messages_without_chat"] == 1, "the recently deleted message has no chat join")
    expect(report["chats_total"] == 5 and report["chats_in_scope"] == 3, f"chat counts: {report}")
    expect(report["contact_handles"] >= 5, "contacts loaded")
    text = json.dumps(report)
    for m in manifest["messages"]:
        if m["text"] and len(m["text"]) > 6:
            expect(m["text"] not in text, "doctor output must not contain message text")
    listing = {c["id"]: c for c in mi.list_chats(cfg)}
    expect(listing[sm.CHATS["alex"].guid]["participants"] == ["Alex Chen"], "chats lists contact names")
    expect(listing[sm.CHATS["garden"].guid]["in_scope"] is False, "chats marks scope")


def test_cli(tmp: Path, root: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "MESSAGES_INDEX_CONFIG"}
    out = subprocess.run([sys.executable, "-I", str(CLI), "run"], capture_output=True, text=True, env=env)
    expect(out.returncode == 2, "no config is exit code 2")
    copy = tmp / "synthetic-messages-locked" / "store"
    sm.build(copy)
    cfg = write_config(tmp / "locked.toml", copy / "Messages", tmp / "locked" / "index.sqlite")
    chat_db = copy / "Messages" / "chat.db"
    chat_db.chmod(0)
    try:
        if os.access(chat_db, os.R_OK):  # running as root: permissions don't apply
            print("skip permission check (running as root)")
        else:
            out = subprocess.run([sys.executable, "-I", str(CLI), "--config", str(cfg), "run"],
                                 capture_output=True, text=True, env=env)
            expect(out.returncode == 4, f"unreadable store is exit code 4, got {out.returncode}: {out.stderr}")
            expect("Full Disk Access" in out.stderr, "exit 4 explains Full Disk Access")
    finally:
        chat_db.chmod(0o644)
    out = subprocess.run([sys.executable, "-I", str(CLI), "--config", str(cfg), "run"],
                         capture_output=True, text=True, env=env)
    expect(out.returncode == 0 and json.loads(out.stdout)["added"] > 0, "CLI run prints counts")
    db = sqlite3.connect(copy / "Messages" / "chat.db")
    db.execute("ALTER TABLE message RENAME COLUMN is_from_me TO from_me_flag")
    db.commit()
    db.close()
    out = subprocess.run([sys.executable, "-I", str(CLI), "--config", str(cfg), "doctor"],
                         capture_output=True, text=True, env=env)
    expect(out.returncode == 3 and "message.is_from_me" in out.stderr, "unknown layout is exit code 3")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="msgindex-test-") as tmp_name:
        tmp = Path(tmp_name)
        root = tmp / "synthetic-messages" / "store"
        manifest = sm.build(root)
        test_config_errors(tmp, root / "Messages")
        test_full_run(tmp, root, manifest)
        test_reader_never_writes_store(tmp)
        test_incremental(tmp)
        test_doctor_and_chats(tmp, root, manifest)
        test_cli(tmp, root)
    print("ok messages indexer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
