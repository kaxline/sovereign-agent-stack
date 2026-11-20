"""Session export and import for scripts/sessions.sh, run inside the hermes
container (python from /opt/hermes/.venv) as the hermes user.

    sessions_move.py profiles          profile names, one per line
    sessions_move.py export NAME       NAME's sessions as JSON lines on stdout
    sessions_move.py import NAME       JSON lines from stdin into NAME's store
    sessions_move.py manifest          manifest.json from stdin -> TSV lines

NAME is `default` for the store at the Hermes home, or a directory under
profiles/. export uses SessionDB.export_all(include_inactive=True), the shape
`hermes sessions export --format jsonl` writes: every stored row with its
active/compacted flags, so compaction-archived turns come back archived.

import calls SessionDB.import_sessions in batches. One call writes nothing
when any entry in it is invalid or was deleted here, so ids are checked first:
present ids are skipped, ids deleted here are refused, and sessions over
Hermes' per-session limits are reported as too_large. Parents go before their
children, so a child whose parent landed in an earlier batch still attaches.

Titles are unique in a store, and import_sessions inserts them unchanged, so
one clash raises and rolls back the whole call. A session whose title the
target or an earlier session in the import already holds gets the suffix
Hermes' own import paths use, " (<last 12 characters of the id>)", and is
counted as renamed once imported. A batch that still raises is retried one
session at a time, so only the sessions that raise again are failed.

Only counts and session ids reach stdout; titles and message text never do.
Anything Hermes prints while working goes to stderr.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Below Hermes' 25 MB per-call cap: each batch holds the write lock for one
# transaction, and a live gateway waits on it.
BATCH_BYTES = 8 * 1024 * 1024
SUMMARY_TAG = "@@sessions_move "


def _real_stdout():
    """A writer on the original stdout, after pointing fd 1 at stderr so log
    output from Hermes modules can't mix with the data or result lines."""
    fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    return os.fdopen(fd, "w", encoding="utf-8")


def _db_path(name):
    if name == "default":
        return HOME / "state.db"
    if "/" in name or name in ("", ".", ".."):
        raise SystemExit(f"bad profile name: {name!r}")
    return HOME / "profiles" / name / "state.db"


def _profile_exists(name):
    return name == "default" or (HOME / "profiles" / name).is_dir()


def cmd_profiles():
    names = ["default"]
    root = HOME / "profiles"
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for name in names:
        print(name)


def cmd_export(name):
    out = _real_stdout()
    path = _db_path(name)
    sessions = messages = 0
    if path.is_file():
        from hermes_state import SessionDB
        db = SessionDB(db_path=path, read_only=True)
        try:
            for row in db.export_all(include_inactive=True):
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                sessions += 1
                messages += len(row.get("messages") or [])
        finally:
            db.close()
    out.flush()
    print(SUMMARY_TAG + json.dumps({"sessions": sessions, "messages": messages}, separators=(",", ":")), file=sys.stderr)


