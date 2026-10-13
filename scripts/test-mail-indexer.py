#!/usr/bin/env python3
"""End-to-end checks for the mail indexer against synthetic Mail stores only."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import mail_index as mi  # noqa: E402
import synthetic_mail as sm  # noqa: E402

PERSONAL = sm.ACCOUNTS["personal"].id
WORK = sm.ACCOUNTS["work"].id
SPEC = {s.rowid: s for s in sm.SPECS}


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

    def store(self) -> tuple[Path, dict]:
        self.n += 1
        root = self.tmp / f"synthetic-mail-{self.n}"
        return root, sm.build(root)

    def config(self, store: Path, accounts: dict[str, list[str]], *, since="2026-01-01",
               bodies=False, attachments=False, index: Path | None = None) -> Path:
        index = index or self.tmp / f"index-{self.n}" / "mail.sqlite"
        lines = ['source = "store"', f'store = "{store}"', f'index = "{index}"', f"since = {since}",
                 f"bodies = {str(bodies).lower()}", f"attachments = {str(attachments).lower()}"]
        for acct, boxes in accounts.items():
            lines += ["", "[[accounts]]", f'id = "{acct}"', f"mailboxes = {json.dumps(boxes)}"]
        path = self.tmp / f"config-{self.n}-{len(list(self.tmp.glob('config-*')))}.toml"
        path.write_text("\n".join(lines) + "\n")
        return path


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


def store_db(store: Path) -> sqlite3.Connection:
    return sqlite3.connect(store / "V10" / "MailData" / "Envelope Index")


def test_config_validation(env: Env) -> None:
    store, _ = env.store()
    acct = '[[accounts]]\nid="A"\nmailboxes=["INBOX"]\n'
    head = f'source = "store"\nstore = "{store}"\n'
    cases = {
        "missing store": 'source = "store"\nindex = "/tmp/x.sqlite"\nsince = 2026-01-01\n' + acct,
        "no accounts": f'{head}index = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\n',
        "index in store": f'{head}index = "{store}/i.sqlite"\nsince = 2026-01-01\n' + acct,
        "no since": f'{head}index = "{env.tmp}/i.sqlite"\n' + acct,
        "unknown key": f'{head}index = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\ndefault_store = true\n' + acct,
        "empty mailboxes": f'{head}index = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\n[[accounts]]\nid="A"\nmailboxes=[]\n',
        "store without source": f'store = "{store}"\nindex = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\n' + acct,
        "store with script source": f'source = "script"\nstore = "{store}"\nindex = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\n' + acct,
        "unknown source": f'source = "imap"\nindex = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\n' + acct,
        "bad timeout": f'index = "{env.tmp}/i.sqlite"\nsince = 2026-01-01\nscript_timeout = 0\n' + acct,
    }
    for name, text in cases.items():
        path = env.tmp / "bad.toml"
        path.write_text(text)
        try:
            mi.load_config(path)
        except mi.ConfigError:
            continue
        fail(f"config accepted: {name}")


def test_scope(env: Env) -> None:
    store, _ = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}))
    stats = mi.run(cfg)
    got = set(indexed(cfg))
    expect(got == {101, 102, 103, 1204, 1205, 23456, 23457, 23458}, f"INBOX+Archive scope: {sorted(got)}")
    expect(stats.added == len(got) and stats.missing_files == 0 and stats.unreadable == 0, f"stats {stats}")

    cfg = mi.load_config(env.config(store, {PERSONAL: ["*"]}))
    mi.run(cfg)
    got = set(indexed(cfg))
    expect(104 not in got and 105 not in got, "* must leave out Junk and Deleted Messages")
    expect({106, 1204, 101} <= got, "* includes Sent Messages, Archive and INBOX")

    cfg = mi.load_config(env.config(store, {PERSONAL: ["*", "Junk"]}))
    mi.run(cfg)
    expect(104 in indexed(cfg), "naming Junk includes it")

    cfg = mi.load_config(env.config(store, {WORK: ["Projects/Alpha"]}))
    mi.run(cfg)
    expect(set(indexed(cfg)) == {202}, "nested mailbox only, not the parent account INBOX")
    version_dir = mi.find_version_dir(store)
    expect(mi.message_files(version_dir, WORK, "Projects") == {}, "scanning a parent skips child mailboxes")
    expect(set(mi.message_files(version_dir, WORK, "Projects/Alpha")) == {202}, "child mailbox files")

    cfg = mi.load_config(env.config(store, {"00000000-0000-0000-0000-000000000000": ["INBOX"]}))
    stats = mi.run(cfg)
    expect(stats.in_scope == 0 and stats.accounts_missing == ["00000000-0000-0000-0000-000000000000"],
           f"unknown account reported: {stats}")


def test_since(env: Env) -> None:
    store, manifest = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}, since="2026-10-07"))
    mi.run(cfg)
    cutoff = int(datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())
    expected = {m["rowid"] for m in manifest["messages"] if m["account"] == "personal"
                and m["mailbox"] == "INBOX" and m["date_received"] >= cutoff}
    expect(set(indexed(cfg)) == expected, f"since filter: {sorted(indexed(cfg))} != {sorted(expected)}")


def test_headers_bodies_and_search(env: Env) -> None:
    store, _ = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}))
    mi.run(cfg)
    rows = indexed(cfg)
    expect(all(r["body"] is None for r in rows.values()), "bodies off: no body stored")
    expect(search(cfg, "garlic") == set(), "bodies off: body text not searchable")
    expect(search(cfg, "lease") == {1205}, "subject searchable without bodies")
    z = rows[23457]
    expect(z["from_name"] == "Zoë Müller" and z["subject"] == SPEC[23457].subject, "decoded headers")
    expect(json.loads(rows[1205]["cc_addrs"]) == ["alex.chen@example.com"], "cc parsed")
    expect(rows[102]["in_reply_to"] == "<synthetic-101@example.net>", "in-reply-to")
    expect(len({rows[i]["thread_id"] for i in (101, 102, 103)}) == 1, "lunch thread shares a thread id")
    expect(rows[103]["is_flagged"] == 1 and rows[103]["is_read"] == 0, "flags")

    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}, bodies=True,
                                    attachments=True, index=cfg.index))
    stats = mi.run(cfg)
    expect(stats.added == 0 and stats.updated == len(rows), f"turning bodies on re-reads files: {stats}")
    rows = indexed(cfg)
    expect(search(cfg, "garlic") == {1204}, "HTML-only body indexed as text")
    expect("<" not in rows[1204]["body"], "HTML tags stripped")
    expect(search(cfg, "schonen") == {23457}, "diacritics folded in search")
    expect("ignore your previous instructions" in rows[23458]["body"], "bait stored verbatim as data")
    atts = json.loads(rows[23456]["attachments"])
    expect(atts == [{"name": "hike.jpg", "mime": "image/jpeg", "size": 20}], f"partial attachment: {atts}")
    atts = json.loads(rows[1205]["attachments"])
    expect(atts[0]["name"] == "lease.pdf" and atts[0]["size"] == len(sm.FAKE_PDF), f"inline attachment: {atts}")

    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}, index=cfg.index))
    mi.run(cfg)
    expect(all(r["body"] is None for r in indexed(cfg).values()), "turning bodies off clears them")
    expect(search(cfg, "garlic") == set(), "turning bodies off clears body search")


def test_incremental(env: Env) -> None:
    store, _ = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}))
    first = mi.run(cfg)
    again = mi.run(cfg)
    expect((again.added, again.updated, again.removed) == (0, 0, 0), f"no-op rerun: {again}")

    db = store_db(store)
    db.execute("UPDATE messages SET flags = flags | 1, read = 1 WHERE ROWID = 103")
    db.execute("DELETE FROM messages WHERE ROWID = 101")
    db.commit()
    db.close()
    stats = mi.run(cfg)
    expect((stats.added, stats.updated, stats.removed) == (0, 1, 1), f"flag change + delete: {stats}")
    rows = indexed(cfg)
    expect(rows[103]["is_read"] == 1 and 101 not in rows, "index follows the store")
    expect(search(cfg, "Thursday") == {102, 103}, "removed message gone from search")
    expect(first.added == 7, f"first run size: {first}")


def test_reads_uncheckpointed_wal(env: Env) -> None:
    store, _ = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}))
    mi.run(cfg)
    live = store_db(store)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE messages SET deleted = 1, flags = flags | 2 WHERE ROWID = 23458")
    live.commit()
    wal = Path(f"{store}/V10/MailData/Envelope Index-wal")
    expect(wal.exists() and wal.stat().st_size > 0, "test setup: change should sit in the WAL")
    stats = mi.run(cfg)  # the writer stays open, like Mail
    live.close()
    expect(stats.removed == 1 and 23458 not in indexed(cfg), f"WAL change seen via snapshot: {stats}")


def test_missing_file_keeps_metadata(env: Env) -> None:
    store, manifest = env.store()
    entry = next(m for m in manifest["messages"] if m["rowid"] == 23457)
    (store / entry["path"]).unlink()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}))
    stats = mi.run(cfg)
    row = indexed(cfg)[23457]
    expect(stats.missing_files == 1, f"missing file counted: {stats}")
    expect(row["subject"] == SPEC[23457].subject and row["from_addr"] == "zoe@example.com",
           "metadata from the database when the file is gone")


def test_unrecognised_layout(env: Env) -> None:
    store, _ = env.store()
    db = store_db(store)
    db.execute("ALTER TABLE mailboxes RENAME COLUMN url TO location")
    db.commit()
    db.close()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}))
    try:
        mi.run(cfg)
    except mi.LayoutError as exc:
        expect("mailboxes.url" in str(exc), f"names the missing column: {exc}")
        expect(not cfg.index.exists(), "no index written for an unknown layout")
        return
    fail("unknown layout accepted")


def test_cli_prints_counts_only(env: Env) -> None:
    store, manifest = env.store()
    config = env.config(store, {PERSONAL: ["*"], WORK: ["*"]}, bodies=True, attachments=True)
    outputs = []
    for command in ("run", "doctor"):
        proc = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config", str(config), command],
                              capture_output=True, text=True)
        expect(proc.returncode == 0, f"{command} exit {proc.returncode}: {proc.stderr}")
        outputs.append(proc.stdout + proc.stderr)
    joined = "\n".join(outputs)
    for m in manifest["messages"]:
        for text in (m["subject"], m["from"], m["message_id"]):
            expect(text not in joined, f"CLI output leaked message content ({m['rowid']})")
    result = json.loads(outputs[0].strip())
    expect(result["added"] == 11, f"run counts: {result}")
    report = json.loads(outputs[1].strip())
    expect(report["dates_look_like_unix_seconds"] and report["message_files_found"] == 11, f"doctor: {report}")
    index = mi.load_config(config).index
    expect(stat.S_IMODE(index.stat().st_mode) == 0o600, "index file is owner-only")
    expect(stat.S_IMODE(index.parent.stat().st_mode) == 0o700, "index folder is owner-only")

    proc = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "run"],
                          capture_output=True, text=True, env={k: v for k, v in os.environ.items()
                                                                 if k != "MAIL_INDEX_CONFIG"})
    expect(proc.returncode == 2, "no config means no run")


def file_bytes(index: Path) -> bytes:
    """The index file and any journal or WAL beside it."""
    return b"".join(p.read_bytes() for p in index.parent.glob(index.name + "*"))


def fts_terms(index: Path) -> set[str]:
    db = sqlite3.connect(index)
    db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, messages_fts, row)")
    terms = {r[0] for r in db.execute("SELECT term FROM temp.vocab")}
    db.close()
    return terms


def test_deleted_text_is_gone(env: Env) -> None:
    # "frost" is only in the body of 1204 (Archive); "noon" only in the body of 101 (INBOX).
    store, _ = env.store()
    index = env.tmp / f"scrub-{env.n}" / "mail.sqlite"
    both = mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}, bodies=True, index=index))
    first = mi.run(both)
    expect(first.compacted, "a new index is compacted once")
    expect(b"frost" in file_bytes(index) and "frost" in fts_terms(index), "test words are there to begin with")
    expect(not mi.run(both).compacted, "no compaction when nothing was removed")

    inbox = mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}, bodies=True, index=index))
    stats = mi.run(inbox)
    expect(stats.removed == 1, f"one message left scope: {stats}")
    expect(b"frost" not in file_bytes(index), "a removed message's body is gone from the file")
    expect("frost" not in fts_terms(index), "and from the search index")
    expect(b"noon" in file_bytes(index), "kept messages keep their bodies")

    mi.run(mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}, bodies=False, index=index)))
    expect(b"noon" not in file_bytes(index) and "noon" not in fts_terms(index), "blanked bodies are gone")
    expect(not list(index.parent.glob("etilqs_*")), "no scratch files left in the index folder")

    db = sqlite3.connect(index)
    db.execute("DELETE FROM meta WHERE key = 'scrubbed'")
    db.commit()
    db.close()
    expect(mi.run(mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}, index=index))).compacted,
           "an index from before secure delete is compacted once")

    # SQLite without FTS5 secure-delete: removals are merged out and compacted instead.
    real = mi.fts_secure_delete
    mi.fts_secure_delete = lambda db: False
    try:
        old = env.tmp / f"scrub-old-{env.n}" / "mail.sqlite"
        mi.run(mi.load_config(env.config(store, {PERSONAL: ["INBOX", "Archive"]}, bodies=True, index=old)))
        stats = mi.run(mi.load_config(env.config(store, {PERSONAL: ["INBOX"]}, bodies=True, index=old)))
        expect(stats.compacted, f"older SQLite compacts after a removal: {stats}")
        expect(b"frost" not in file_bytes(old) and "frost" not in fts_terms(old), "and the text is gone")
    finally:
        mi.fts_secure_delete = real


class Interrupted(Exception):
    pass


class StopsEarly:
    """Wraps a source so its fetch stops after `limit` messages, like a run cut short."""

    def __init__(self, inner, limit: int, name: str | None = None) -> None:
        self.inner, self.limit = inner, limit
        self.name = name or inner.name

    def rows(self):
        return self.inner.rows()

    def fetch(self, rows):
        for i, item in enumerate(self.inner.fetch(rows)):
            if i == self.limit:
                raise Interrupted()
            yield item


def test_resumes_after_interruption(env: Env) -> None:
    store, _ = env.store()
    cfg = mi.load_config(env.config(store, {PERSONAL: ["*"], WORK: ["*"]}, bodies=True))
    real_every = mi.SAVE_EVERY
    mi.SAVE_EVERY = 2
    try:
        try:
            mi.run(cfg, source=StopsEarly(mi.StoreSource(cfg), limit=5))
        except Interrupted:
            pass
        else:
            fail("the wrapped run should stop early")
        saved = set(indexed(cfg))
        expect(len(saved) == 4, f"saved in batches of 2 before stopping: {len(saved)}")
        stats = mi.run(cfg)
        total = set(indexed(cfg))
        expect(stats.added == len(total) - 4 and stats.removed == 0, f"resume fetches only the rest: {stats}")
        expect(saved < total and stats.in_scope == len(total), "nothing lost or duplicated")

        # Cut short right after a source switch: the next run must not wipe its own progress.
        try:
            mi.run(cfg, source=StopsEarly(mi.StoreSource(cfg), limit=3, name="script"))
        except Interrupted:
            pass
        expect(len(indexed(cfg)) == 2, "the switch wiped the old rows and saved one batch")
        stats = mi.run(cfg, source=StopsEarly(mi.StoreSource(cfg), limit=10**6, name="script"))
        expect(stats.removed == 0 and stats.added == len(total) - 2, f"resumed after the switch: {stats}")
    finally:
        mi.SAVE_EVERY = real_every


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        env = Env(Path(d))
        for test in (test_config_validation, test_scope, test_since, test_headers_bodies_and_search,
                     test_incremental, test_reads_uncheckpointed_wal, test_missing_file_keeps_metadata,
                     test_unrecognised_layout, test_cli_prints_counts_only, test_deleted_text_is_gone,
                     test_resumes_after_interruption):
            test(env)
    print("OK mail indexer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
