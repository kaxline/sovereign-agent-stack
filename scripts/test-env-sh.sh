#!/usr/bin/env bash
# scripts/env.sh and the shared .env writers in scripts/lib/env.sh: atomic
# replacement beside the file, kept mode and symlink, the client allowlist,
# idempotence and the --json result.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# shellcheck source=lib/env.sh
. "${ROOT}/scripts/lib/env.sh"

# --- lib/env.sh: env_get ---------------------------------------------------
# First match wins; one layer of matching quotes comes off.
g="${tmp}/get.env"
cat > "$g" <<'EOF'
PLAIN=abc
DQ="a b"
SQ='a b'
NESTED="'x'"
NESTED2='"x"'
LEAD_DQ="abc
TRAIL_DQ=abc"
LEAD_SQ='abc
MIXED="abc'
INNER=Keith's KB
EQ="a=b"
QUOTE_ONLY="
EMPTY_DQ=""
EMPTY=
DUP=first
DUP=second
EOF
eg() { ENV_FILE="$g" env_get "$@"; }
check_get() {
  local got
  got="$(eg "$1" "${3:-}")"
  [ "$got" = "$2" ] || fail "env_get $1: want [$2], got [$got]"
}
check_get PLAIN 'abc'
check_get DQ 'a b'
check_get SQ 'a b'
check_get NESTED "'x'"
check_get NESTED2 '"x"'
check_get LEAD_DQ '"abc'
check_get TRAIL_DQ 'abc"'
check_get LEAD_SQ "'abc"
check_get MIXED "\"abc'"
check_get INNER "Keith's KB"
check_get EQ 'a=b'
check_get QUOTE_ONLY '"'
check_get EMPTY_DQ 'dflt' dflt
check_get EMPTY 'dflt' dflt
check_get MISSING 'dflt' dflt
check_get MISSING ''
check_get DUP 'first'
[ "$(ENV_FILE="${tmp}/none.env" env_get PLAIN dflt)" = dflt ] || fail "env_get without a file"
(cd "$tmp" && cp "$g" .env && [ "$(env_get DQ)" = 'a b' ]) || fail "env_get reads ./.env by default"
rm -f "${tmp}/.env"
[ "$(env_get_file "$g" SQ)" = 'a b' ] || fail "env_get_file"

# --- lib/env.sh: env_true ---------------------------------------------------
# addon_manifest.truthy's rule: 1, true, yes, on, any case, ends trimmed.
t="${tmp}/true.env"
printf '%s\n' 'A=1' 'B=True' 'C=YES' 'D=on' 'E=" on "' 'F=0' 'G=off' 'H=nope' 'I=' 'J=t rue' > "$t"
for k in A B C D E; do ENV_FILE="$t" env_true "$k" || fail "env_true $k: want true"; done
for k in F G H I J MISSING; do
  if ENV_FILE="$t" env_true "$k"; then fail "env_true $k: want false"; fi
done
ENV_FILE="$t" env_true MISSING yes || fail "env_true default yes: want true"
if ENV_FILE="$t" env_true I 0; then fail "env_true empty with default 0: want false"; fi
# Python's own rule agrees on every value above.
for k in A B C D E F G H I J; do
  v="$(ENV_FILE="$t" env_get "$k")"
  py="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import addon_manifest as m; print(int(m.truthy(sys.argv[2])))' "${ROOT}/scripts/lib" "$v")"
  sh=0; ENV_FILE="$t" env_true "$k" && sh=1
  [ "$py" = "$sh" ] || fail "env_true $k=[$v]: shell $sh, addon_manifest.truthy $py"
done
# No inline copies of the rule left in doctor or the upgrade hooks.
for s in scripts/doctor.sh scripts/lib/doctor-*.sh upgrades/lib.sh; do
  if grep -E -q '1 ?\| ?true ?\| ?(TRUE|yes)' "$s"; then fail "${s} spells truthiness inline; use env_true"; fi
done

# env_get keeps a trailing " # comment" as part of the value (compose drops
# it), and setup.sh copies .env.example to .env, so comments go on their own
# line there.
inline="$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=[^"'"'"'#]*[[:space:]]#' "${ROOT}/.env.example" || true)"
[ -z "$inline" ] || fail ".env.example has inline comments: ${inline}"

