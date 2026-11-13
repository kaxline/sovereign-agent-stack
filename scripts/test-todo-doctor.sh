#!/usr/bin/env bash
# make doctor's per-profile Vikunja checks (todo_profile_checks in
# scripts/lib/doctor-todo.sh) against profile files laid out as the bootstrap
# writes them: mcp_servers.todos with an Authorization header that references
# ${TODOS_KEY}, and TODOS_KEY in the profile's .env.
#
#   bash scripts/test-todo-doctor.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
work="$tmp/work"
mkdir -p "$work/data/hermes/profiles/api-server" "$work/data/hermes/profiles/browser"

cat > "$tmp/harness.sh" <<'SH'
set -euo pipefail
ok() { printf '  OK  %s\n' "$1"; }
bad() { printf ' FAIL %s\n' "$1"; }
warn() { printf ' WARN %s\n' "$1"; }
env_get() {
  local val=""
  [[ -f .env ]] && val="$(grep -E "^${1}=" .env | head -1 | cut -d= -f2- || true)"
  echo "${val:-${2:-}}"
}
# shellcheck source=lib/doctor-todo.sh
source "${ROOT}/scripts/lib/doctor-todo.sh"
todo_profile_checks
SH

ok() { printf 'ok: %s\n' "$1"; }
out="$tmp/out"
doctor() {
  (cd "$work" && ROOT="$ROOT" bash "$tmp/harness.sh") > "$out" 2>&1 || fail "the block exited non-zero ($(cat "$out"))"
}
has() { grep -qF -- "$2" "$out" || fail "$1 (output: $(cat "$out"))"; }
hasnt() { if grep -qF -- "$2" "$out"; then fail "$1 (output: $(cat "$out"))"; fi; }

printf 'VIKUNJA_MCP_TOKEN_RW=tok-rw\nVIKUNJA_MCP_TOKEN_RO=tok-ro\n' > "$work/.env"
config() { # DIR: a config.yaml as the bootstrap writes it
  cat > "$1/config.yaml" <<'YAML'
mcp_servers:
  todos:
    url: http://vikunja:3456/api/v2/mcp
    timeout: 60
    connect_timeout: 30
    headers:
      Authorization: Bearer ${TODOS_KEY}
tools: {}
YAML
}
h="$work/data/hermes"
for d in "$h" "$h/profiles/api-server" "$h/profiles/browser"; do config "$d"; done
printf 'TODOS_KEY=tok-rw\n' > "$h/.env"
printf 'TODOS_KEY=tok-ro\n' > "$h/profiles/api-server/.env"
printf 'TODOS_KEY=tok-rw\n' > "$h/profiles/browser/.env"

doctor
for p in default api-server browser; do
  has "a bootstrapped ${p} profile passes" "OK  Hermes ${p} profile registers the todos MCP server"
done
hasnt "a bootstrapped install has no warnings" "WARN"
hasnt "a bootstrapped install has no failures" "FAIL"
ok "a bootstrapped install passes every profile check"

printf 'TODOS_KEY=tok-rw\n' > "$h/profiles/api-server/.env"
doctor
has "api-server holding the read/write token fails" "FAIL api-server holds the read/write Vikunja token"
ok "api-server holding the read/write token fails"

printf 'TODOS_KEY=\n' > "$h/profiles/api-server/.env"
doctor
has "an empty TODOS_KEY warns" "WARN Hermes api-server profile has no todos MCP entry"
ok "an empty TODOS_KEY warns"

printf 'TODOS_KEY=tok-ro\n' > "$h/profiles/api-server/.env"
printf 'mcp_servers:\n  vikunja:\n    url: http://vikunja:3456/api/v2/mcp\n' > "$h/profiles/browser/config.yaml"
doctor
has "the old vikunja key warns" "WARN Hermes browser profile still uses the old mcp_servers.vikunja key"
ok "the old vikunja key warns"

rm "$h/profiles/browser/config.yaml"
doctor
hasnt "a profile without config.yaml is skipped" "browser"
ok "a profile without config.yaml is skipped"

printf 'OK doctor Vikunja profile checks\n'
