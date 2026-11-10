#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the GPT Researcher MCP, and
# which tools. Registration goes through the generic add-on path
# (register_addon_mcp_servers reading addons/gptr/addon.json); the calls it
# makes must match what the GPT Researcher-specific code made in v0.3.9.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
compose="${ROOT}/docker-compose.yml"
# Only GPT Researcher's manifest: other add-ons add their own drop calls.
addons="$(mktemp -d)"
ADDONS_DIR="$addons"
mkdir "${ADDONS_DIR}/gptr"
cp "${ROOT}/addons/gptr/addon.json" "${ADDONS_DIR}/gptr/addon.json"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args register_addon_mcp_servers mcp_tool_name

log() { :; }

# The plan is read in a pipeline, so the stubs' record has to come back
# through a file rather than the subshell's $calls.
record="$(mktemp)"
trap 'rm -rf "$record" "$addons"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() { printf 'register:%s:%s:%s:%s;' "${1:-default}" "$2" "$5" "$(shift 6; echo "$*")" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
drop_mcp_entries_for_url() { printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

URL="http://gptr-mcp:8000/sse"
TOOLS="deep_research quick_search write_report get_research_sources get_research_context"
PINS="deep_research quick_search write_report"

export GPTR_MCP_ENABLED=0
run ""; got
[ "$calls" = "drop:default:${URL};" ] || fail "disabled: default should only drop entries ($calls)"

GPTR_MCP_ENABLED=1
for profile in "" browser; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$calls" = "register:${p}:gptr:900:${TOOLS};drop:${p}:${URL};set:${p}:mcp_servers.gptr.transport=sse;pin:${p}:${PINS};" ] \
    || fail "${p} registers the five tools over SSE and pins three ($calls)"
done
run api-server; got
[ "$calls" = "drop:api-server:${URL};" ] || fail "api-server must drop, never register ($calls)"

# GPTR_MCP_URL and GPTR_MCP_TIMEOUT still override the manifest.
export GPTR_MCP_URL="http://elsewhere:9000/sse" GPTR_MCP_TIMEOUT=60
run browser; got
case "$calls" in
  "register:browser:gptr:60:"*) ;;
  *) fail "GPTR_MCP_TIMEOUT overrides the manifest ($calls)" ;;
esac
GPTR_MCP_ENABLED=0
run browser; got
[ "$calls" = "drop:browser:http://elsewhere:9000/sse;" ] || fail "GPTR_MCP_URL overrides the manifest ($calls)"
unset GPTR_MCP_URL GPTR_MCP_TIMEOUT

# reconcile_stack_eager_tools re-pins the same tools under both names.
GPTR_MCP_ENABLED=1
pins="$(addon_mcp_pin_args browser)"
[ "$pins" = " deep_research mcp__gptr__deep_research quick_search mcp__gptr__quick_search write_report mcp__gptr__write_report" ] \
  || fail "pin args for browser ($pins)"
[ -z "$(addon_mcp_pin_args api-server)" ] || fail "no pin args on api-server"
GPTR_MCP_ENABLED=0
[ -z "$(addon_mcp_pin_args browser)" ] || fail "no pin args with the flag off"

# A broken manifest is skipped, not fatal.
broken="$(mktemp -d)"
mkdir "${broken}/gptr"
printf '{not json' > "${broken}/gptr/addon.json"
ADDONS_DIR="$broken"
GPTR_MCP_ENABLED=1
run browser 2>/dev/null; got
[ -z "$calls" ] || fail "a broken manifest registers nothing ($calls)"
rm -rf "$broken"
ADDONS_DIR="$addons"

# Compose: gptr-mcp starts with research or automation, and the REST service
# only with automation. test-addon-manifests.py checks the flag reaches both
# bootstraps and its sync-profiles row.
sed -n '/^  gptr-mcp:/,/^    container_name:/p' "$compose" | grep -q 'profiles: \["research", "automation"\]' \
  || fail "gptr-mcp must be in the research and automation profiles"
sed -n '/^  gpt-researcher:/,/^    container_name:/p' "$compose" | grep -q 'profiles: \["automation"\]' \
  || fail "gpt-researcher (REST, port 8000) stays in automation only"

printf 'OK GPT Researcher MCP registration\n'
