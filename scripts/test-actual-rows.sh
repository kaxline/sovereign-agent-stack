#!/bin/sh
# shellcheck disable=SC2034  # read by the eval'd bootstrap functions
# Connected Actual budgets ("rows", <data root>/addons/actual/instances) as
# Hermes MCP entries: actual_rows.py plan on fixture rows, the bootstrap's
# register_actual_budget_rows with stubbed writers, then the real writers in
# the pinned Hermes image against a scratch config (skipped when Docker or
# the image is missing unless --image is given). No secret is involved: the
# bootstrap never reads secrets/.
#
#   sh scripts/test-actual-rows.sh [--host-only | --image]
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"
rows_py="${ROOT}/scripts/lib/actual_rows.py"
image="nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG:-v0.22.7}"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

[ -f "$rows_py" ] || fail "scripts/lib/actual_rows.py exists"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
idir="${tmp}/addon-data/actual/instances"
mkdir -p "$idir"
row() { printf '%s\n' "$2" > "${idir}/$1.json"; }

READ="get-transactions spending-by-category monthly-summary balance-history budget-vs-actual net-worth category-trends spending-by-payee cash-flow get-accounts get-grouped-categories get-payees get-rules get-custom-reports get-dashboards get-budget-months get-budget-month get-schedules"
WRITE="add-dashboard-widget batch-update-transactions create-category create-category-group create-custom-report create-dashboard-page create-payee create-rule create-transaction delete-category delete-category-group delete-custom-report delete-dashboard-page delete-payee delete-rule delete-transaction hold-budget-for-next-month import-transactions merge-payees organize-dashboard remove-dashboard-widget rename-dashboard-page reset-budget-hold set-budget-amount set-budget-carryover update-category update-category-group update-custom-report update-dashboard-widget update-payee update-rule update-transaction"
ALL="$READ $WRITE"
PINS="get-accounts get-grouped-categories get-transactions monthly-summary get-budget-month get-payees"
[ "$(echo "$READ" | wc -w | tr -d ' ')" = 18 ] || fail "fixture: 18 read tools"

plan() { python3 "$rows_py" plan "$idir" "${ROOT}/addons" "$1" 2> "${tmp}/plan.err"; }

# --- actual_rows.py plan -----------------------------------------------------

[ -z "$(plan default)" ] || fail "an empty store plans nothing"
[ -z "$(python3 "$rows_py" plan "${tmp}/nope" "${ROOT}/addons" default)" ] || fail "a missing store plans nothing"

row main '{"server": "local", "sync_id": "sync-main", "profile": "default", "access": "read"}'
want_main_ro="budget-main-ro|Actual Budget 'main' (read-only)|http://actual-mcp-ro:3000/main/mcp|120|60|${READ}|${PINS}"
[ "$(plan default)" = "$want_main_ro" ] || fail "a read row gives one -ro line with the 18 read tools ($(plan default))"

row home '{"server": "https://actual.example.com", "sync_id": "sync-home", "profile": "default", "access": "write"}'
want_home_ro="budget-home-ro|Actual Budget 'home' (read-only)|http://actual-mcp-ro:3000/home/mcp|120|60|${READ}|${PINS}"
want_home_rw="budget-home|Actual Budget 'home'|http://actual-mcp-rw:3000/home/mcp|120|60|${ALL}|${PINS}"
want="$(printf '%s\n%s\n%s' "$want_home_ro" "$want_home_rw" "$want_main_ro")"
[ "$(plan default)" = "$want" ] || fail "a write row gives -ro and the full list, slugs in order ($(plan default))"
[ "$(plan browser)" = "$want" ] || fail "browser gets the same entries as default"
[ -z "$(plan api-server)" ] || fail "api-server gets nothing"
[ -z "$(plan research)" ] || fail "another profile gets nothing"
[ "$(python3 "$rows_py" plan "$idir" "${ROOT}/addons" "")" = "$want" ] || fail "an empty profile is default"

