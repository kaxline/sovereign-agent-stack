#!/usr/bin/env bash
# scripts/sessions.sh export and import between two synthetic Hermes homes, in
# throwaway containers of the pinned image (SESSIONS_PY): ids, message counts,
# parent links and archived rows survive; a profile the target lacks is
# reported; deleted, oversized and malformed entries are counted; a title the
# target or an earlier session already holds gets Hermes' id-tail suffix and
# is counted as renamed; a batch that raises is retried one session at a time;
# a second import imports nothing; the manifest names each profile's session
# ids, and an import's id lists hold every one of them once (CR 0039 R6);
# output never carries titles or message text.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
# shellcheck source=lib/hermes-image.sh
. "${ROOT}/scripts/lib/hermes-image.sh"
hermes_image_gate "${1:-}" "sessions.sh"

tmp="$(mktemp -d)"
# The image writes as the hermes user; on a Linux bind mount the caller could
# not delete those files, so hand everything back before removing it.
cleanup() {
  [ -z "${KEEP:-}" ] || return 0
  docker run --rm -u 0 -v "${tmp}:/t" --entrypoint chown "$HERMES_IMAGE" -R "$(id -u):$(id -g)" /t \
    > /dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT
src="${tmp}/src"
dst="${tmp}/dst"
mkdir -p "${src}/profiles/coder" "${src}/profiles/writer" "${dst}/profiles/coder"
chmod -R 777 "$src" "$dst"

py_in() { # py_in HOME: python in the image with HOME as the Hermes home
  printf 'docker run --rm -i -u hermes -v %s:/opt/data -w /opt/hermes --entrypoint /opt/hermes/.venv/bin/python %s' \
    "$1" "$HERMES_IMAGE"
}

# Seed both homes. Titles and text carry a marker the output must never show.
$(py_in "$src") - <<'PY' || fail "seeding the source home"
import json, sys, time
sys.path.insert(0, "/opt/hermes")
from pathlib import Path
from hermes_state import SessionDB

def sess(sid, n, parent=None, archived=0):
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"SECRET-TEXT {sid} {i}",
             "timestamp": time.time() - 100 + i} for i in range(n)]
    for m in msgs[:archived]:
        m.update(active=0, compacted=1)
    s = {"id": sid, "source": "cli", "title": f"SECRET-TITLE {sid}", "started_at": time.time() - 200,
         "messages": msgs}
    if parent:
        s["parent_session_id"] = parent
    return s

def seed(path, sessions):
    db = SessionDB(db_path=Path(path))
    res = db.import_sessions(sessions)
    assert res["ok"] and res["imported"] == len(sessions), res
    db.close()

seed("/opt/data/state.db", [sess("s-parent", 3), sess("s-child", 2, parent="s-parent"),
                            sess("s-arch", 4, archived=2), sess("s-deleted", 2)])
seed("/opt/data/profiles/coder/state.db", [sess("c-1", 2), sess("c-2", 5)])
seed("/opt/data/profiles/writer/state.db", [sess("w-1", 1)])
PY

$(py_in "$dst") - <<'PY' || fail "seeding the target home"
import sys, time
sys.path.insert(0, "/opt/hermes")
from pathlib import Path
from hermes_state import SessionDB
from hermes_state_mutation_retirement import retired_session
db = SessionDB(db_path=Path("/opt/data/state.db"))
assert db.import_sessions([{"id": "s-deleted", "source": "cli", "started_at": time.time(),
                            "messages": [{"role": "user", "content": "x"}]}])["ok"]
db.delete_session("s-deleted")
assert retired_session(db, "s-deleted"), "delete_session did not fence the id"
# A local chat whose title the source's s-parent also has (CR 0039 R5).
assert db.import_sessions([{"id": "t-local", "source": "cli", "title": "SECRET-TITLE s-parent",
                            "started_at": time.time(), "messages": [{"role": "user", "content": "x"}]}])["ok"]
PY

out="${tmp}/out"
SESSIONS_PY="$(py_in "$src")" "${ROOT}/scripts/sessions.sh" export --dest "$out" --json \
  > "${tmp}/export.jsonl" 2> "${tmp}/export.err" || { cat "${tmp}/export.err" >&2; fail "export exited non-zero"; }
grep -qxF '{"phase":"profile","profile":"default","sessions":4,"messages":11}' "${tmp}/export.jsonl" \
  || fail "export default line: $(cat "${tmp}/export.jsonl")"
grep -qxF '{"phase":"profile","profile":"coder","sessions":2,"messages":7}' "${tmp}/export.jsonl" \
  || fail "export coder line"
