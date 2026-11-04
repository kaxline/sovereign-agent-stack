#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the GPT Researcher MCP, and which tools.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
compose="${ROOT}/docker-compose.yml"

# shellcheck disable=SC1090
eval "$(sed -n '/^register_gptr_mcp_servers() {/,/^}/p' "$bootstrap")"
eval "$(grep -E '^GPTR_(MCP_URL|MCP_TIMEOUT|MCP_CONNECT_TIMEOUT|MCP_TOOLS|MCP_PINS)=' "$bootstrap")"

calls=""
log() { :; }
register_mcp_server() { calls="${calls}register:${1:-default}:${2}:${5}:$(shift 6; echo "$*");"; }
hermes_config_set() { calls="${calls}set:${1:-default}:${2}=${3};"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default}:$(shift 2; echo "$*");"; }
drop_mcp_entries_for_url() { calls="${calls}drop:${1:-default}:${3};"; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

run() {
  calls=""
  register_gptr_mcp_servers "$1"
}

URL="http://gptr-mcp:8000/sse"
TOOLS="deep_research quick_search write_report get_research_sources get_research_context"
PINS="deep_research quick_search write_report"

GPTR_MCP_ENABLED=0
run ""
[ "$calls" = "drop:default:${URL};" ] || fail "disabled: default should only drop entries ($calls)"

GPTR_MCP_ENABLED=1
for profile in "" browser; do
  run "$profile"
  p="${profile:-default}"
  [ "$calls" = "register:${p}:gptr:900:${TOOLS};set:${p}:mcp_servers.gptr.transport=sse;pin:${p}:${PINS};" ] \
    || fail "${p} registers the five tools over SSE and pins three ($calls)"
done
run api-server
[ "$calls" = "drop:api-server:${URL};" ] || fail "api-server must drop, never register ($calls)"

# Compose: gptr-mcp starts with research or automation, the REST service only
# with automation, and both bootstraps receive the flag.
sed -n '/^  gptr-mcp:/,/^    container_name:/p' "$compose" | grep -q 'profiles: \["research", "automation"\]' \
  || fail "gptr-mcp must be in the research and automation profiles"
sed -n '/^  gpt-researcher:/,/^    container_name:/p' "$compose" | grep -q 'profiles: \["automation"\]' \
  || fail "gpt-researcher (REST, port 8000) stays in automation only"
[ "$(grep -cF -- '- GPTR_MCP_ENABLED=' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services must receive GPTR_MCP_ENABLED"

grep -q '^GPTR_MCP_ENABLED research 1$' "${ROOT}/scripts/sync-profiles.sh" \
  || fail "sync-profiles.sh must map GPTR_MCP_ENABLED to research"

printf 'OK GPT Researcher MCP registration\n'
