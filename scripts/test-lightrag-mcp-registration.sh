#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# What hermes-*-bootstrap does with the LightRAG MCP on the profile it manages,
# with LIGHTRAG_MCP_ENABLED on and off (CR 0022 R1).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
compose="${ROOT}/docker-compose.yml"

# shellcheck disable=SC1090
eval "$(sed -n '/^register_lightrag_mcp_servers() {/,/^}/p' "$bootstrap")"
eval "$(grep -E '^LIGHTRAG_MCP_(URL|TIMEOUT|CONNECT_TIMEOUT|TOOLS)=' "$bootstrap")"
command -v register_lightrag_mcp_servers >/dev/null \
  || { printf 'FAIL register_lightrag_mcp_servers not found in bootstrap\n' >&2; exit 1; }

calls=""
log() { :; }
register_mcp_server() { calls="${calls}register:${1:-default}:${2}:${5}:$(shift 6; echo "$*");"; }
drop_unfiltered_mcp_duplicate() { calls="${calls}dedupe:${1:-default}:${2}:${3};"; }
pin_mcp_tools() { calls="${calls}pin:${1:-default}:$(shift 2; echo "$*");"; }
drop_mcp_entries_for_url() { calls="${calls}drop:${1:-default}:${2}:${3};"; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

run() {
  calls=""
  register_lightrag_mcp_servers "$1"
}

URL="http://lightrag-mcp:8000/mcp"
TOOLS="query_document get_documents get_pipeline_status get_graph_labels check_lightrag_health"

# Off: every entry on the LightRAG URL goes, whatever its key, so the
# unfiltered lightrag-mcp copy cloned from default goes too.
for flag in 0 "" false no; do
  LIGHTRAG_MCP_ENABLED="$flag"
  for profile in api-server browser; do
    run "$profile"
    [ "$calls" = "drop:${profile}::${URL};" ] \
      || fail "LIGHTRAG_MCP_ENABLED='${flag}': ${profile} should only drop entries ($calls)"
  done
done

# On: the five read tools, no unfiltered duplicate, pins.
for flag in 1 true yes; do
  LIGHTRAG_MCP_ENABLED="$flag"
  for profile in api-server browser; do
    run "$profile"
    [ "$calls" = "register:${profile}:lightrag:120:${TOOLS};dedupe:${profile}:lightrag:lightrag-mcp;pin:${profile}:${TOOLS};" ] \
      || fail "LIGHTRAG_MCP_ENABLED='${flag}': ${profile} registers the five read tools ($calls)"
  done
done

# The default profile is the user's own config: bootstrap never calls this for it.
grep -qE '^ *register_lightrag_mcp_servers ""' "$bootstrap" \
  && fail "register_lightrag_mcp_servers must not run on the default profile"
# shellcheck disable=SC2016  # a literal $PROFILE in the pattern
grep -qE '^register_lightrag_mcp_servers "\$PROFILE"$' "$bootstrap" \
  || fail "bootstrap must call register_lightrag_mcp_servers for \$PROFILE"

[ "$(grep -cF -- '- LIGHTRAG_MCP_ENABLED=' "$compose")" = 2 ] \
  || fail "both hermes-*-bootstrap services must receive LIGHTRAG_MCP_ENABLED"

printf 'OK LightRAG MCP registration\n'
