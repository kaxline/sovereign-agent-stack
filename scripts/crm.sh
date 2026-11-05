#!/usr/bin/env bash
# Twenty CRM (crm profile or a remote instance): workspaces Hermes can use.
# See docs/crm.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

ENV_PATH="${ENV_FILE:-.env}"
WS_DIR="${CRM_WORKSPACES_DIR:-${ROOT}/compose/twenty/workspaces}"
LOCAL_URL="http://twenty-server:3000"

usage() {
  cat <<'EOF'
Usage: scripts/crm.sh <command> [options]

  init [--slug S] [--display-name N] [--rotate]
        Local instance: owner account, workspace S (default main), the two
        Hermes roles and keys, written to compose/twenty/workspaces/S.env.
        Re-running for an existing slug does nothing unless --rotate is given.
  add --slug S --url URL [--force]
        A workspace on a remote Twenty. Keys come from TWENTY_KEY_RW and
        TWENTY_KEY_RO, or are asked for (hidden) when run in a terminal.
  open [--slug S]   Open a workspace in the browser
  mode 0|1          Single- (0) or multi-workspace (1) local instance
  ensure            Create data dirs and sync flags (run by make ensure-local)

After init, add or mode: make hermes-reload
EOF
}

log() { printf '[crm] %s\n' "$1"; }
die() { printf '[crm] ERROR: %s\n' "$1" >&2; exit 1; }

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# A regex, not a glob: bash 3.2 bracket ranges follow the locale, where [a-z]
# also matches uppercase letters.
valid_slug() {
  [[ "$1" =~ ^[a-z0-9-]+$ ]]
}

# One value from a workspace file, without sourcing it.
ws_get() {
  local v
  v="$(grep -E "^${2}=" "$1" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"
  printf '%s' "$v"
}

