#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Which Hermes profiles hermes-*-bootstrap gives the Actual Budget MCP servers
# and which tools, read from the real addons/actual/addon.json.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
# Only Actual's manifest: other add-ons add their own drop calls.
ADDONS_DIR="$(mktemp -d)"
mkdir "${ADDONS_DIR}/actual"
cp "${ROOT}/addons/actual/addon.json" "${ADDONS_DIR}/actual/addon.json"
ADDON_MANIFEST_PY="${ROOT}/scripts/lib/addon_manifest.py"

for fn in addon_mcp_plan addon_mcp_pin_args addon_mcp_urls register_addon_mcp_servers mcp_tool_name; do
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

RO="http://actual-mcp-ro:3000/mcp"
RW="http://actual-mcp-rw:3000/mcp"
READ="get-transactions spending-by-category monthly-summary balance-history budget-vs-actual net-worth category-trends spending-by-payee cash-flow get-accounts get-grouped-categories get-payees get-rules get-custom-reports get-dashboards get-budget-months get-budget-month get-schedules"
WRITE="add-dashboard-widget batch-update-transactions create-category create-category-group create-custom-report create-dashboard-page create-payee create-rule create-transaction delete-category delete-category-group delete-custom-report delete-dashboard-page delete-payee delete-rule delete-transaction hold-budget-for-next-month import-transactions merge-payees organize-dashboard remove-dashboard-widget rename-dashboard-page reset-budget-hold set-budget-amount set-budget-carryover update-category update-category-group update-custom-report update-dashboard-widget update-payee update-rule update-transaction"
PINS="get-accounts get-grouped-categories get-transactions monthly-summary get-budget-month get-payees"

# The registered tool list is the read tools, then the write tools, in manifest order.
ALL="$READ $WRITE"
# Pins keep manifest order, not the order of $PINS above.
pins_of() { python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["mcp"]["pin"]))' "${ADDONS_DIR}/actual/addon.json"; }
[ "$(pins_of)" = "$PINS" ] || fail "manifest pins ($(pins_of))"

unset ACTUAL_ACCESS
export ACTUAL_MCP_ENABLED=1
for profile in "" browser; do
  p="${profile:-default}"
  run "$profile"; got
  [ "$calls" = "drop:${p}:${RO};register:${p}:budget:${RW}:${ALL};drop:${p}:${RW};pin:${p}:${PINS};" ] \
    || fail "${p} registers rw with all tools ($calls)"
done
run api-server; got
[ "$calls" = "drop:api-server:${RW};register:api-server:budget:${RO}:${READ};drop:api-server:${RO};pin:api-server:${PINS};" ] \
  || fail "api-server registers ro with read tools only ($calls)"

export ACTUAL_ACCESS=read
run ""; got
[ "$calls" = "drop:default:${RW};register:default:budget:${RO}:${READ};drop:default:${RO};pin:default:${PINS};" ] \
  || fail "ACTUAL_ACCESS=read puts default on ro ($calls)"
export ACTUAL_ACCESS=nope
for p in "" browser api-server; do
  run "$p"; got
  [ "$calls" = "drop:${p:-default}:${RO};drop:${p:-default}:${RW};" ] \
    || fail "ACTUAL_ACCESS=nope registers nothing on ${p:-default} ($calls)"
done
unset ACTUAL_ACCESS

ACTUAL_MCP_ENABLED=0
for p in "" browser api-server research; do
  run "$p"; got
  [ "$calls" = "drop:${p:-default}:${RO};drop:${p:-default}:${RW};" ] \
    || fail "flag off drops both URLs on ${p:-default} ($calls)"
done
ACTUAL_MCP_ENABLED=1

# cron "ro": the rw URL is fenced out of cron, the ro URL and the name are not.
fence="$(python3 -I - "$ADDONS_DIR" "$ROOT/scripts/lib" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
import addon_manifest
from pathlib import Path
names, urls = addon_manifest.cron_fence(Path(sys.argv[1]))
print(" ".join(names), "|", " ".join(urls))
PY
)"
case "$fence" in
  *"budget"*"|"*) fail "cron fence must not carry the name budget ($fence)" ;;
esac
case "$fence" in
  *"${RW}"*) ;;
  *) fail "cron fence lists the rw URL ($fence)" ;;
esac
case "$fence" in
  *"${RO}"*) fail "cron fence leaves the ro URL ($fence)" ;;
esac

# Pins are re-pinned under the prefixed name too.
[ "$(mcp_tool_name budget get-accounts)" = "mcp__budget__get_accounts" ] || fail "mcp_tool_name"
args="$(addon_mcp_pin_args api-server)"
for t in $PINS; do
  case "$args" in
    *" $t mcp__budget__$(printf '%s' "$t" | tr - _)"*) ;;
    *) fail "pin args carry $t ($args)" ;;
  esac
done
[ -z "$(addon_mcp_pin_args research)" ] || fail "no pin args on a profile the manifest leaves out"

printf 'OK Actual Budget MCP registration\n'