# Rows that are skipped, as the launcher's readRows skips them, plus a row for
# another profile (not in this build).
row alice '{"server": "local", "sync_id": "s", "profile": "alice", "access": "write"}'
row Bad_Slug '{"server": "local", "sync_id": "s"}'
row admin '{"server": "local", "sync_id": "s", "access": "admin"}'
row ftp '{"server": "ftp://actual.example.com", "sync_id": "s"}'
row nosync '{"server": "local"}'
row blank '{"server": "local", "sync_id": "  "}'
row list '["server", "local"]'
row broken '{"server": '
printf '{}\n' > "${idir}/sample.json.example"
[ "$(plan default)" = "$want" ] || fail "skipped rows plan nothing ($(plan default))"
for s in alice Bad_Slug admin ftp nosync blank list broken; do
  grep -q "row ${s}:" "${tmp}/plan.err" || fail "the skipped row ${s} is named on stderr ($(cat "${tmp}/plan.err"))"
done

# No access field means read.
row plain '{"server": "local", "sync_id": "s"}'
plan default | grep -qx "budget-plain-ro|.*|http://actual-mcp-ro:3000/plain/mcp|.*" || fail "a row with no access is read"
plan default | grep -q '^budget-plain|' && fail "a row with no access gets no write entry"
rm -f "${idir}"/alice.json "${idir}"/Bad_Slug.json "${idir}"/admin.json "${idir}"/ftp.json "${idir}"/nosync.json \
  "${idir}"/blank.json "${idir}"/list.json "${idir}"/broken.json "${idir}"/plain.json

# An invalid Actual manifest fails the plan (the bootstrap then leaves entries as they were).
mkdir -p "${tmp}/bad-addons/actual"
printf '{"id": "io.github.kaxline.stack.actual"}\n' > "${tmp}/bad-addons/actual/addon.json"
if python3 "$rows_py" plan "$idir" "${tmp}/bad-addons" default > /dev/null 2>&1; then
  fail "an invalid Actual manifest fails the plan"
fi
mkdir -p "${tmp}/no-actual"
[ -z "$(python3 "$rows_py" plan "$idir" "${tmp}/no-actual" default)" ] || fail "no Actual manifest plans nothing"
echo "ok: plan"

# --- register_actual_budget_rows with stubbed writers ------------------------

for fn in actual_rows_plan actual_rows_on actual_single_budget_off register_actual_budget_rows \
    actual_row_urls actual_row_pin_args mcp_tool_name; do
  range='/^'"${fn}"'() {/,/^}/p'
  # shellcheck disable=SC1090
  eval "$(sed -n "$range" "$bootstrap")"
  command -v "$fn" >/dev/null 2>&1 || fail "${fn} missing from bootstrap"
done
ADDONS_DIR="${ROOT}/addons"
ADDON_DATA_DIR="${tmp}/addon-data"
ACTUAL_ROWS_PY="$rows_py"
log() { :; }
record="${tmp}/record"
: > "$record"
register_mcp_server() { printf 'register:%s:%s:%s;' "${1:-default}" "$2" "$4" >> "$record"; }
pin_mcp_tools() { printf 'pin:%s:%s:%s;' "${1:-default}" "$2" "$(shift 2; echo "$*")" >> "$record"; }
reconcile_actual_budget_rows() { printf 'reconcile:%s:%s;' "${1:-default}" "$(shift; echo "$*")" >> "$record"; }
got() { calls="$(cat "$record")"; : > "$record"; }
run() { register_actual_budget_rows "$1"; got; }

RO_MAIN="http://actual-mcp-ro:3000/main/mcp"
RO_HOME="http://actual-mcp-ro:3000/home/mcp"
RW_HOME="http://actual-mcp-rw:3000/home/mcp"

export ACTUAL_MCP_ENABLED=1 ACTUAL_BUDGET_FILE=./compose/actual/budget.env
for p in "" browser; do
  run "$p"
  d="${p:-default}"
  [ "$calls" = "register:${d}:budget-home-ro:${RO_HOME};pin:${d}:budget-home-ro:${PINS};register:${d}:budget-home:${RW_HOME};pin:${d}:budget-home:${PINS};register:${d}:budget-main-ro:${RO_MAIN};pin:${d}:budget-main-ro:${PINS};reconcile:${d}:rows keep budget-home-ro budget-home budget-main-ro;" ] \
    || fail "${d} registers the rows and keeps them ($calls)"
