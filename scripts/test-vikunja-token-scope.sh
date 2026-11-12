#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Vikunja token and pins hermes-*-bootstrap hands each profile. The todos
# entry comes from addons/vikunja/addon.json through the generic add-on path
# (register_addon_mcp_servers), with mcp.auth: the read-only token on
# api-server, the read/write one on default and browser, copied into the
# profile's .env as TODOS_KEY. No tools.include: the token's scopes decide.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
# Only Vikunja's manifest: other add-ons add their own calls.
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/vikunja"
cp "${ROOT}/addons/vikunja/addon.json" "${ADDONS_DIR}/vikunja/addon.json" 2>/dev/null || true
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args register_addon_mcp_servers mcp_tool_name

log() { :; }

[ -f "${ADDONS_DIR}/vikunja/addon.json" ] || fail "addons/vikunja/addon.json not found"

record="$(mktemp)"
trap 'rm -rf "$record" "$ADDONS_DIR"' EXIT
run() {
  register_addon_mcp_servers "$1" > /dev/null
}
profile_env_file() { printf '%s/%s.env\n' "$ADDONS_DIR" "${1:-default}"; }
register_mcp_server() { printf 'register:%s:%s;' "${1:-default}" "$2" >> "$record"; }
write_bearer_mcp_entry() { printf 'bearer:%s:%s:%s:%s:%s:%s:%s %s;' "${1:-default}" "$2" "$3" "$4" "$5" "$6" "$7" "$8" >> "$record"; }
upsert_env() { printf 'env:%s:%s=%s;' "$(basename "$1" .env)" "$2" "$3" >> "$record"; }
remove_env_key() { printf 'unenv:%s:%s;' "$(basename "$1" .env)" "$2" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
drop_mcp_entries_for_url() { printf 'drop:%s:%s:%s;' "${1:-default}" "$2" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

unset VIKUNJA_MCP_URL VIKUNJA_MCP_TIMEOUT VIKUNJA_MCP_CONNECT_TIMEOUT
URL="http://vikunja:3456/api/v2/mcp"
PIN_RO="tasks_list tasks_read projects_list"
PIN_RW="tasks_list tasks_read tasks_create tasks_update projects_list"
export VIKUNJA_MCP_TOKEN_RO=ro-token VIKUNJA_MCP_TOKEN_RW=rw-token

# Off: the URL and the profile's key are dropped on every profile.
for flag in 0 "" false; do
  export VIKUNJA_MCP_ENABLED="$flag"
  for profile in api-server browser ""; do
    run "$profile"; got
    p="${profile:-default}"
    [ "$calls" = "drop:${p}::${URL};unenv:${p}:TODOS_KEY;" ] \
      || fail "VIKUNJA_MCP_ENABLED='${flag}': ${p} should drop the entry and its key ($calls)"
  done
done

export VIKUNJA_MCP_ENABLED=1
run api-server; got
[ "$calls" = "env:api-server:TODOS_KEY=ro-token;bearer:api-server:todos:${URL}:TODOS_KEY:60:30:Authorization Bearer;drop:api-server:todos:${URL};pin:api-server:${PIN_RO};" ] \
  || fail "api-server must get the read-only token and read pins ($calls)"
for profile in browser ""; do
  run "$profile"; got
  p="${profile:-default}"
  [ "$calls" = "env:${p}:TODOS_KEY=rw-token;bearer:${p}:todos:${URL}:TODOS_KEY:60:30:Authorization Bearer;drop:${p}:todos:${URL};pin:${p}:${PIN_RW};" ] \
    || fail "${p} must get the read/write token and pins ($calls)"
done

# A missing read-only token never falls back to the read/write one.
VIKUNJA_MCP_TOKEN_RO=
run api-server; got
[ "$calls" = "drop:api-server::${URL};unenv:api-server:TODOS_KEY;" ] \
  || fail "api-server must not fall back to the read/write token ($calls)"
[ -z "$(addon_mcp_pin_args api-server)" ] || fail "no pins on api-server without its token"
VIKUNJA_MCP_TOKEN_RO=ro-token

# reconcile_stack_eager_tools re-pins under both names.
want=""
for t in $PIN_RO; do want="${want} ${t} mcp__todos__${t}"; done
[ "$(addon_mcp_pin_args api-server)" = "$want" ] || fail "pin args for api-server"
want=""
for t in $PIN_RW; do want="${want} ${t} mcp__todos__${t}"; done
[ "$(addon_mcp_pin_args browser)" = "$want" ] || fail "pin args for browser"

# The plan names the variables, never the tokens.
addon_mcp_plan browser | grep -q 'rw-token' && fail "mcp-plan printed a token"

# The bootstrap carries no Vikunja-specific registration any more.
grep -q 'register_vikunja_mcp_servers\|VIKUNJA_MCP_TOKEN' "$bootstrap" \
  && fail "Vikunja registration must come from the manifest only"

printf 'OK vikunja token scope\n'