grep -q '^{"phase":"done","action":"export",.*"sessions":7,"messages":19}$' "${tmp}/export.jsonl" \
  || fail "export done line: $(tail -1 "${tmp}/export.jsonl")"
{ [ -f "${out}/manifest.json" ] && [ -f "${out}/writer.jsonl" ]; } || fail "export files missing"
python3 - "${out}/manifest.json" <<'PY' || fail "manifest session_ids: $(cat "${out}/manifest.json")"
import json, sys
ids = {p["name"]: p["session_ids"] for p in json.load(open(sys.argv[1]))["profiles"]}
assert sorted(ids["default"]) == ["s-arch", "s-child", "s-deleted", "s-parent"], ids
assert sorted(ids["coder"]) == ["c-1", "c-2"] and ids["writer"] == ["w-1"], ids
PY
[ "$(stat -c %a "${out}/default.jsonl" 2> /dev/null || stat -f %Lp "${out}/default.jsonl")" = 600 ] \
  || fail "export file is not mode 600"
SESSIONS_PY="$(py_in "$src")" "${ROOT}/scripts/sessions.sh" export --dest "$out" --json > /dev/null 2>&1 \
  && fail "export into a non-empty directory succeeded"

# An oversized session, a malformed line, and a session whose title s-arch,
# earlier in the same import, already takes, beside the real ones.
{
  $(py_in "$src") -c 'import json; print(json.dumps({"id": "s-huge", "messages": [{"role": "user", "content": "x"}] * 10001}))'
  printf 'not json\n'
  $(py_in "$src") -c 'import json, time; print(json.dumps({"id": "s-dup", "source": "cli", "title": "SECRET-TITLE s-arch", "started_at": time.time(), "messages": [{"role": "user", "content": "SECRET-TEXT dup"}]}))'
} >> "${out}/default.jsonl"

SESSIONS_PY="$(py_in "$dst")" "${ROOT}/scripts/sessions.sh" import --from "$out" --json \
  > "${tmp}/import.jsonl" 2> "${tmp}/import.err" || { cat "${tmp}/import.err" >&2; fail "import exited non-zero"; }
grep -q '^{"phase":"profile","profile":"default","sessions":7,"imported":4,"skipped":0,"detached":0,"renamed":2,"refused":1,"too_large":1,"invalid":1,"failed":0,"imported_ids":\[[^]]*\],"skipped_ids":\[\],"refused_ids":\["s-deleted"\],"too_large_ids":\["s-huge"\],"invalid_ids":\[\],"failed_ids":\[\],"renamed_ids":\["s-parent","s-dup"\]}$' \
  "${tmp}/import.jsonl" || fail "import default line: $(cat "${tmp}/import.jsonl")"
grep -q '^{"phase":"profile","profile":"coder","sessions":2,"imported":2,' "${tmp}/import.jsonl" \
  || fail "import coder line"
grep -qxF '{"phase":"profile","profile":"writer","sessions":1,"status":"profile_missing"}' "${tmp}/import.jsonl" \
  || fail "writer not reported profile_missing"
grep -q '^{"phase":"done","action":"import","imported":6,"skipped":0,"detached":0,"renamed":2,"refused":1,"too_large":1,"invalid":1,"failed":0,"profile_missing":\["writer"\]}$' "${tmp}/import.jsonl" \
  || fail "import done line: $(tail -1 "${tmp}/import.jsonl")"
[ ! -e "${dst}/profiles/writer" ] || fail "import created the missing profile"
# id_lists IMPORT_JSONL PROFILE IMPORTED SKIPPED: the profile line lists those
# ids (comma-separated, any order), and every manifest id of every imported
# profile is in exactly one of its six id lists.
id_lists() {
  python3 - "${out}/manifest.json" "$@" <<'PY'
import json, sys
manifest = {p["name"]: p["session_ids"] for p in json.load(open(sys.argv[1]))["profiles"]}
rows = [json.loads(line) for line in open(sys.argv[2])]
for row in rows:
    if row.get("phase") != "profile" or "status" in row:
        continue
    held = [sid for k in ("imported_ids", "skipped_ids", "refused_ids", "too_large_ids",
                          "invalid_ids", "failed_ids") for sid in row[k]]
    assert len(held) == len(set(held)), (row["profile"], held)
    assert not set(manifest[row["profile"]]) - set(held), (row["profile"], held)
    if row["profile"] == sys.argv[3]:
        want = lambda arg: sorted(filter(None, arg.split(",")))
        assert sorted(row["imported_ids"]) == want(sys.argv[4]), row
        assert sorted(row["skipped_ids"]) == want(sys.argv[5]), row
PY
}
id_lists "${tmp}/import.jsonl" default "s-arch,s-child,s-dup,s-parent" "" \
  || fail "import id lists: $(head -1 "${tmp}/import.jsonl")"
if grep -q 'SECRET-' "${tmp}/export.jsonl" "${tmp}/import.jsonl" "${tmp}/export.err" "${tmp}/import.err"; then
  fail "output carries titles or message text"
