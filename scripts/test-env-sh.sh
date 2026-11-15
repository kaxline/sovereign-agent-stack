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
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/json-lines.sh" "${stack}/scripts/lib/"
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

echo "ok env.sh"
