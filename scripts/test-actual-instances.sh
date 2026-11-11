#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Connected Actual budgets (<data root>/addons/actual/instances) as Hermes MCP
# entries through the generic mcp.instances path: addon_manifest.py
# instances-plan on a synthetic store with the real Actual manifest, the cron
# fence for its {access} URLs, and the bootstrap's register_addon_instance_servers
# with stubbed writers for an entry that takes no key. No secret is involved:
# the plan and the bootstrap never read Actual's secrets/.
#
#   sh scripts/test-actual-instances.sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
manifest_py="${ROOT}/scripts/lib/addon_manifest.py"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
data="${tmp}/addon-data"
idir="${data}/actual/instances"
mkdir -p "$idir" "${data}/actual/secrets"
row() { printf '%s\n' "$2" > "${idir}/$1.json"; }
# A secret file the plan must never read or print.
printf 'tok-never-printed' > "${data}/actual/secrets/home.session_token"

READ="get-transactions spending-by-category monthly-summary balance-history budget-vs-actual net-worth category-trends spending-by-payee cash-flow get-accounts get-grouped-categories get-payees get-rules get-custom-reports get-dashboards get-budget-months get-budget-month get-schedules"
WRITE="add-dashboard-widget batch-update-transactions create-category create-category-group create-custom-report create-dashboard-page create-payee create-rule create-transaction delete-category delete-category-group delete-custom-report delete-dashboard-page delete-payee delete-rule delete-transaction hold-budget-for-next-month import-transactions merge-payees organize-dashboard remove-dashboard-widget rename-dashboard-page reset-budget-hold set-budget-amount set-budget-carryover update-category update-category-group update-custom-report update-dashboard-widget update-payee update-rule update-transaction"
ALL="$READ $WRITE"
PINS="get-accounts get-grouped-categories get-transactions monthly-summary get-budget-month get-payees"
[ "$(echo "$READ" | wc -w | tr -d ' ')" = 18 ] || fail "fixture: 18 read tools"

row home '{"server": "local", "sync_id": "s1", "bind": {"default": "rw", "browser": "rw", "api-server": "ro"}}'
row main '{"server": "https://budget.example.com", "sync_id": "s2"}'
row solo '{"server": "local", "sync_id": "s3", "bind": {"default": "rw"}}'
row bad '{"server": "local", "sync_id": "s4", "bind": {"api-server": "rw"}}'

ON="ACTUAL_MCP_ENABLED=1"
plan() { env "$ON" python3 "$manifest_py" instances-plan "${ROOT}/addons" "$data" "$1" 2> "${tmp}/plan.err"; }
# line PROFILE NAME: that instance's plan line.
line() { plan "$1" | grep "^$2|" || true; }
field() { printf '%s' "$1" | cut -d'|' -f"$2"; }

RO_HOME="http://actual-mcp-ro:3000/home/mcp"
RW_HOME="http://actual-mcp-rw:3000/home/mcp"
RO_MAIN="http://actual-mcp-ro:3000/main/mcp"

# --- instances-plan ----------------------------------------------------------
case "$(plan default)" in *tok-never-printed*) fail "the plan prints a secret" ;; esac
l="$(line default 'budget-\*')"
[ "$(field "$l" 3)" = 1 ] && [ "$(field "$l" 10)" = ACTUAL_MCP_ENABLED ] || fail "the add-on line ($l)"

l="$(line default budget-home)"
[ "$(field "$l" 3)" = 1 ] && [ "$(field "$l" 4)" = "$RW_HOME" ] || fail "bind rw on default gives the rw URL ($l)"
[ "$(field "$l" 5)$(field "$l" 6)$(field "$l" 11)$(field "$l" 12)" = "" ] || fail "no key, key file or header ($l)"
[ "$(field "$l" 13)" = "$ALL" ] && [ "$(field "$l" 9)" = "$PINS" ] || fail "rw gets every tool and pin ($l)"
l="$(line api-server budget-home)"
[ "$(field "$l" 4)" = "http://actual-mcp-ro:3000/home/mcp" ] && [ "$(field "$l" 13)" = "$READ" ] \
  || fail "bind ro on api-server gives the ro URL and read tools ($l)"

for prof in default browser api-server; do
  l="$(line "$prof" budget-main)"
  [ "$(field "$l" 3)" = 1 ] && [ "$(field "$l" 4)" = "$RO_MAIN" ] && [ "$(field "$l" 13)" = "$READ" ] \
    || fail "no bind: ${prof} gets the manifest's ro ($l)"
