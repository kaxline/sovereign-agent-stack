# make doctor's Actual Budget block. Sourced by scripts/doctor.sh, which
# provides ok, warn, bad, env_get and has_profile and runs from the repo root;
# scripts/test-actual-doctor.sh runs it against stubs.
# shellcheck shell=bash

actual_is_true() {
  case "$1" in 1 | true | TRUE | yes | YES) return 0 ;; *) return 1 ;; esac
}

# Health of a compose service: healthy, starting, a state, or empty when it
# isn't running.
actual_service_state() {
  local cid
  cid="$(docker compose ps -q "$1" 2>/dev/null || true)"
  [[ -n "$cid" ]] || return 0
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true
}

# The write tools named in the add-on manifest, one per line. sed, not jq or
# python3: the host may have neither.
actual_write_tools() {
  sed -n '/"writeTools"[[:space:]]*:/,/\]/p' "${ROOT}/addons/actual/addon.json" \
    | grep -o '"[A-Za-z0-9_-]*"' | tr -d '"' | grep -vx writeTools || true
}

# An MCP tools/list from inside the hermes container (it shares the budget
# network with the MCP servers, which publish no port). Prints the answer.
actual_tools_list() {
  docker compose exec -T hermes sh -s "$1" 2>/dev/null <<'SH'
u="$1"
h="$(mktemp)"
accept='Accept: application/json, text/event-stream'
curl -s -m 15 -D "$h" -o /dev/null -H 'Content-Type: application/json' -H "$accept" -X POST "$u" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doctor","version":"0"}}}' \
  || { rm -f "$h"; exit 7; }
sid="$(grep -i '^mcp-session-id:' "$h" | head -1 | cut -d: -f2- | tr -d ' \r')"
rm -f "$h"
if [ -n "$sid" ]; then
  set -- -H "mcp-session-id: $sid"
else
  set --
fi
curl -s -m 15 -o /dev/null -H 'Content-Type: application/json' -H "$accept" "$@" -X POST "$u" \
  --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -s -m 15 -H 'Content-Type: application/json' -H "$accept" "$@" -X POST "$u" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
SH
}

# Whether any connected budget (<data root>/addons/actual/instances/*.json)
# exists. The data root is resolved as budget.sh does, BUDGET_DATA_ROOT first.
actual_has_rows() {
  (
    # shellcheck source=data-root.sh
    source "${ROOT}/scripts/lib/data-root.sh"
    data="$(resolve_data_root_path "${BUDGET_DATA_ROOT:-}")"
    validate_data_root_path "$data" 2>/dev/null || exit 1
    for f in "${data}/addons/actual/instances/"*.json; do
      [[ -e "$f" ]] && exit 0
    done
    exit 1
  )
}

doctor_actual() {
  actual_is_true "$(env_get ACTUAL_MCP_ENABLED 0)" || return 0
  local local_on=0
  actual_is_true "$(env_get ACTUAL_LOCAL_ENABLED 0)" && local_on=1
  echo
  echo "--- Budget (Actual) ---"

  if has_profile budget; then
    ok "COMPOSE_PROFILES includes budget"
  else
    bad "ACTUAL_MCP_ENABLED=1 but 'budget' not in COMPOSE_PROFILES — run make ensure-local"
  fi
  if [[ "$local_on" -eq 1 ]]; then
    if has_profile budget-local; then
      ok "COMPOSE_PROFILES includes budget-local"
    else
      bad "ACTUAL_LOCAL_ENABLED=1 but 'budget-local' not in COMPOSE_PROFILES — run make ensure-local"
    fi
  fi
  if [[ "$(uname -s)" == Linux && "$(id -u)" != 1000 ]] && actual_has_rows; then
    bad "on Linux the MCP containers read the connected budgets' secret files as uid 1000 (the image's node user), and this host runs as uid $(id -u); at mode 600 they can't open them"
  fi

  docker info >/dev/null 2>&1 || return 0
  actual_services
  actual_budget_rows
}