fi

# The target holds the same rows as the source. src and dst are mounted on
# their own: $tmp is the caller's mode-700 directory, closed to the hermes user.
docker run --rm -i -u hermes -v "${src}:/opt/data/src" -v "${dst}:/opt/data/dst" -w /opt/hermes \
  --entrypoint /opt/hermes/.venv/bin/python "$HERMES_IMAGE" - <<'PY' || fail "target rows differ from the source"
import sqlite3
def rows(path):
    c = sqlite3.connect(path)
    s = {r[0]: r[1:] for r in c.execute("SELECT id, message_count, parent_session_id, title FROM sessions")}
    m = {r[0]: r[1:] for r in c.execute("SELECT session_id, COUNT(*), SUM(active = 0) FROM messages GROUP BY session_id")}
    return s, m
for name, keep in (("state.db", {"s-parent", "s-child", "s-arch"}), ("profiles/coder/state.db", {"c-1", "c-2"})):
    s1, m1 = rows(f"/opt/data/src/{name}")
    s2, m2 = rows(f"/opt/data/dst/{name}")
    for sid in keep:
        assert s1[sid][:2] == s2[sid][:2], (name, sid, s1[sid], s2[sid])
        assert m1[sid] == m2[sid], (name, sid, m1[sid], m2[sid])
s2, m2 = rows("/opt/data/dst/state.db")
assert s2["s-child"][1] == "s-parent", s2["s-child"]
assert s2["s-child"][2] == "SECRET-TITLE s-child", s2["s-child"]
assert s2["t-local"][2] == "SECRET-TITLE s-parent", s2["t-local"]
assert s2["s-parent"][2] == "SECRET-TITLE s-parent (s-parent)", s2["s-parent"]
assert s2["s-arch"][2] == "SECRET-TITLE s-arch", s2["s-arch"]
assert s2["s-dup"][2] == "SECRET-TITLE s-arch (s-dup)", s2["s-dup"]
assert m2["s-arch"] == (4, 2), m2["s-arch"]
assert "s-deleted" not in s2 and "s-huge" not in s2
PY

SESSIONS_PY="$(py_in "$dst")" "${ROOT}/scripts/sessions.sh" import --from "$out" --json \
  > "${tmp}/again.jsonl" 2> /dev/null || fail "second import exited non-zero"
grep -q '^{"phase":"done","action":"import","imported":0,"skipped":6,"detached":0,"renamed":0,' "${tmp}/again.jsonl" \
  || fail "second import done line: $(tail -1 "${tmp}/again.jsonl")"
id_lists "${tmp}/again.jsonl" default "" "s-arch,s-child,s-dup,s-parent" \
  || fail "second import id lists: $(head -1 "${tmp}/again.jsonl")"

# A batch that raises is retried one session at a time, so only the session
# that raises again is failed. A stub store stands in for SessionDB.
docker run --rm -i -u hermes -v "${ROOT}/scripts/lib:/lib-stack:ro" -w /opt/hermes \
  --entrypoint /opt/hermes/.venv/bin/python "$HERMES_IMAGE" - <<'PY' || fail "batch retry"
import sqlite3, sys
sys.path.insert(0, "/lib-stack")
import sessions_move as sm

class Stub:
    calls = []
    def import_sessions(self, sessions):
        ids = [s["id"] for s in sessions]
        self.calls.append(ids)
        if "bad" in ids:
            raise sqlite3.IntegrityError("UNIQUE constraint failed")
        return {"ok": True, "imported": len(ids), "skipped": 0, "detached": 0, "imported_ids": ids}

result = {k: 0 for k in ("imported", "skipped", "detached", "renamed", "invalid", "failed")}
result.update(imported_ids=[], skipped_ids=[], invalid_ids=[], failed_ids=[], renamed_ids=[])
def add(kind, sid):
    result[kind] += 1
    result[kind + "_ids"].append(sid)
db = Stub()
batch = [(sid, {"id": sid}) for sid in ("a", "bad", "c")]
sm._import_batch(db, batch, result, add, renamed={"a", "bad"})
assert db.calls == [["a", "bad", "c"], ["a"], ["bad"], ["c"]], db.calls
assert result["imported"] == 2 and result["failed_ids"] == ["bad"], result
assert result["renamed_ids"] == ["a"], result
PY

SESSIONS_PY="$(py_in "$dst")" "${ROOT}/scripts/sessions.sh" import --from "${tmp}/nowhere" --json > "${tmp}/bad.jsonl" 2> /dev/null \
  && fail "import without a manifest succeeded"
grep -q '"error":"bad_input"' "${tmp}/bad.jsonl" || fail "missing manifest is not bad_input"

echo "ok sessions.sh"