done

l="$(line browser budget-solo)"
[ "$(field "$l" 3)" = 0 ] && [ "$(field "$l" 10)" = "profile not in the instance's bind" ] \
  || fail "a profile the bind leaves out gets the instance off ($l)"
l="$(line default budget-bad)"
case "$(field "$l" 3)|$(field "$l" 10)" in
  0\|bind*api-server*) ;;
  *) fail "bind rw on api-server turns the instance off ($l)" ;;
esac

ON="ACTUAL_MCP_ENABLED=0"
l="$(line default budget-home)"
[ "$(field "$l" 3)" = 0 ] && [ "$(field "$l" 10)" = "flag ACTUAL_MCP_ENABLED is off" ] || fail "flag off ($l)"
ON="ACTUAL_MCP_ENABLED=1"
echo "ok: instances-plan"

# --- cron fence: cron "ro" fences each budget's rw URL, no name ----------------
fence="$(python3 - "${ROOT}/addons" "$data" "$manifest_py" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[3]).parent))
import addon_manifest as am
names, urls = am.cron_fence(Path(sys.argv[1]), {}, data_dir=Path(sys.argv[2]))
print(" ".join(n for n in names if n.startswith("budget")) + "|" + " ".join(u for u in urls if "actual" in u))
PY
)"
want="|http://actual-mcp-rw:3000/bad/mcp ${RW_HOME} http://actual-mcp-rw:3000/main/mcp http://actual-mcp-rw:3000/solo/mcp"
[ "$fence" = "$want" ] || fail "cron fence (got $fence)"
echo "ok: cron fence"

# --- register_addon_instance_servers with stubbed writers ----------------------
load_fns "$bootstrap" addon_instances_plan register_addon_instance_servers addon_instances_warn_none
ADDONS_DIR="${ROOT}/addons"
ADDON_DATA_DIR="$data"
ADDON_MANIFEST_PY="$manifest_py"
ACTUAL_MCP_ENABLED=1
TWENTY_MCP_ENABLED=0
export ACTUAL_MCP_ENABLED TWENTY_MCP_ENABLED
log() { :; }
record="${tmp}/record"
: > "$record"
caldav_profile_env_file() { echo "${tmp}/env/${1:-default}.env"; }
register_mcp_server() { printf 'register:%s:%s:%s:%s;' "${1:-default}" "$2" "$4" "$(shift 6; echo "$*")" >> "$record"; }
write_bearer_mcp_entry() { printf 'bearer:%s;' "$2" >> "$record"; }
upsert_env() { printf 'env:%s;' "$2" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s;' "$2" "$(shift 2; echo "$*")" >> "$record"; }
reconcile_addon_instance_servers() { printf 'reconcile:%s;' "$(shift; echo "$*")" >> "$record"; }
register_addon_instance_servers ""
calls="$(cat "$record")"
case "$calls" in *bearer:*|*env:*) fail "a keyless entry writes a bearer header or a key ($calls)" ;; esac
case "$calls" in *"register:default:budget-home:${RW_HOME}:${ALL};"*) ;; *) fail "budget-home registers rw with every tool ($calls)" ;; esac
case "$calls" in *"register:default:budget-main:${RO_MAIN}:${READ};"*) ;; *) fail "budget-main registers ro with read tools ($calls)" ;; esac
case "$calls" in *"register:default:budget-bad:"*) fail "an instance with a bad bind registers ($calls)" ;; esac
case "$calls" in *"reconcile:"*"budget-home budget-main budget-solo;"*) ;; *) fail "reconcile keeps the registered names ($calls)" ;; esac
echo "ok: register_addon_instance_servers (stubs)"

# --- wiring --------------------------------------------------------------------
grep -q 'actual_rows' "$bootstrap" && fail "the bootstrap still names actual_rows"
for svc in hermes-api-bootstrap hermes-browser-bootstrap; do
  sed -n "/^  ${svc}:\$/,/^  [a-z-]*:\$/p" "${ROOT}/docker-compose.yml" > "${tmp}/svc"
  grep -q 'actual_rows\|ACTUAL_BUDGET_FILE\|ACTUAL_ACCESS' "${tmp}/svc" \
    && fail "${svc} still carries the single budget or actual_rows.py"
done
echo "ok: wiring"