# These read .env through lib/env.sh alone (AS-0006, AS-0014). install.sh
# keeps its own reader because it runs before the checkout exists.
for s in scripts/doctor.sh scripts/coding-root.sh scripts/context-root.sh \
  scripts/agent-create.sh scripts/lib/data-root.sh scripts/setup.sh scripts/up.sh \
  scripts/lib/python.sh Makefile scripts/corpus.sh scripts/kb-eval-fixture.sh \
  scripts/opencode-smoke-fixture.sh scripts/reset-neo4j-password.sh \
  scripts/agent-attach-buzz.sh scripts/bootstrap-buzz.sh scripts/buzz-admin.sh \
  scripts/buzz-relay.sh scripts/install-buzz-cli.sh scripts/sync-buzz-profile.sh \
  scripts/sync-signal-profile.sh scripts/mail-triage-cron.sh \
  scripts/messages-triage-cron.sh; do
  if grep -q '^ *env_get() {' "${ROOT}/${s}"; then
    fail "${s} defines its own env_get"
  fi
  # A KEY= reader; doctor's bw_get reads another format (spaces around =).
  # shellcheck disable=SC2016  # a regex: $ and {} match those characters
  if grep -E -q '\^[A-Za-z_${}]+=.*cut -d= -f2-' "${ROOT}/${s}"; then
    fail "${s} reads .env with grep | cut instead of env_get"
  fi
done

# --- lib/env.sh: upsert_env and remove_env -------------------------------

f="${tmp}/a.env"
printf 'A=1\nB=2\n' > "$f"
chmod 640 "$f"
TMPDIR="${tmp}/elsewhere-does-not-exist" upsert_env "$f" B 3 \
  || fail "upsert_env must not need TMPDIR"
[ "$(cat "$f")" = "$(printf 'A=1\nB=3')" ] || fail "upsert_env replace: $(cat "$f")"
mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")"
[ "$mode" = 640 ] || fail "upsert_env kept mode 640, got $mode"
inode_of() { stat -c %i "$1" 2>/dev/null || stat -f %i "$1"; }
ino_before="$(inode_of "$f")"
upsert_env "$f" C 4
[ "$(tail -1 "$f")" = "C=4" ] || fail "upsert_env append"
# An append replaces the file too, rather than writing into it.
[ "$(inode_of "$f")" != "$ino_before" ] || fail "upsert_env append wrote in place"

# Scripts use lib/env.sh's upsert_env; a local copy is how in-place appends
# to .env came back before.
copies="$(grep -l '^upsert_env() {' "${ROOT}"/scripts/*.sh | grep -v '/test-' || true)"
[ -z "$copies" ] || fail "scripts define their own upsert_env: ${copies}"
remove_env "$f" A
grep -q '^A=' "$f" && fail "remove_env left A"
[ -z "$(find "$tmp" -name '.a.env.*')" ] || fail "temp file left behind"

# A symlinked .env (a worktree linking main's) stays a link; the target changes.
real="${tmp}/real.env"
printf 'X=1\n' > "$real"
ln -s "$real" "${tmp}/link.env"
upsert_env "${tmp}/link.env" X 2
[ -L "${tmp}/link.env" ] || fail "upsert_env replaced the symlink"
[ "$(cat "$real")" = "X=2" ] || fail "upsert_env did not write the link target"

# --- scripts/env.sh -------------------------------------------------------
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib"
cp "${ROOT}/scripts/env.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/json-lines.sh" "${ROOT}/scripts/lib/data-root.sh" \
  "${ROOT}/scripts/lib/stack-lock.sh" "${stack}/scripts/lib/"
printf 'HERMES_AGENT_IMAGE_TAG=v1\nLLM_PROXY_ADMIN_TOKEN=secret\n' > "${stack}/.env"
run() { (cd "$stack" && bash scripts/env.sh "$@"); }

out="$(run set HERMES_AGENT_IMAGE_TAG v2 --json)" || fail "set pin failed: $out"
[ "$out" = '{"phase":"done","key":"HERMES_AGENT_IMAGE_TAG","changed":true}' ] \
  || fail "set result: $out"
before="$(cat "${stack}/.env")"
out="$(run set HERMES_AGENT_IMAGE_TAG v2 --json)"
[ "$out" = '{"phase":"done","key":"HERMES_AGENT_IMAGE_TAG","changed":false}' ] \
  || fail "second set result: $out"
[ "$(cat "${stack}/.env")" = "$before" ] || fail "second set changed the file"

run set LLM_MODEL qwen --json > /dev/null || fail "LLM_MODEL refused"
run set CODING_EXTRA_ROOTS "/a,/b" --json > /dev/null || fail "CODING_ refused"
run set HERMES_TERMINAL_BACKEND local --json > /dev/null || fail "terminal backend refused"
run set CALDAV_TZ America/Chicago --json > /dev/null || fail "CALDAV_TZ refused"
out="$(run set LIGHTRAG_LLM_MODEL m --json)" || fail "LIGHTRAG_LLM_MODEL refused: $out"
[ "$out" = '{"phase":"done","key":"LIGHTRAG_LLM_MODEL","changed":true}' ] || fail "lightrag result: $out"
out="$(run set LIGHTRAG_LLM_BINDING_API_KEY '' --json)" || fail "blank LightRAG key refused: $out"
case "$out" in '{"phase":"done"'*) ;; *) fail "blank LightRAG key: $out" ;; esac
run set EMBEDDING_MODEL e --json > /dev/null || fail "EMBEDDING_MODEL refused"
grep -q '^EMBEDDING_MODEL=e$' "${stack}/.env" || fail "EMBEDDING_MODEL not written"
grep -q '^CALDAV_TZ=America/Chicago$' "${stack}/.env" || fail "CALDAV_TZ not written"
grep -q '^LLM_MODEL=qwen$' "${stack}/.env" || fail "LLM_MODEL not written"