# The containers' health.
actual_services() {
  local svc state services="actual-mcp-rw actual-mcp-ro"
  [[ "$local_on" -eq 1 ]] && services="actual-server ${services}"
  for svc in $services; do
    state="$(actual_service_state "$svc")"
    case "$state" in
      healthy) ok "${svc} is healthy" ;;
      starting) warn "${svc} is starting" ;;
      "") warn "${svc} not running — make up" ;;
      *) bad "${svc} is ${state} — docker compose logs ${svc}" ;;
    esac
  done
}

# The connected budgets (addons/actual/instances/*.json): one line each from
# budget.sh status, which re-checks the stored credential, then, for each
# budget in state ok, tools/list on its /<slug>/mcp path: actual-mcp-ro must
# list no write tool, and actual-mcp-rw must answer for a budget with access
# write. Its error lines (no data root, say) print nothing; no rows is the
# normal case.
actual_budget_rows() {
  local lines line slug state access good=""
  lines="$("${ROOT}/scripts/budget.sh" status --json 2>/dev/null || true)"
  while IFS= read -r line; do
    case "$line" in
      '{"phase":"budget"'*) ;;
      *) continue ;;
    esac
    slug="$(printf '%s' "$line" | sed -n 's/.*"slug":"\([^"]*\)".*/\1/p')"
    state="$(printf '%s' "$line" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')"
    access="$(printf '%s' "$line" | sed -n 's/.*"access":"\([^"]*\)".*/\1/p')"
    case "$state" in
      ok)
        ok "budget-${slug}: ok"
        good="${good} ${slug}:${access}" ;;
      timeout | server_unreachable)
        warn "budget-${slug}: ${state} - check that its Actual server is running and reachable" ;;
      server_off)
        warn "budget-${slug}: server_off - the local Actual server is off; turn it back on with: echo '{\"local\": true}' | scripts/addon.sh configure actual, then scripts/addon.sh enable actual (add --reinit if the add-on was first enabled with the local server off)" ;;
      *)
        warn "budget-${slug}: ${state} - reconnect it: make budget-disconnect SLUG=${slug}, then make budget-connect, or in Boundary" ;;
    esac
  done <<EOF_ROWS
${lines}
EOF_ROWS

  [[ -n "$good" ]] || return 0
  if [[ -z "$(docker compose ps -q hermes 2>/dev/null || true)" ]]; then
    warn "hermes not running — the MCP tools/list checks need it"
    return 0
  fi
  local item writes
  writes="$(actual_write_tools)"
  for item in $good; do
    slug="${item%%:*}"
    access="${item#*:}"
    actual_row_tools actual-mcp-ro "$slug" "$writes"
    if [[ "$access" == write ]]; then
      actual_row_tools actual-mcp-rw "$slug" "$writes"
    fi
  done
}

# actual_row_tools SERVICE SLUG WRITES: tools/list on SERVICE's /SLUG/mcp.
# The read-only service must list none of WRITES.
actual_row_tools() {
  local svc="$1" slug="$2" writes="$3" resp names n w found=""
  [[ -n "$(docker compose ps -q "$svc" 2>/dev/null || true)" ]] || return 0
  resp="$(actual_tools_list "http://${svc}:3000/${slug}/mcp" || true)"
  names="$(printf '%s' "$resp" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | sort -u || true)"
  n="$(printf '%s' "$names" | grep -c . || true)"
  if [[ "$n" -eq 0 ]]; then
    bad "budget-${slug} on ${svc} gave no tools on tools/list — docker compose logs ${svc}"
    return 0
  fi
  if [[ "$svc" == actual-mcp-rw ]]; then
    ok "budget-${slug} on actual-mcp-rw answers tools/list (${n} tools)"
    return 0
  fi
  for w in $writes; do
    printf '%s\n' "$names" | grep -qxF -- "$w" && found="${found} ${w}"
  done
  if [[ -n "$found" ]]; then
    bad "budget-${slug} on actual-mcp-ro lists write tools:${found} — it must run without --enable-write"
  else
    ok "budget-${slug} on actual-mcp-ro lists ${n} tools and none of the $(printf '%s\n' "$writes" | grep -c .) write tools"
  fi
}