done
run api-server
[ "$calls" = "reconcile:api-server:all keep;" ] || fail "api-server registers nothing and drops every row entry ($calls)"

for f in "" ./compose/actual/no-budget.env /abs/compose/actual/no-budget.env; do
  ACTUAL_BUDGET_FILE="$f"
  run ""
  case "$calls" in
    *"reconcile:default:rows drop-legacy budget-home-ro budget-home budget-main-ro;") ;;
    *) fail "ACTUAL_BUDGET_FILE='${f}' drops the legacy budget entry ($calls)" ;;
  esac
  run api-server
  [ "$calls" = "reconcile:api-server:all drop-legacy;" ] || fail "api-server drops legacy too with '${f}' ($calls)"
done
unset ACTUAL_BUDGET_FILE
run ""
case "$calls" in *"rows drop-legacy "*) ;; *) fail "an unset ACTUAL_BUDGET_FILE drops the legacy entry ($calls)" ;; esac
ACTUAL_BUDGET_FILE=./compose/actual/budget.env

for off in 0 "" false; do
  ACTUAL_MCP_ENABLED="$off"
  for p in "" browser api-server; do
    run "$p"
    [ "$calls" = "reconcile:${p:-default}:all keep;" ] || fail "flag '${off}' registers nothing and drops row entries on ${p:-default} ($calls)"
  done
done
ACTUAL_MCP_ENABLED=1

# A plan that fails leaves entries alone.
ACTUAL_ROWS_PY="${tmp}/missing.py"
run "" 2>/dev/null
[ -z "$calls" ] || fail "a failed plan changes nothing ($calls)"
ACTUAL_ROWS_PY="$rows_py"

# Parked retry and eager pins follow the plan.
[ "$(actual_row_urls "" | tr '\n' ' ')" = "${RO_HOME} ${RW_HOME} ${RO_MAIN} " ] || fail "actual_row_urls ($(actual_row_urls ""))"
[ -z "$(actual_row_urls api-server)" ] || fail "no row URLs on api-server"
args="$(actual_row_pin_args browser)"
for n in budget_home_ro budget_home budget_main_ro; do
  case "$args" in *" get-accounts mcp__${n}__get_accounts"*) ;; *) fail "pin args carry ${n} ($args)" ;; esac
done
ACTUAL_MCP_ENABLED=0
[ -z "$(actual_row_urls "")$(actual_row_pin_args "")" ] || fail "no row URLs or pins with the flag off"
ACTUAL_MCP_ENABLED=1

# Wiring: called after register_addon_mcp_servers on default and on $PROFILE,
# its URLs get the parked retry, its pins are healed, legacy pins stay out
# when the single budget is off, the script reaches the container and counts
# toward the bootstrap stamp.
grep -A1 '^  register_addon_mcp_servers ""$' "$bootstrap" | grep -q '^  register_actual_budget_rows ""$' \
  || fail "register_actual_budget_rows runs after register_addon_mcp_servers on default"
# shellcheck disable=SC2016  # literal bootstrap text
grep -A1 '^register_addon_mcp_servers "\$PROFILE"$' "$bootstrap" | grep -q '^register_actual_budget_rows "\$PROFILE"$' \
  || fail "register_actual_budget_rows runs after register_addon_mcp_servers on \$PROFILE"
# shellcheck disable=SC2016  # literal bootstrap text
grep -q '\$(actual_row_urls "\$1")' "$bootstrap" || fail "set_stack_mcp_parked_retry covers row URLs"
# shellcheck disable=SC2016
grep -q '\$(actual_row_pin_args "\$_rst_profile")' "$bootstrap" || fail "reconcile_stack_eager_tools re-pins row tools"
# shellcheck disable=SC2016
grep -q 'cat "\$ACTUAL_ROWS_PY"' "$bootstrap" || fail "actual_rows.py is part of the bootstrap stamp"
for svc in hermes-api-bootstrap hermes-browser-bootstrap; do
  sed -n "/^  ${svc}:\$/,/^  [a-z-]*:\$/p" "${ROOT}/docker-compose.yml" > "${tmp}/svc"
  grep -qF -- '- ./scripts/lib/actual_rows.py:/bootstrap/actual_rows.py:ro' "${tmp}/svc" \
    || fail "${svc} mounts actual_rows.py next to addon_manifest.py"
