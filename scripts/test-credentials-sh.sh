#!/usr/bin/env bash
# scripts/credentials.sh against synthetic .env and api-server.env files in a
# temp copy of the stack: values, placeholders and missing keys or files.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib" "${stack}/compose/hermes"
cp "${ROOT}/scripts/credentials.sh" "${stack}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/json-lines.sh" "${stack}/scripts/lib/"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
run() { bash "${stack}/scripts/credentials.sh" "$@"; }
field() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(repr('"$1"'))'; }

# No files at all: every value null.
out="$(run --json)"
[ "$out" = '{"HERMES_DASHBOARD_PASSWORD":null,"HERMES_DASHBOARD_HANDOFF_SECRET":null,"API_SERVER_KEY":null,"LLM_PROXY_ADMIN_TOKEN":null}' ] \
  || fail "no files: $out"

# Real-looking values, one quoted, and a value with a quote and a backslash.
cat > "${stack}/.env" << 'EOF'
HERMES_DASHBOARD_PASSWORD="synthetic-dash-pw"
HERMES_DASHBOARD_HANDOFF_SECRET=synthetic-handoff
LLM_PROXY_ADMIN_TOKEN=syn"tok\en
OTHER=x
EOF
printf 'API_SERVER_ENABLED=true\nAPI_SERVER_KEY=synthetic-api-key\n' > "${stack}/compose/hermes/api-server.env"
out="$(run --json)"
[ "$(printf '%s' "$out" | field 'd["HERMES_DASHBOARD_PASSWORD"]')" = "'synthetic-dash-pw'" ] || fail "password: $out"
[ "$(printf '%s' "$out" | field 'd["HERMES_DASHBOARD_HANDOFF_SECRET"]')" = "'synthetic-handoff'" ] || fail "handoff: $out"
[ "$(printf '%s' "$out" | field 'd["API_SERVER_KEY"]')" = "'synthetic-api-key'" ] || fail "api key: $out"
[ "$(printf '%s' "$out" | field 'd["LLM_PROXY_ADMIN_TOKEN"]')" = "'syn\"tok\\\\en'" ] || fail "token escaping: $out"
[ "$(printf '%s' "$out" | field 'sorted(d)')" = "['API_SERVER_KEY', 'HERMES_DASHBOARD_HANDOFF_SECRET', 'HERMES_DASHBOARD_PASSWORD', 'LLM_PROXY_ADMIN_TOKEN']" ] \
  || fail "keys: $out"

# Placeholders from the example files and an empty value are null.
cat > "${stack}/.env" << 'EOF'
HERMES_DASHBOARD_PASSWORD=change-me-generate-with-openssl-rand-hex-16
HERMES_DASHBOARD_HANDOFF_SECRET=
LLM_PROXY_ADMIN_TOKEN=change-me
EOF
cp "${ROOT}/compose/hermes/api-server.env.example" "${stack}/compose/hermes/api-server.env"
out="$(run --json)"
[ "$out" = '{"HERMES_DASHBOARD_PASSWORD":null,"HERMES_DASHBOARD_HANDOFF_SECRET":null,"API_SERVER_KEY":null,"LLM_PROXY_ADMIN_TOKEN":null}' ] \
  || fail "placeholders: $out"

# JSON only; unknown options are bad_input (exit 2).
set +e
run > /dev/null 2>&1
rc=$?
out="$(run --json --nope 2> /dev/null)"
rc2=$?
set -e
[ "$rc" = 2 ] || fail "without --json exit $rc"
[ "$rc2" = 2 ] || fail "unknown option exit $rc2"
case "$out" in *'"error":"bad_input"'*) ;; *) fail "unknown option: $out" ;; esac

echo "ok credentials.sh"
