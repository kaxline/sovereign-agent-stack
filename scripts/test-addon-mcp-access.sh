#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# An add-on whose mcp.service maps ro and rw to separate compose services
# (scripts/fixtures/addons/split): each profile registers the URL for its
# access and drops the other, and addon_mcp_urls lists both.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/split"
cp "${ROOT}/scripts/fixtures/addons/split/addon.json" "${ADDONS_DIR}/split/addon.json"

for fn in addon_mcp_plan addon_mcp_urls register_addon_mcp_servers mcp_tool_name; do
  range='/^'"${fn}"'() {/,/^}/p'
  # shellcheck disable=SC1090
  eval "$(sed -n "$range" "$bootstrap")"
done

log() { :; }

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

record="$(mktemp)"
trap 'rm -rf "$record" "$ADDONS_DIR"' EXIT
run() { register_addon_mcp_servers "$1" > /dev/null; }
register_mcp_server() { printf 'register:%s:%s:%s:%s;' "${1:-default}" "$2" "$4" "$(shift 6; echo "$*")" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
drop_mcp_entries_for_url() { printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

RO="http://split-ro:9000/mcp"
RW="http://split-rw:9000/mcp"
TOOLS="get_a get_b set_a"

export SPLIT_MCP_ENABLED=1
run ""; got
[ "$calls" = "drop:default:${RO};register:default:split:${RW}:${TOOLS};pin:default:;" ] \
  || fail "default registers the rw URL after dropping ro ($calls)"
run browser; got
[ "$calls" = "drop:browser:${RO};register:browser:split:${RW}:${TOOLS};pin:browser:;" ] \
  || fail "browser registers the rw URL after dropping ro ($calls)"
run api-server; got
[ "$calls" = "drop:api-server:${RW};register:api-server:split:${RO}:${TOOLS};pin:api-server:;" ] \
  || fail "api-server registers the ro URL after dropping rw ($calls)"

SPLIT_MCP_ENABLED=0
for p in "" browser api-server; do
  run "$p"; got
  [ "$calls" = "drop:${p:-default}:${RO};drop:${p:-default}:${RW};" ] \
    || fail "flag off drops both URLs on ${p:-default} ($calls)"
done

# A profile the manifest leaves out gets no entry, and both URLs go.
SPLIT_MCP_ENABLED=1
run research; got
[ "$calls" = "drop:research:${RO};drop:research:${RW};" ] \
  || fail "a profile with no access drops both URLs ($calls)"

urls="$(addon_mcp_urls "")"
[ "$urls" = "${RO}
${RW}" ] || fail "addon_mcp_urls lists both URLs ($urls)"

printf 'OK add-on MCP access levels\n'