done
echo "ok: register_actual_budget_rows (stubs)"

# --- budget.sh ensure creates the store before Docker does -------------------

droot="${tmp}/droot"
mkdir -p "$droot"
BUDGET_DATA_ROOT="$droot" bash "${ROOT}/scripts/budget.sh" ensure > /dev/null || fail "budget.sh ensure"
for d in addons/actual addons/actual/instances addons/actual/secrets; do
  [ -d "${droot}/${d}" ] || fail "budget.sh ensure creates ${d}"
  mode="$(stat -f %Lp "${droot}/${d}" 2>/dev/null || stat -c %a "${droot}/${d}")"
  [ "$mode" = 700 ] || fail "${d} is 0700, not ${mode}"
done
grep -q '^	@./scripts/budget.sh ensure$' "${ROOT}/Makefile" || fail "make ensure-local runs budget.sh ensure"
echo "ok: budget.sh ensure"

[ "${1:-}" = "--host-only" ] && exit 0
if [ "${1:-}" != "--image" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "skip image checks ($image not present)"
    exit 0
  fi
fi

# --- The real writers in the Hermes image ------------------------------------

work="${tmp}/image"
mkdir -p "${work}/home/profiles/api-server" "${work}/data/actual/instances"
{
  for fn in actual_rows_plan actual_rows_on actual_single_budget_off register_actual_budget_rows \
      reconcile_actual_budget_rows register_mcp_server set_yaml_list hermes_config_set config_flush \
      pin_mcp_tools mcp_tool_name pin_tool_search_always_include ensure_yaml_list_items \
      config_path_for python3; do
    sed -n "/^${fn}() {/,/^}/p" "$bootstrap"
  done
  cat <<'EOF'
log() { printf '%s\n' "$1"; }
HERMES_HOME=/w/home
ADDONS_DIR=/addons
ADDON_MANIFEST_PY=/addon_manifest.py
ACTUAL_ROWS_PY=/actual_rows.py
ADDON_DATA_DIR=/w/data
CONFIG_QUEUE="$(mktemp)"
register_actual_budget_rows "$1"
config_flush
EOF
} > "${work}/run.sh"

seed() {
  cat > "$1" <<'EOF'
mcp_servers:
  budget:
    url: http://actual-mcp-rw:3000/mcp
  budget-old-ro:
    url: http://actual-mcp-ro:3000/old/mcp
  my-budget:
    url: http://actual-mcp-ro:3000/main/mcp
  searxng:
    url: http://mcp-searxng:3000/mcp
tools:
  tool_search:
    always_include:
      - session_search
      - get-accounts
      - mcp__budget__get_accounts
      - mcp__budget_old_ro__get_accounts
      - mcp__searxng__web_url_read
EOF
}
cfg="${work}/home/config.yaml"
api_cfg="${work}/home/profiles/api-server/config.yaml"
irow() { printf '%s\n' "$2" > "${work}/data/actual/instances/$1.json"; }
irow main '{"server": "local", "sync_id": "sync-main", "profile": "default", "access": "read"}'
irow home '{"server": "https://actual.example.com", "sync_id": "sync-home", "profile": "default", "access": "write"}'

# irun <profile> <ACTUAL_MCP_ENABLED> <ACTUAL_BUDGET_FILE>
irun() {
  chmod -R a+rwX "$work"
  docker run --rm --entrypoint sh -v "${work}:/w" -v "${ROOT}/addons:/addons:ro" \
    -v "${ROOT}/scripts/lib/addon_manifest.py:/addon_manifest.py:ro" \
    -v "${rows_py}:/actual_rows.py:ro" \
    -e ACTUAL_MCP_ENABLED="$2" -e ACTUAL_BUDGET_FILE="$3" "$image" /w/run.sh "$1" > "${work}/out" 2>&1 \
    || { cat "${work}/out" >&2; fail "register_actual_budget_rows exited non-zero"; }
}

# q <config> <python expression over c (the config)>: prints its value.
q() {
  docker run --rm --entrypoint python3 -v "${work}:/w" "$image" -c '
import sys
sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:
    import hermes_yaml as yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
s = c.get("mcp_servers") or {}
pins = ((c.get("tools") or {}).get("tool_search") or {}).get("always_include") or []
print(eval(sys.argv[2]))
' "/w/${1#"${work}"/}" "$2"
}

seed "$cfg"
irun "" 1 ""
state="$(q "$cfg" '" ".join(sorted(s)) + "|" + s["budget-home"]["url"] + "|" + s["budget-home-ro"]["url"] + "|" + s["budget-main-ro"]["url"] + "|" + str(len(s["budget-main-ro"]["tools"]["include"])) + "|" + str(len(s["budget-home"]["tools"]["include"])) + "|" + str(s["budget-home"]["timeout"]) + "|" + " ".join(p for p in pins if p.startswith("mcp__budget"))')"
[ "$state" = "budget-home budget-home-ro budget-main-ro my-budget searxng|http://actual-mcp-rw:3000/home/mcp|http://actual-mcp-ro:3000/home/mcp|http://actual-mcp-ro:3000/main/mcp|18|50|120|mcp__budget_home_ro__get_accounts mcp__budget_home_ro__get_grouped_categories mcp__budget_home_ro__get_transactions mcp__budget_home_ro__monthly_summary mcp__budget_home_ro__get_budget_month mcp__budget_home_ro__get_payees mcp__budget_home__get_accounts mcp__budget_home__get_grouped_categories mcp__budget_home__get_transactions mcp__budget_home__monthly_summary mcp__budget_home__get_budget_month mcp__budget_home__get_payees mcp__budget_main_ro__get_accounts mcp__budget_main_ro__get_grouped_categories mcp__budget_main_ro__get_transactions mcp__budget_main_ro__monthly_summary mcp__budget_main_ro__get_budget_month mcp__budget_main_ro__get_payees" ] \
  || fail "default: rows registered, budget-old-ro and the legacy budget (and their pins) dropped, my-budget kept ($state)"

seed "$cfg"
irun "" 1 ./compose/actual/budget.env
state="$(q "$cfg" '" ".join(sorted(s)) + "|" + str("mcp__budget__get_accounts" in pins)')"
[ "$state" = "budget budget-home budget-home-ro budget-main-ro my-budget searxng|True" ] \
  || fail "the legacy budget entry stays while ACTUAL_BUDGET_FILE is set ($state)"

rm -f "${work}/data/actual/instances/home.json"
irun "" 1 ./compose/actual/budget.env
state="$(q "$cfg" '" ".join(sorted(s)) + "|" + str(any(p.startswith("mcp__budget_home") for p in pins))')"
[ "$state" = "budget budget-main-ro my-budget searxng|False" ] || fail "a removed row's entries and pins go ($state)"

irun "" 0 ./compose/actual/budget.env
state="$(q "$cfg" '" ".join(sorted(s))')"
[ "$state" = "budget searxng" ] || fail "flag off drops every entry on the row URLs, the legacy /mcp left to the add-on path ($state)"

seed "$api_cfg"
printf '  budget-main-ro:\n    url: http://actual-mcp-ro:3000/main/mcp\n' >> "$api_cfg"
irun api-server 1 ./compose/actual/no-budget.env
state="$(q "$api_cfg" '" ".join(sorted(s))')"
[ "$state" = "searxng" ] || fail "api-server keeps no row entry and drops the legacy budget with no-budget.env ($state)"
if grep -qi 'sync-main\|sync-home' "${work}/out"; then fail "the log names a sync id"; fi
echo "ok: image ($image)"

printf 'OK Actual budget rows\n'
