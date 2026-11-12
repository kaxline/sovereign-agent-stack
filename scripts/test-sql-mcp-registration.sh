#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# What hermes-*-bootstrap does with the SQL MCP, with SQL_MCP_ENABLED on and
# off. Registration goes through the generic add-on path
# (register_addon_mcp_servers reading addons/sql/addon.json), whose two
# entries share the name sql: sql-mcp-ro on api-server, sql-mcp-rw on default
# and browser. The calls must leave each profile with the entry the
# SQL-specific code wrote before the port.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
# Only SQL's manifest: other add-ons add their own drop calls.
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/sql"
cp "${ROOT}/addons/sql/addon.json" "${ADDONS_DIR}/sql/addon.json" 2>/dev/null || true
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args register_addon_mcp_servers mcp_tool_name

log() { :; }

[ -f "${ADDONS_DIR}/sql/addon.json" ] || fail "addons/sql/addon.json not found"

# The plan is read in a pipeline, so the stubs' record has to come back
# through a file rather than the subshell's $calls.
record="$(mktemp)"
trap 'rm -rf "$record" "$ADDONS_DIR"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
register_mcp_server() { printf 'register:%s:%s:%s:%s:%s;' "${1:-default}" "$2" "$4" "$5:$6" "$(shift 6; echo "$*")" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
drop_mcp_entries_for_url() { printf 'drop:%s:%s:%s;' "${1:-default}" "$2" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

unset SQL_MCP_RO_URL SQL_MCP_RW_URL SQL_MCP_TIMEOUT SQL_MCP_CONNECT_TIMEOUT
RO="http://sql-mcp-ro:8000/sse"
RW="http://sql-mcp-rw:8000/sse"
TOOLS="list_schemas list_objects get_object_details execute_sql explain_query"

# Off: both URLs are dropped on every profile, whatever the key.
for flag in 0 "" false no; do
  export SQL_MCP_ENABLED="$flag"
  for profile in api-server browser ""; do
    run "$profile"; got
    p="${profile:-default}"
    [ "$calls" = "drop:${p}::${RO};drop:${p}::${RW};" ] \
      || fail "SQL_MCP_ENABLED='${flag}': ${p} should only drop both URLs ($calls)"
  done
done

# On: api-server gets sql at the ro sidecar first, so the entry cloned from
# default is re-pointed in place; then anything left at the rw URL goes.
for flag in 1 true yes; do
  export SQL_MCP_ENABLED="$flag"
  run api-server; got
  [ "$calls" = "register:api-server:sql:${RO}:60:30:${TOOLS};drop:api-server:sql:${RO};set:api-server:mcp_servers.sql.transport=sse;pin:api-server:${TOOLS};drop:api-server::${RW};" ] \
    || fail "SQL_MCP_ENABLED='${flag}': api-server registers sql read-only ($calls)"
  for profile in browser ""; do
    run "$profile"; got
    p="${profile:-default}"
    [ "$calls" = "drop:${p}::${RO};register:${p}:sql:${RW}:60:30:${TOOLS};drop:${p}:sql:${RW};set:${p}:mcp_servers.sql.transport=sse;pin:${p}:${TOOLS};" ] \
      || fail "SQL_MCP_ENABLED='${flag}': ${p} registers sql read/write ($calls)"
  done
done

# The URL and timeout variables still override the manifest.
export SQL_MCP_RO_URL="http://x:1/sse" SQL_MCP_RW_URL="http://y:2/sse" SQL_MCP_TIMEOUT=90 SQL_MCP_CONNECT_TIMEOUT=10
run api-server; got
[ "$calls" = "register:api-server:sql:http://x:1/sse:90:10:${TOOLS};drop:api-server:sql:http://x:1/sse;set:api-server:mcp_servers.sql.transport=sse;pin:api-server:${TOOLS};drop:api-server::http://y:2/sse;" ] \
  || fail "SQL_MCP_*_URL and timeouts override the manifest on api-server ($calls)"
run browser; got
[ "$calls" = "drop:browser::http://x:1/sse;register:browser:sql:http://y:2/sse:90:10:${TOOLS};drop:browser:sql:http://y:2/sse;set:browser:mcp_servers.sql.transport=sse;pin:browser:${TOOLS};" ] \
  || fail "SQL_MCP_*_URL and timeouts override the manifest on browser ($calls)"
unset SQL_MCP_RO_URL SQL_MCP_RW_URL SQL_MCP_TIMEOUT SQL_MCP_CONNECT_TIMEOUT

# reconcile_stack_eager_tools re-pins the same tools under both names.
SQL_MCP_ENABLED=1
want=""
for t in $TOOLS; do want="${want} ${t} mcp__sql__${t}"; done
for profile in api-server browser ""; do
  [ "$(addon_mcp_pin_args "$profile")" = "$want" ] || fail "pin args for ${profile:-default}"
done
SQL_MCP_ENABLED=0
[ -z "$(addon_mcp_pin_args browser)" ] || fail "no pin args with the flag off"

# The bootstrap carries no SQL-specific registration any more.
grep -q 'register_sql_mcp_servers\|SQL_MCP_TOOLS\|SQL_MCP_ENABLED' "$bootstrap" \
  && fail "SQL registration must come from the manifest only"

printf 'OK SQL MCP registration\n'
