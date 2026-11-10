#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# An add-on whose mcp.service maps ro and rw to separate compose services
# (scripts/fixtures/addons/split): each profile registers the URL for its
# access and drops the other, and addon_mcp_urls lists both.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/split"
cp "${ROOT}/scripts/fixtures/addons/split/addon.json" "${ADDONS_DIR}/split/addon.json"

load_fns "$bootstrap" addon_mcp_plan addon_mcp_pin_args addon_mcp_urls register_addon_mcp_servers \
  mcp_tool_name

log() { :; }

record="$(mktemp)"
trap 'rm -rf "$record" "$ADDONS_DIR"' EXIT
run() { register_addon_mcp_servers "$1" > /dev/null; }
register_mcp_server() { printf 'register:%s:%s:%s:%s;' "${1:-default}" "$2" "$4" "$(shift 6; echo "$*")" >> "$record"; }
hermes_config_set() { printf 'set:%s:%s=%s;' "${1:-default}" "$2" "$3" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "${1:-default}" "$(shift 2; echo "$*")" >> "$record"; }
# shellcheck disable=SC2317,SC2329  # called by the sourced bootstrap; redefined below
drop_mcp_entries_for_url() { printf 'drop:%s:%s;' "${1:-default}" "$3" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }

RO="http://split-ro:9000/mcp"
RW="http://split-rw:9000/mcp"
TOOLS="get_a get_b set_a"

export SPLIT_MCP_ENABLED=1
run ""; got
[ "$calls" = "drop:default:${RO};register:default:split:${RW}:${TOOLS};drop:default:${RW};pin:default:get_a set_a;" ] \
  || fail "default registers the rw URL after dropping ro ($calls)"
run browser; got
[ "$calls" = "drop:browser:${RO};register:browser:split:${RW}:${TOOLS};drop:browser:${RW};pin:browser:get_a set_a;" ] \
  || fail "browser registers the rw URL after dropping ro ($calls)"
run api-server; got
[ "$calls" = "drop:api-server:${RW};register:api-server:split:${RO}:get_a get_b;drop:api-server:${RO};pin:api-server:get_a;" ] \
  || fail "api-server registers the ro URL after dropping rw ($calls)"

# set_a is a writeTool: no pin args for it on api-server either.
[ "$(addon_mcp_pin_args api-server)" = " get_a mcp__split__get_a" ] \
  || fail "api-server pin args leave out set_a ($(addon_mcp_pin_args api-server))"
[ "$(addon_mcp_pin_args default)" = " get_a mcp__split__get_a set_a mcp__split__set_a" ] \
  || fail "default pin args keep set_a ($(addon_mcp_pin_args default))"

SPLIT_MCP_ENABLED=0
for p in "" browser api-server; do
  run "$p"; got
  [ "$calls" = "drop:${p:-default}:${RO};drop:${p:-default}:${RW};" ] \
    || fail "flag off drops both URLs on ${p:-default} ($calls)"
done

# SPLIT_ACCESS caps the manifest's level. Unknown values fail closed.
SPLIT_MCP_ENABLED=1
export SPLIT_ACCESS=read
run ""; got
[ "$calls" = "drop:default:${RW};register:default:split:${RO}:get_a get_b;drop:default:${RO};pin:default:get_a;" ] \
  || fail "read caps default to the ro URL and read tools ($calls)"
for v in write ""; do
  export SPLIT_ACCESS="$v"
  run ""; got
  [ "$calls" = "drop:default:${RO};register:default:split:${RW}:${TOOLS};drop:default:${RW};pin:default:get_a set_a;" ] \
    || fail "SPLIT_ACCESS='$v' keeps rw ($calls)"
done
unset SPLIT_ACCESS
run ""; got
case "$calls" in "drop:default:${RO};register:default:split:${RW}:"*) ;; *) fail "unset keeps rw ($calls)" ;; esac
for v in Read ask readonly; do
  export SPLIT_ACCESS="$v"
  for p in "" api-server; do
    run "$p"; got
    [ "$calls" = "drop:${p:-default}:${RO};drop:${p:-default}:${RW};" ] \
      || fail "SPLIT_ACCESS=$v registers nothing on ${p:-default} ($calls)"
  done
done
unset SPLIT_ACCESS

# A profile the manifest leaves out gets no entry, and both URLs go.
SPLIT_MCP_ENABLED=1
run research; got
[ "$calls" = "drop:research:${RO};drop:research:${RW};" ] \
  || fail "a profile with no access drops both URLs ($calls)"

urls="$(addon_mcp_urls)"
[ "$urls" = "${RO}
${RW}" ] || fail "addon_mcp_urls lists both URLs ($urls)"

# After registering, every other key at the add-on's URL goes (a profile cloned
# from default can carry an unfiltered entry for the same server).
drop_mcp_entries_for_url() { printf 'drop:%s:%s:%s;' "${1:-default}" "$2" "$3" >> "$record"; }
addon_mcp_plan() {
  printf '%s\n' 'demo|Demo|1|rw|http://demo:8000/mcp|60|10|http|tool_a tool_b|tool_a|http://demo:8000/mcp'
}
run browser; got
[ "$calls" = "register:browser:demo:http://demo:8000/mcp:tool_a tool_b;drop:browser:demo:http://demo:8000/mcp;pin:browser:tool_a;" ] \
  || fail "registering drops other keys at the URL ($calls)"

printf 'OK add-on MCP access levels\n'
