# make doctor's Actual Budget block. Sourced by scripts/doctor.sh, which
# provides ok, warn, bad, env_get and has_profile and runs from the repo root;
# scripts/test-actual-doctor.sh runs it against stubs.
# shellcheck shell=bash

actual_is_true() {
  case "$1" in 1 | true | TRUE | yes | YES) return 0 ;; *) return 1 ;; esac
}

# One value from the budget file, without sourcing it; strips one pair of
# matching quotes, as dotenv does.
actual_file_get() {
  local v
  v="$(grep -E "^${2}=" "$1" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [[ ${#v} -ge 2 ]]; then
    case "${v:0:1}" in
      \' | \" | \`) [[ "${v: -1}" == "${v:0:1}" ]] && { v="${v#?}"; v="${v%?}"; } ;;
    esac
  fi
  printf '%s' "$v"
}

# A URL for messages: any user:password@ in it is masked.
actual_safe_url() {
  printf '%s' "$1" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@#\1***@#'
}

actual_file_mode() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || echo unknown
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

# The connected budgets (addons/actual/instances/*.json): one line each from
# budget.sh status, which re-checks the stored credential. Its error lines
# (no data root, say) print nothing; no rows is the normal case.
actual_budget_rows() {
  if [[ "$(uname -s)" == Linux && "$(id -u)" != 1000 ]] && actual_has_rows; then
    bad "on Linux the MCP containers read the connected budgets' secret files as uid 1000 (the image's node user), and this host runs as uid $(id -u); at mode 600 they can't open them"
  fi
  docker info >/dev/null 2>&1 || return 0
  local lines line slug state
  lines="$("${ROOT}/scripts/budget.sh" status --json 2>/dev/null || true)"
  while IFS= read -r line; do
    case "$line" in
      '{"phase":"budget"'*) ;;
      *) continue ;;
    esac
    slug="$(printf '%s' "$line" | sed -n 's/.*"slug":"\([^"]*\)".*/\1/p')"
    state="$(printf '%s' "$line" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')"
    if [[ "$state" == ok ]]; then
      ok "budget-${slug}: ok"
    else
      case "$state" in
        timeout | server_unreachable)
          warn "budget-${slug}: ${state} - check that its Actual server is running and reachable" ;;
        server_off)
          warn "budget-${slug}: server_off - the local Actual server is off; turn it back on with: echo '{\"local\": true}' | scripts/addon.sh configure actual, then scripts/addon.sh enable actual (add --reinit if the add-on was first enabled with the local server off)" ;;
        *)
          warn "budget-${slug}: ${state} - reconnect it: make budget-disconnect SLUG=${slug}, then make budget-connect, or in Boundary" ;;
      esac
    fi
  done <<EOF_ROWS
${lines}
EOF_ROWS
}

doctor_actual() {
  actual_is_true "$(env_get ACTUAL_MCP_ENABLED 0)" || return 0
  actual_single_budget
  actual_budget_rows
}

actual_single_budget() {
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

  local access
  access="$(env_get ACTUAL_ACCESS)"
  case "$access" in
    "") ok "ACTUAL_ACCESS is empty (chats may make changes)" ;;
    read | write) ok "ACTUAL_ACCESS=${access}" ;;
    *) bad "ACTUAL_ACCESS=${access} in .env; use read, write or leave it empty (Hermes registers no budget server until then)" ;;
  esac

  # The file: there, mode 600, readable by the containers, with the keys set.
  # An install with connected budgets and no single budget skips this part.
  # actual_budget_file and actual_services_and_tools read and set these.
  local ref missing=1 url="" actual_file="" actual_ref=""
  ref="$(env_get ACTUAL_BUDGET_FILE)"
  if [[ -z "$ref" || "$ref" == *no-budget.env ]] && actual_has_rows; then
    ok "no single budget is configured (ACTUAL_BUDGET_FILE is ${ref:-empty}); the connected budgets are listed below"
  else
    actual_budget_file "$ref" || return 0
  fi

  actual_services_and_tools
}