def _measure(raw):
    # As SessionDB._validate_import_session measures it: without `timings`.
    measured = {k: v for k, v in raw.items() if k != "timings"}
    return len(json.dumps(measured, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _parents_first(items):
    """items: [(id, raw)] -> same items, each parent in the payload before its children."""
    by_id = dict(items)
    done, ordered = set(), []

    def visit(sid, trail):
        if sid in done or sid in trail:
            return
        trail.add(sid)
        parent = str(by_id[sid].get("parent_session_id") or "").strip()
        if parent in by_id:
            visit(parent, trail)
        done.add(sid)
        ordered.append((sid, by_id[sid]))

    for sid, _ in items:
        visit(sid, set())
    return ordered


def _free_title(title, sid, taken, max_len):
    """TITLE, or with the id-tail suffix (cut to fit MAX_TITLE_LENGTH, as
    import_moved_session does) when TAKEN already holds it."""
    if not isinstance(title, str) or title not in taken:
        return title
    suffix = f" ({sid[-12:]})"
    renamed = title[:max_len - len(suffix)] + suffix
    if renamed in taken:  # the suffixed title is taken too: fall back to the whole id
        suffix = f" ({sid})"
        renamed = title[:max(0, max_len - len(suffix))] + suffix
    return renamed


def _batches(items, db):
    batch, nbytes, nmsgs = [], 0, 0
    for sid, raw, size in items:
        count = len(raw.get("messages") or [])
        if batch and (len(batch) >= db._IMPORT_MAX_SESSIONS or nbytes + size > BATCH_BYTES
                      or nmsgs + count > db._IMPORT_MAX_TOTAL_MESSAGES):
            yield batch
            batch, nbytes, nmsgs = [], 0, 0
        batch.append((sid, raw))
        nbytes += size
        nmsgs += count
    if batch:
        yield batch


def cmd_import(name):
    out = _real_stdout()
    result = {"phase": "profile", "profile": name, "sessions": 0, "imported": 0, "skipped": 0,
              "detached": 0, "renamed": 0, "refused": 0, "too_large": 0, "invalid": 0, "failed": 0,
              "refused_ids": [], "too_large_ids": [], "invalid_ids": [], "failed_ids": [],
              "renamed_ids": []}

    def add(kind, sid):
        result[kind] += 1
        if sid:
            result[kind + "_ids"].append(sid)

    if not _profile_exists(name):
        raise SystemExit(f"profile {name} does not exist")
    rows, seen = [], set()
    for line in sys.stdin:
        if not line.strip():
            continue
        result["sessions"] += 1
        try:
            raw = json.loads(line)
            sid = str(raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        except ValueError:
            raw, sid = None, ""
        if not sid or sid in seen:
            add("invalid", sid)
            continue
        seen.add(sid)
        rows.append((sid, raw))

    from hermes_state import SessionDB
    from hermes_state_mutation_retirement import retired_in
    db = SessionDB(db_path=_db_path(name))
    try:
        pending = []
        with db._read_ctx() as conn:
            taken = {r[0] for r in conn.execute("SELECT title FROM sessions WHERE title IS NOT NULL")}
            for sid, raw in rows:
                if conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (sid,)).fetchone():
                    result["skipped"] += 1
                elif retired_in(conn, sid):
                    add("refused", sid)
                else:
                    pending.append((sid, raw))
        sized, renamed = [], set()
        for sid, raw in _parents_first(pending):
            title = _free_title(raw.get("title"), sid, taken, db.MAX_TITLE_LENGTH)
            clash = title != raw.get("title")
            if clash:
                raw = {**raw, "title": title}
            try:
                size = _measure(raw)
            except (TypeError, ValueError):
                add("invalid", sid)
                continue
            if (size > db._IMPORT_MAX_SESSION_BYTES
                    or len(raw.get("messages") or []) > db._IMPORT_MAX_MESSAGES_PER_SESSION):
                add("too_large", sid)
                continue
            if isinstance(title, str):
                taken.add(title)
            if clash:
                renamed.add(sid)
            sized.append((sid, raw, size))
        for batch in _batches(sized, db):
            _import_batch(db, batch, result, add, renamed)
    finally:
        db.close()
    out.write(json.dumps(result, separators=(",", ":")) + "\n")
    out.flush()


def _import_batch(db, batch, result, add, renamed=frozenset()):
    """import_sessions on BATCH; entries it rejects as invalid are dropped and
    the rest retried once. A batch of more than one session that raises is
    retried one session at a time. Any other refusal fails the batch. Ids in
    RENAMED that get imported are counted as renamed."""
    for attempt in (1, 2):
        try:
            res = db.import_sessions([raw for _, raw in batch])
        except Exception as exc:  # sqlite lock timeout, a cap we mismeasured, a constraint
            print(f"import batch failed: {type(exc).__name__}", file=sys.stderr)
            if len(batch) > 1:
                for item in batch:
                    _import_batch(db, [item], result, add, renamed)
                return
            for sid, _ in batch:
                add("failed", sid)
            return
        if res.get("ok"):
            result["imported"] += res.get("imported", 0)
            result["skipped"] += res.get("skipped", 0)
            result["detached"] += res.get("detached", 0)
            for sid in res.get("imported_ids") or []:
                if sid in renamed:
                    add("renamed", sid)
            return
        bad = {err["index"] for err in res.get("errors") or [] if isinstance(err, dict) and "index" in err}
        if attempt == 2 or not bad or len(bad) != len(res.get("errors") or []):
            break
        for i in sorted(bad):
            add("invalid", batch[i][0])
        batch = [item for i, item in enumerate(batch) if i not in bad]
        if not batch:
            return
    for sid, _ in batch:
        add("failed", sid)


def cmd_manifest():
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or data.get("format") != "assistant-sessions/1":
        raise SystemExit("not a sessions export manifest")
    for entry in data.get("profiles") or []:
        name, file = str(entry.get("name") or ""), str(entry.get("file") or "")
        if not name or "/" in name or "\t" in name or "/" in file or not file.endswith(".jsonl"):
            raise SystemExit("bad profile entry in manifest")
        print(f"{name}\t{file}\t{int(entry.get('sessions') or 0)}")


def main(argv):
    if argv[:1] == ["profiles"]:
        cmd_profiles()
    elif argv[:1] == ["manifest"]:
        cmd_manifest()
    elif len(argv) == 2 and argv[0] == "export":
        cmd_export(argv[1])
    elif len(argv) == 2 and argv[0] == "import":
        cmd_import(argv[1])
    else:
        raise SystemExit("usage: sessions_move.py profiles|manifest|export NAME|import NAME")


if __name__ == "__main__":
    main(sys.argv[1:])
