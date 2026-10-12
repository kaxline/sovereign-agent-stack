#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Vikunja token and pins hermes-api-bootstrap hands each profile.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

# shellcheck disable=SC1090
eval "$(sed -n '/^vikunja_mcp_token_for() {/,/^}/p; /^vikunja_mcp_pins_for() {/,/^}/p' "$bootstrap")"
eval "$(grep -E '^VIKUNJA_MCP_PIN_(RO|RW)=' "$bootstrap")"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

VIKUNJA_MCP_TOKEN_RO=ro-token
VIKUNJA_MCP_TOKEN_RW=rw-token

VIKUNJA_MCP_ENABLED=0
if vikunja_mcp_token_for "" >/dev/null; then fail "token handed out with VIKUNJA_MCP_ENABLED=0"; fi

VIKUNJA_MCP_ENABLED=1
[ "$(vikunja_mcp_token_for api-server)" = ro-token ] || fail "api-server must get the read-only token"
[ "$(vikunja_mcp_token_for "")" = rw-token ] || fail "default must get the read/write token"
[ "$(vikunja_mcp_token_for browser)" = rw-token ] || fail "browser must get the read/write token"

VIKUNJA_MCP_TOKEN_RO=
if vikunja_mcp_token_for api-server >/dev/null; then
  fail "api-server must not fall back to the read/write token"
fi

case " $(vikunja_mcp_pins_for api-server) " in
  *" tasks_create "*|*" tasks_update "*) fail "api-server pins a write tool" ;;
esac
case " $(vikunja_mcp_pins_for browser) " in
  *" tasks_create "*) ;;
  *) fail "browser does not pin tasks_create" ;;
esac

printf 'OK vikunja token scope\n'