# actual_budget_file REF: the single budget's file checks. Sets missing (0
# when the keys are all set) and url. Returns 1 when the file is missing, and
# the rest of the block is skipped.
actual_budget_file() {
  local ref="$1" file
  if [[ -z "$ref" ]]; then
    bad "ACTUAL_BUDGET_FILE is unset, so the MCP servers get compose/actual/no-budget.env — run make budget-init or make budget-add"
    ref="./compose/actual/budget.env"
  fi
  file="$ref"
  [[ "$file" == /* ]] || file="${PWD}/${file#./}"
  if [[ ! -f "$file" ]]; then
    bad "${ref#./} is missing — run make budget-init or make budget-add"
    return 1
  fi
  local mode
  mode="$(actual_file_mode "$file")"
  if [[ "$mode" == 600 ]]; then
    ok "${ref#./} exists, mode 600"
  else
    bad "${ref#./} is mode ${mode}; it holds passwords — chmod 600 ${ref#./}"
  fi
  if [[ "$(uname -s)" == Linux && "$(id -u)" != 1000 ]]; then
    bad "on Linux the MCP containers read ${ref#./} as uid 1000 (the image's node user), and this host runs as uid $(id -u); at mode 600 they can't open it"
  fi
  local key
  missing=0
  for key in ACTUAL_SERVER_URL ACTUAL_PASSWORD ACTUAL_BUDGET_SYNC_ID; do
    if [[ -z "$(actual_file_get "$file" "$key")" ]]; then
      bad "${ref#./} has no ${key} — make budget-init or make budget-add FORCE=1"
      missing=1
    fi
  done

  url="$(actual_file_get "$file" ACTUAL_SERVER_URL)"
  if [[ "${url%/}" == http://actual-server:5006 ]]; then
    if [[ "$local_on" -eq 0 ]]; then
      warn "${ref#./} points at the local actual-server but ACTUAL_LOCAL_ENABLED=0 — set it to 1, or make budget-add for a remote server"
    fi
  elif [[ -n "$url" ]]; then
    ok "remote server: $(actual_safe_url "$url")"
  fi
  actual_file="$file"
  actual_ref="$ref"
  return 0
}

# The containers' health, the single budget's credential check (when its file
# has every key) and each instance's tools/list.
actual_services_and_tools() {
  docker info >/dev/null 2>&1 || return 0

  local svc state
  local services="actual-mcp-rw actual-mcp-ro"
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

  # The credentials: budget.sh check opens the budget with actual-admin.mjs
  # in the MCP image. Its output names no password.
  if [[ "$missing" -eq 0 ]]; then
    local sync rc=0 err file="$actual_file" ref="$actual_ref"
    sync="$(actual_file_get "$file" ACTUAL_BUDGET_SYNC_ID)"
    err="$(BUDGET_ENV_PATH="$file" "${ROOT}/scripts/budget.sh" check 2>&1 >/dev/null)" || rc=$?
    case "$rc" in
      0) ok "budget ${sync} opens with the credentials in ${ref#./}" ;;
      2) bad "budget ${sync} doesn't open with ACTUAL_BUDGET_ENCRYPTION_PASSWORD (wrong, missing, or encryption was turned on, which also changes the sync id) — make budget-add FORCE=1" ;;
      3) bad "the server password changed; re-run make budget-add FORCE=1 (remote), or make budget-remove and then make budget-init, which asks for it (local; export ACTUAL_BUDGET_ENCRYPTION_PASSWORD first if the budget is encrypted)" ;;
      4) warn "no answer from $(actual_safe_url "$url") within ${ACTUAL_ADMIN_TIMEOUT_S:-45} s; the credential check was skipped (ACTUAL_ADMIN_TIMEOUT_S sets the limit)" ;;
      *) warn "couldn't open budget ${sync}: $(printf '%s' "$err" | grep -v '^[[:space:]]*$' | tail -1)" ;;
    esac
  fi

  # Each instance answers tools/list, and the read-only one has no write tool.
  if [[ -z "$(docker compose ps -q hermes 2>/dev/null || true)" ]]; then
    warn "hermes not running — the MCP tools/list checks need it"
    return 0
  fi
  local resp names n writes w found
  writes="$(actual_write_tools)"
  for svc in actual-mcp-rw actual-mcp-ro; do
    [[ -n "$(docker compose ps -q "$svc" 2>/dev/null || true)" ]] || continue
    resp="$(actual_tools_list "http://${svc}:3000/mcp" || true)"
    names="$(printf '%s' "$resp" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | sort -u || true)"
    n="$(printf '%s' "$names" | grep -c . || true)"
    if [[ "$n" -eq 0 ]]; then
      bad "${svc} gave no tools on tools/list — docker compose logs ${svc}"
      continue
    fi
    if [[ "$svc" == actual-mcp-rw ]]; then
      ok "actual-mcp-rw answers tools/list (${n} tools)"
      continue
    fi
    found=""
    for w in $writes; do
      printf '%s\n' "$names" | grep -qxF -- "$w" && found="${found} ${w}"
    done
    if [[ -n "$found" ]]; then
      bad "actual-mcp-ro lists write tools:${found} — it must run without --enable-write"
    else
      ok "actual-mcp-ro lists ${n} tools and none of the $(printf '%s\n' "$writes" | grep -c .) write tools"
    fi
  done
}
