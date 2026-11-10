#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# What hermes-*-bootstrap does with the LightRAG MCP, with LIGHTRAG_MCP_ENABLED
# on and off. Registration goes through the generic add-on path
# (register_addon_mcp_servers reading addons/lightrag/addon.json); the calls it
# makes must match what the LightRAG-specific code made before CR 0022 R2:
# the same key, the same five read tools, the same pins, api-server and
# browser only, and nothing on the default profile.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
# Only LightRAG's manifest: other add-ons add their own drop calls.
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/lightrag"
cp "${ROOT}/addons/lightrag/addon.json" "${ADDONS_DIR}/lightrag/addon.json" 2>/dev/null || true
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args register_addon_mcp_servers mcp_tool_name

log() { :; }

[ -f "${ADDONS_DIR}/lightrag/addon.json" ] || fail "addons/lightrag/addon.json not found"

# The plan is read in a pipeline, so the stubs' record has to come back
# through a file rather than the subshell's $calls.
record="$(mktemp)"
trap 'rm -rf "$record" "$ADDONS_DIR"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() { printf 'register:%s:%s:%s:%s;' "${1:-default}" "$2" "$5" "$(shift 6; echo "$*")" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
drop_mcp_entries_for_url() { printf 'drop:%s:%s:%s;' "${1:-default}" "$2" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

unset LIGHTRAG_MCP_URL LIGHTRAG_MCP_TIMEOUT LIGHTRAG_MCP_CONNECT_TIMEOUT
URL="http://lightrag-mcp:8000/mcp"
TOOLS="query_document get_documents get_pipeline_status get_graph_labels check_lightrag_health"

# Off: every entry on the LightRAG URL goes, whatever its key, so the
# unfiltered lightrag-mcp copy cloned from default goes too.
for flag in 0 "" false no; do
  export LIGHTRAG_MCP_ENABLED="$flag"
  for profile in api-server browser; do
    run "$profile"; got
    [ "$calls" = "drop:${profile}::${URL};" ] \
      || fail "LIGHTRAG_MCP_ENABLED='${flag}': ${profile} should only drop entries ($calls)"
  done
  run ""; got
  [ -z "$calls" ] || fail "LIGHTRAG_MCP_ENABLED='${flag}': default is left alone ($calls)"
done

# On: the five read tools, the unfiltered duplicate dropped, pins.
for flag in 1 true yes; do
  export LIGHTRAG_MCP_ENABLED="$flag"
  for profile in api-server browser; do
    run "$profile"; got
    [ "$calls" = "register:${profile}:lightrag:120:${TOOLS};drop:${profile}:lightrag:${URL};pin:${profile}:${TOOLS};" ] \
      || fail "LIGHTRAG_MCP_ENABLED='${flag}': ${profile} registers the five read tools ($calls)"
  done
  run ""; got
  [ -z "$calls" ] || fail "LIGHTRAG_MCP_ENABLED='${flag}': bootstrap never registers LightRAG on default ($calls)"
done

# LIGHTRAG_MCP_URL and the timeouts still override the manifest.
export LIGHTRAG_MCP_URL="http://x:1/mcp" LIGHTRAG_MCP_TIMEOUT=60
run browser; got
[ "$calls" = "register:browser:lightrag:60:${TOOLS};drop:browser:lightrag:http://x:1/mcp;pin:browser:${TOOLS};" ] \
  || fail "LIGHTRAG_MCP_URL and LIGHTRAG_MCP_TIMEOUT override the manifest ($calls)"
LIGHTRAG_MCP_ENABLED=0
run api-server; got
[ "$calls" = "drop:api-server::http://x:1/mcp;" ] || fail "LIGHTRAG_MCP_URL applies to the drop ($calls)"
unset LIGHTRAG_MCP_URL LIGHTRAG_MCP_TIMEOUT

# An invalid manifest (here: not canonical JSON) fails closed: the URL is
# dropped from api-server and browser, flag on or off; default stays alone.
tr -d '\n' < "${ROOT}/addons/lightrag/addon.json" > "${ADDONS_DIR}/lightrag/addon.json"
for flag in 0 1; do
  LIGHTRAG_MCP_ENABLED="$flag"
  run api-server 2>/dev/null; got
  [ "$calls" = "drop:api-server::${URL};" ] \
    || fail "invalid manifest, flag ${flag}: api-server should drop the URL ($calls)"
  run "" 2>/dev/null; got
  [ -z "$calls" ] || fail "invalid manifest, flag ${flag}: default is left alone ($calls)"
done
cp "${ROOT}/addons/lightrag/addon.json" "${ADDONS_DIR}/lightrag/addon.json"

# reconcile_stack_eager_tools re-pins the same tools under both names.
LIGHTRAG_MCP_ENABLED=1
want=""
for t in $TOOLS; do want="${want} ${t} mcp__lightrag__${t}"; done
for profile in api-server browser; do
  [ "$(addon_mcp_pin_args "$profile")" = "$want" ] || fail "pin args for ${profile}"
done
[ -z "$(addon_mcp_pin_args "")" ] || fail "no pin args on default"
LIGHTRAG_MCP_ENABLED=0
[ -z "$(addon_mcp_pin_args browser)" ] || fail "no pin args with the flag off"

# The bootstrap carries no LightRAG-specific registration any more.
grep -q 'register_lightrag_mcp_servers\|LIGHTRAG_MCP_TOOLS' "$bootstrap" \
  && fail "LightRAG registration must come from the manifest only"

printf 'OK LightRAG MCP registration\n'
