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

# --- lib/env.sh: upsert_env and remove_env -------------------------------
# shellcheck source=lib/env.sh
. "${ROOT}/scripts/lib/env.sh"

f="${tmp}/a.env"
printf 'A=1\nB=2\n' > "$f"
chmod 640 "$f"
TMPDIR="${tmp}/elsewhere-does-not-exist" upsert_env "$f" B 3 \
  || fail "upsert_env must not need TMPDIR"
[ "$(cat "$f")" = "$(printf 'A=1\nB=3')" ] || fail "upsert_env replace: $(cat "$f")"
mode="$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")"
[ "$mode" = 640 ] || fail "upsert_env kept mode 640, got $mode"
upsert_env "$f" C 4
[ "$(tail -1 "$f")" = "C=4" ] || fail "upsert_env append"
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