# Slugs of workspace files that point at the local instance.
local_slugs() {
  local f url
  for f in "$WS_DIR"/*.env; do
    [[ -f "$f" ]] || continue
    url="$(ws_get "$f" TWENTY_URL)"
    url="${url%/}"
    if [[ "$url" == "$LOCAL_URL" || "$url" == "${LOCAL_URL}/mcp" ]]; then
      basename "$f" .env
    fi
  done
}

data_dir() {
  resolve_data_root_path "$(env_get TWENTY_DATA_DIR ./data/twenty)"
}

sync_bool() {
  if is_true "$(env_get TWENTY_MULTIWORKSPACE 0)"; then
    upsert_env "$ENV_PATH" TWENTY_MULTIWORKSPACE_BOOL true
  else
    upsert_env "$ENV_PATH" TWENTY_MULTIWORKSPACE_BOOL false
  fi
}

server_running() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose ps --status running -q twenty-server 2>/dev/null | grep -q .
}

require_running() {
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
  server_running \
    || die "twenty-server is not running — ./scripts/setup.sh --crm (or TWENTY_LOCAL_ENABLED=1), then make up"
  local i
  for i in $(seq 1 60); do
    if [[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q twenty-server)" 2>/dev/null)" == healthy ]]; then
      return 0
    fi
    [[ "$i" -eq 1 ]] && log "Waiting for twenty-server to finish starting (first start runs migrations)"
    sleep 5
  done
  die "twenty-server did not become healthy — docker compose logs twenty-server"
}

twenty_psql() {
  docker compose exec -T twenty-db psql -X -q -At -v ON_ERROR_STOP=1 -U twenty -d twenty "$@"
}

# Twenty limits creating workspaces to server admins, and an API signup isn't
# one. Called by twenty_admin.py right after it creates the owner.
cmd_grant_admin() {
  [[ -n "${1:-}" ]] || die "usage: crm.sh grant-admin EMAIL"
  twenty_psql -v email="$1" <<'SQL'
UPDATE core."user" SET "canAccessFullAdminPanel" = true, "canImpersonate" = true WHERE email = :'email';
SQL
}

cmd_init() {
  local slug="main" display="" rotate=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --display-name) display="${2:-}"; shift 2 ;;
      --rotate) rotate="--rotate"; shift ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  valid_slug "$slug" || die "slug '${slug}' must use lowercase letters, digits and hyphens"
  require_python3
  require_running

  local username password password_file
  username="$(env_get TWENTY_USERNAME)"
  password="$(env_get TWENTY_PASSWORD)"
  password_file="$(env_get TWENTY_PASSWORD_FILE)"
  if [[ -z "$username" ]]; then
    username="owner@crm.localhost"
    upsert_env "$ENV_PATH" TWENTY_USERNAME "$username"
  fi
  if [[ -n "$password" && -n "$password_file" ]]; then
    die "set TWENTY_PASSWORD or TWENTY_PASSWORD_FILE in .env, not both"
  fi
  # With TWENTY_PASSWORD_FILE, twenty_admin.py reads or generates the file and
  # the password never goes into .env.
  if [[ -z "$password" && -z "$password_file" ]]; then
    # Saved before any call, so a failed run signs in next time instead of
    # creating a second account. Twenty accepts 8 to 50 characters.
    password="$(openssl rand -hex 16)Aa1!"
    upsert_env "$ENV_PATH" TWENTY_PASSWORD "$password"
    log "Generated the owner password (TWENTY_PASSWORD in .env)"
  fi

  local args=(init --slug "$slug" --display-name "${display:-$slug}")
  [[ -n "$rotate" ]] && args+=("$rotate")
  TWENTY_BASE_URL="http://127.0.0.1:$(env_get TWENTY_PORT 3000)" \
  TWENTY_USERNAME="$username" \
  TWENTY_PASSWORD="$password" \
  TWENTY_PASSWORD_FILE="$password_file" \
  TWENTY_MULTIWORKSPACE="$(env_get TWENTY_MULTIWORKSPACE 0)" \
  TWENTY_WORKSPACES_DIR="$WS_DIR" \
  TWENTY_DATA_DIR="$(data_dir)" \
  TWENTY_GRANT_ADMIN_CMD="${ROOT}/scripts/crm.sh" \
    python3 -I "${ROOT}/scripts/twenty_admin.py" "${args[@]}"

  if ! is_true "$(env_get TWENTY_MCP_ENABLED 0)"; then
    upsert_env "$ENV_PATH" TWENTY_MCP_ENABLED 1
    log "Set TWENTY_MCP_ENABLED=1"
  fi
  log "Next: make hermes-reload, then ask Hermes about your CRM. make crm-open opens it."
}

cmd_add() {
  local slug="" url="" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --url) url="${2:-}"; shift 2 ;;
      --force) force=1; shift ;;
      *) die "unknown option for add: $1" ;;
    esac
  done
  valid_slug "$slug" || die "--slug must use lowercase letters, digits and hyphens"
  case "$url" in
    http://* | https://*) ;;
    *) die "--url must start with http:// or https://" ;;
  esac
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  url="${url%/mcp}"
  local file="${WS_DIR}/${slug}.env"
  if [[ -e "$file" && -z "$force" ]]; then
    die "${file#"${ROOT}"/} exists; pass --force to replace it"
  fi

  local rw="${TWENTY_KEY_RW:-}" ro="${TWENTY_KEY_RO:-}"
  if [[ -z "$rw" ]]; then
    [[ -t 0 ]] || die "set TWENTY_KEY_RW (no terminal to ask in)"
    printf 'Read/write API key for %s: ' "$slug" >&2
    IFS= read -rs rw
    printf '\n' >&2
    [[ -n "$rw" ]] || die "a read/write key is required"
  fi
  if [[ -z "${TWENTY_KEY_RO+x}" && -t 0 ]]; then
    printf 'Read-only API key (Enter to keep this CRM off unattended runs): ' >&2
    IFS= read -rs ro
    printf '\n' >&2
  fi

  mkdir -p "$WS_DIR"
  local tmp
  tmp="$(umask 077 && mktemp "${WS_DIR}/.${slug}.XXXXXX")"
  {
    printf '# Written by make crm-add. Hermes registers it as mcp_servers.crm-%s. See docs/crm.md.\n' "$slug"
    printf 'TWENTY_URL=%s\n' "$url"
    printf 'TWENTY_WORKSPACE_URL=%s/\n' "$url"
    printf 'TWENTY_KEY_RW=%s\n' "$rw"
    printf 'TWENTY_KEY_RO=%s\n' "$ro"
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
  log "Wrote ${file#"${ROOT}"/} (crm-${slug})"
  if ! is_true "$(env_get TWENTY_MCP_ENABLED 0)"; then
    upsert_env "$ENV_PATH" TWENTY_MCP_ENABLED 1
    log "Set TWENTY_MCP_ENABLED=1"
  fi
  log "Next: make hermes-reload"
}

cmd_open() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for open: $1" ;;
    esac
  done
  if [[ -z "$slug" ]]; then
    if [[ -f "${WS_DIR}/main.env" ]]; then
      slug=main
    else
      local all
      all="$(local_slugs)"
      [[ "$(printf '%s' "$all" | grep -c .)" -eq 1 ]] || die "several workspaces; pass SLUG=name"
      slug="$all"
    fi
  fi
  local file="${WS_DIR}/${slug}.env" url
  [[ -f "$file" ]] || die "no workspace '${slug}' — make crm-init or make crm-add"
  url="$(ws_get "$file" TWENTY_WORKSPACE_URL)"
  [[ -n "$url" ]] || url="http://localhost:$(env_get TWENTY_PORT 3000)/"
  if [[ "$(ws_get "$file" TWENTY_URL)" == "$LOCAL_URL"* ]]; then
    local where="TWENTY_PASSWORD in .env"
    [[ -n "$(env_get TWENTY_PASSWORD_FILE)" ]] && where="in $(env_get TWENTY_PASSWORD_FILE)"
    log "Sign in as $(env_get TWENTY_USERNAME owner@crm.localhost); the password is ${where}"
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
  else
    printf '%s\n' "$url"
  fi
}

# After a mode change, point each local workspace file's browser URL at the
# right host: <subdomain>.localhost in multi-workspace mode.
update_workspace_urls() {
  local port multi slug file id sub url
  port="$(env_get TWENTY_PORT 3000)"
  multi="$1"
  for slug in $(local_slugs); do
    file="${WS_DIR}/${slug}.env"
    url="http://localhost:${port}/"
    if [[ "$multi" == 1 ]]; then
      id="$(ws_get "$file" TWENTY_WORKSPACE_ID)"
      [[ -n "$id" ]] || continue
      sub="$(printf "SELECT subdomain FROM core.workspace WHERE id = :'id';\n" | twenty_psql -v id="$id" || true)"
      [[ -n "$sub" ]] || continue
      url="http://${sub}.localhost:${port}/"
    fi
    upsert_env "$file" TWENTY_WORKSPACE_URL "$url"
    chmod 600 "$file"
    log "${slug}: ${url}"
  done
}

cmd_mode() {
  local want="${1:-}"
  case "$want" in
    0 | 1) ;;
    *) die "usage: crm.sh mode 0|1 (0 single-workspace, 1 multi-workspace)" ;;
  esac
  if [[ "$want" == 0 ]]; then
    local n
    n="$(local_slugs | grep -c . || true)"
    if [[ "$n" -gt 1 ]]; then
      die "the local instance has ${n} workspaces; single-workspace mode allows one"
    fi
  fi
  upsert_env "$ENV_PATH" TWENTY_MULTIWORKSPACE "$want"
  sync_bool
  log "Set TWENTY_MULTIWORKSPACE=${want}"
  if server_running; then
    docker compose up -d twenty-server twenty-worker
    require_running
    update_workspace_urls "$want"
  else
    log "twenty-server isn't running; the change applies on the next make up"
  fi
}

cmd_ensure() {
  mkdir -p "$WS_DIR"
  sync_bool
  is_true "$(env_get TWENTY_LOCAL_ENABLED 0)" || return 0
  local dir
  dir="$(data_dir)"
  # Created here so Docker doesn't create them root-owned on Linux.
  mkdir -p "${dir}/db" "${dir}/storage"
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add) shift; cmd_add "$@" ;;
  open) shift; cmd_open "$@" ;;
  mode) shift; cmd_mode "$@" ;;
  ensure) cmd_ensure ;;
  grant-admin) shift; cmd_grant_admin "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
