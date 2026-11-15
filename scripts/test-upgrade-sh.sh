#!/usr/bin/env bash
# scripts/upgrade.sh: which hooks run, the directives, the start it asks
# scripts/up.sh for, the --json result, and the shipped hooks on a synthetic
# stack. scripts/up.sh is replaced by a stub that records its arguments.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

new_stack() {
  stack="${tmp}/$1"
  mkdir -p "${stack}/scripts/lib" "${stack}/upgrades"
  cp "${ROOT}/scripts/upgrade.sh" "${stack}/scripts/"
  cp "${ROOT}/scripts/lib/env.sh" "${stack}/scripts/lib/"
  cp "${ROOT}/upgrades/lib.sh" "${stack}/upgrades/"
  # shellcheck disable=SC2016  # expanded by the stub
  printf '#!/bin/sh\necho "$*" > "$(dirname "$0")/../up.args"\n' > "${stack}/scripts/up.sh"
  chmod +x "${stack}/scripts/up.sh"
  : > "${stack}/.env"
}
run() { (cd "$stack" && bash scripts/upgrade.sh "$@"); }
hook() {
  # shellcheck disable=SC2016  # expanded by the hook
  printf '#!/usr/bin/env bash\nset -eu\nsource "$(dirname "$0")/lib.sh"\necho "$STACK_FROM>$STACK_TO" >> "$(dirname "$0")/../ran"\n%s\n' "$2" \
    > "${stack}/upgrades/$1.sh"
}

# --- selection and directives ------------------------------------------------
new_stack sel
hook v0.3.9 'notice "nine"'
hook v0.3.10 'rebuild broker; needs_user ten-id "ten: do a thing"'
hook v0.3.11 'rebuild broker; reload_hermes; notice "eleven"'
hook v0.4.0 'notice "four"'
printf 'v0.3.11\n' > "${stack}/.stack-version"

out="$(run --from v0.3.9 --json)" || fail "upgrade failed: $out"
[ "$(cat "${stack}/ran")" = "$(printf 'v0.3.9>v0.3.11\nv0.3.9>v0.3.11')" ] \
  || fail "hooks run: $(cat "${stack}/ran")"
[ "$(cat "${stack}/up.args")" = "--build broker --reload-hermes" ] \
  || fail "up.sh args: $(cat "${stack}/up.args")"
done_line="$(printf '%s\n' "$out" | tail -1)"
python3 - "$done_line" << 'EOF' || fail "done line: $done_line"
import json, sys
d = json.loads(sys.argv[1])
assert d["event"] == "done", d
assert d["hooks"] == ["v0.3.10", "v0.3.11"], d  # numeric order, v0.3.9 excluded
assert d["needs_user"] == [{"id": "ten-id", "message": "ten: do a thing", "version": "v0.3.10"}], d
assert [n["message"] for n in d["notices"]] == ["eleven"], d
EOF
printf '%s\n' "$out" | grep -q '"step":"start"' || fail "no start step"

# Same version: no hooks, still starts. --no-start doesn't.
rm -f "${stack}/ran" "${stack}/up.args"
run --from v0.3.11 --json > /dev/null
[ ! -e "${stack}/ran" ] || fail "hooks ran for an unchanged version"
[ -e "${stack}/up.args" ] || fail "no start for an unchanged version"
rm -f "${stack}/up.args"
run --from v0.3.9 --to v0.3.10 --no-start --json > /dev/null
[ ! -e "${stack}/up.args" ] || fail "--no-start started"

# A git describe suffix runs that tag's hooks.
rm -f "${stack}/ran"
run --from v0.3.10 --to v0.3.11-4-gabc1234 --no-start > /dev/null 2>&1
[ "$(wc -l < "${stack}/ran" | tr -d ' ')" = 1 ] || fail "describe suffix"

expect_error() {
  want_rc="$1" want="$2"
  shift 2
  set +e
  out="$(run "$@" --json 2> /dev/null)"
  rc=$?
  set -e
  [ "$rc" = "$want_rc" ] || fail "$* exit $rc, want $want_rc"
  case "$out" in *"\"error\":\"${want}\""*) ;; *) fail "$*: $out" ;; esac
}
expect_error 2 bad_input --from v0.4.0 --to v0.3.11
expect_error 2 bad_input --from main
expect_error 2 bad_input --to v0.3.11

# A failing hook stops the upgrade before the start.
hook v0.3.12 'echo "broken step" >&2; exit 3'
rm -f "${stack}/up.args"
expect_error 1 command_failed --from v0.3.11 --to v0.3.12
case "$out" in *'"step":"hook v0.3.12"'*'broken step'* | *'broken step'*'"step":"hook v0.3.12"'*) ;; *) fail "hook failure: $out" ;; esac
[ ! -e "${stack}/up.args" ] || fail "started after a failed hook"
hook v0.3.12 'echo "bogus thing"'
expect_error 1 command_failed --from v0.3.11 --to v0.3.12

# --- the shipped hooks ---------------------------------------------------------
new_stack shipped
cp "${ROOT}"/upgrades/v*.sh "${stack}/upgrades/"
mkdir -p "${stack}/data/hermes/profiles/work" "${stack}/compose/caldav-mcp/accounts"
printf 'A=1\nVIKUNJA_MCP_TOKEN=old\n' > "${stack}/data/hermes/.env"
printf 'VIKUNJA_MCP_TOKEN=old\nTODOS_KEY=new\n' > "${stack}/data/hermes/profiles/work/.env"
printf 'URL=x\n' > "${stack}/compose/caldav-mcp/accounts/home.env"
printf 'BITWARDEN_ENABLED=1\n' > "${stack}/.env"

out="$(run --from v0.3.14 --to v0.3.16 --json)" || fail "shipped hooks failed: $out"
grep -q VIKUNJA_MCP_TOKEN "${stack}/data/hermes/.env" "${stack}/data/hermes/profiles/work/.env" \
  && fail "VIKUNJA_MCP_TOKEN left in a profile .env"
grep -q '^TODOS_KEY=new$' "${stack}/data/hermes/profiles/work/.env" || fail "TODOS_KEY lost"
[ "$(cat "${stack}/up.args")" = "--build bitwarden-broker --reload-hermes" ] \
  || fail "shipped up.sh args: $(cat "${stack}/up.args")"
python3 - "$(printf '%s\n' "$out" | tail -1)" << 'EOF' || fail "shipped done line: $out"
import json, sys
d = json.loads(sys.argv[1])
assert d["hooks"] == ["v0.3.15", "v0.3.16"], d
assert [n["id"] for n in d["needs_user"]] == ["caldav-reconnect-home", "bitwarden-in-page-fill"], d
assert len(d["notices"]) == 2, d
EOF

# Without Bitwarden, v0.3.16 asks for nothing; run again, v0.3.15 is idempotent.
printf 'BITWARDEN_ENABLED=0\n' > "${stack}/.env"
rm "${stack}/compose/caldav-mcp/accounts/home.env"
out="$(run --from v0.3.14 --to v0.3.16 --json)"
[ "$(cat "${stack}/up.args")" = "--reload-hermes" ] || fail "no-bitwarden args: $(cat "${stack}/up.args")"
case "$out" in *'"needs_user":[]'*) ;; *) fail "no-bitwarden needs_user: $out" ;; esac

echo "ok upgrade.sh"