out="$(run unset LLM_MODEL --json)" || fail "unset failed: $out"
[ "$out" = '{"phase":"done","key":"LLM_MODEL","changed":true}' ] || fail "unset result: $out"
grep -q '^LLM_MODEL=' "${stack}/.env" && fail "unset left LLM_MODEL"
out="$(run unset LLM_MODEL --json)"
[ "$out" = '{"phase":"done","key":"LLM_MODEL","changed":false}' ] || fail "second unset: $out"

expect_refused() {
  key="$1" reason="$2"
  shift 2
  set +e
  out="$(run set "$key" "$@" --json)"
  rc=$?
  set -e
  [ "$rc" = 2 ] || fail "set $key exit $rc, want 2"
  case "$out" in
    *'"phase":"error"'*"\"error\":\"${reason}\""*) ;;
    *) fail "set $key: want error ${reason}, got $out" ;;
  esac
}
expect_refused LLM_PROXY_ADMIN_TOKEN not_allowed x
expect_refused BITWARDEN_ENABLED use_addon_sh 1
expect_refused COMPOSE_PROFILES use_addon_sh core
expect_refused RANDOM_KEY not_allowed x
expect_refused 'BAD KEY' bad_input x
expect_refused LLM_MODEL bad_input "$(printf 'a\nb')"
grep -q '^LLM_PROXY_ADMIN_TOKEN=secret$' "${stack}/.env" || fail "refused set changed a secret"

# Human mode prints nothing on stdout.
out="$(run set LLM_MODEL m2)"
[ -z "$out" ] || fail "human mode stdout: $out"

# --- the config lock ----------------------------------------------------------
lock="${stack}/data/addons/.lock"
mkdir -p "${stack}/data/addons"
{ [ ! -e "$lock" ] && [ ! -L "$lock" ]; } || fail "env.sh left the lock behind"

# A live holder with a client name: busy at once, and the error names it.
sleep 30 &
holder=$!
ln -s "${holder}:laptop:1700000000" "$lock"
set +e
out="$(cd "$stack" && STACK_CLIENT=phone bash scripts/env.sh set LLM_MODEL x --json 2> /dev/null)"
rc=$?
set -e
[ "$rc" = 1 ] || fail "busy exit $rc"
python3 - "$out" "$holder" << 'EOF' || fail "busy line: $out"
import json, sys
d = json.loads(sys.argv[1])
assert d["phase"] == "error" and d["error"] == "busy", d
assert d["holder"] == {"pid": int(sys.argv[2]), "client": "laptop", "since": "2023-11-14T22:13:20Z"}, d
assert "client laptop" in d["message"], d
EOF
[ "$(readlink "$lock")" = "${holder}:laptop:1700000000" ] || fail "busy touched the holder's lock"

# With STACK_LOCK_TIMEOUT the call waits for the holder to let go.
( sleep 2; rm -f "$lock" ) &
out="$(cd "$stack" && STACK_LOCK_TIMEOUT=10 bash scripts/env.sh set LLM_MODEL waited --json)" \
  || fail "waiting for the lock failed: $out"
grep -q '^LLM_MODEL=waited$' "${stack}/.env" || fail "the waiting call did not write"
kill "$holder" 2> /dev/null || true

# A dead holder's lock (a bare PID, as before 0.3.17) is taken over.
ln -s 999999 "$lock"
run set LLM_MODEL tookover --json > /dev/null || fail "dead holder's lock not taken over"
[ ! -L "$lock" ] || fail "lock left after a takeover"

# Ten clients at once, each its own key: every change lands.
pids=""
for n in 0 1 2 3 4 5 6 7 8 9; do
  (cd "$stack" && STACK_CLIENT="c${n}" STACK_LOCK_TIMEOUT=30 bash scripts/env.sh set "LLM_CONCURRENT_${n}" "v${n}" --json > /dev/null) &
  pids="${pids} $!"
done
for pid in $pids; do wait "$pid" || fail "a concurrent set failed"; done
for n in 0 1 2 3 4 5 6 7 8 9; do
  grep -q "^LLM_CONCURRENT_${n}=v${n}$" "${stack}/.env" || fail "concurrent set ${n} lost: $(cat "${stack}/.env")"
done
[ ! -L "$lock" ] || fail "lock left after the concurrent sets"

echo "ok env.sh"
