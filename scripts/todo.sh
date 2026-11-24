#!/usr/bin/env bash
# Vikunja todo lists (`todo` profile): database registration, account, and the
# two MCP tokens Hermes uses. See docs/todo.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/secret.sh
source "${ROOT}/scripts/lib/secret.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

SERVICE_FILE="${ROOT}/compose/postgres/services/vikunja.env"
# Tokens are found again by title, so a re-run replaces them instead of piling up.
TOKEN_TITLE_RW="assistant-hermes-rw"
TOKEN_TITLE_RO="assistant-hermes-ro"

usage() {
  cat <<'EOF'
Usage: ./scripts/todo.sh <command> [--json]

Commands:
  prepare   Register Vikunja's schema in the shared Postgres
            (compose/postgres/services/vikunja.env) and create the files
            directory. setup.sh --todo runs this.
  ensure    prepare, but only when todo is in COMPOSE_PROFILES and quietly.
            make ensure-local (and so make up) runs this.
  init      With the stack up: create the Vikunja account (or adopt the
            only existing one) with a generated password, then mint the MCP
            tokens Hermes uses (read/write for the dashboard and WebUI,
            read-only for the API profile). Writes VIKUNJA_USERNAME,
            VIKUNJA_PASSWORD and both tokens to .env. Re-running replaces
            both tokens. Asks nothing.
  open      Open the web app, signed in (http://localhost:3456/login-local).

--json (prepare and init, as addon.sh enable runs them for the vikunja
add-on): progress goes to stderr, and init ends with one JSON done line on
stdout naming the account and the todos MCP server.

Examples:
  make todo-init
  make todo-open
EOF
}

JSON=0
log() { if [[ "$JSON" == 1 ]]; then printf '[todo] %s\n' "$1" >&2; else printf '[todo] %s\n' "$1"; fi; }
die() {
  printf '[todo] ERROR: %s\n' "$1" >&2
  if [[ "$JSON" == 1 ]]; then
    local msg="${1//\\/\\\\}"
    printf '{"phase":"error","error":"command_failed","message":"%s"}\n' "${msg//\"/\\\"}"
  fi
  exit 1
}

cmd_prepare() {
  if [[ ! -f "$SERVICE_FILE" ]]; then
    ./scripts/db.sh service-add vikunja
  fi
  # db-provision reads DB_SERVICE_PASSWORD; Vikunja reads its own variable
  # name from the same env_file, so mirror the password under that name.
  if ! grep -q '^VIKUNJA_DATABASE_PASSWORD=' "$SERVICE_FILE"; then
    local password
    password="$(grep -E '^DB_SERVICE_PASSWORD=' "$SERVICE_FILE" | head -1 | cut -d= -f2-)"
    [[ -n "$password" ]] || die "${SERVICE_FILE#"${ROOT}"/} has no DB_SERVICE_PASSWORD"
    printf 'VIKUNJA_DATABASE_PASSWORD=%s\n' "$password" >> "$SERVICE_FILE"
    log "Added VIKUNJA_DATABASE_PASSWORD to ${SERVICE_FILE#"${ROOT}"/}"
  fi
  local files_dir
  files_dir="$(resolve_data_root_path "$(env_get VIKUNJA_FILES_DIR ./data/vikunja/files)")"
  mkdir -p "$files_dir"
  log "Vikunja attachments: ${files_dir}"
}

cmd_open() {
  local url
  url="http://localhost:$(env_get VIKUNJA_PORT 3456)/login-local"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
  else
    printf '%s\n' "$url"
  fi
}

cmd_ensure() {
  has_compose_profile todo || return 0
  cmd_prepare >/dev/null
}

require_running() {
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
  if ! docker compose ps --status running vikunja 2>/dev/null | grep -q vikunja; then
    die "vikunja is not running — add todo to COMPOSE_PROFILES (./scripts/setup.sh --todo) and make up"
  fi
}

vikunja_cli() {
  docker compose exec -T vikunja /app/vikunja/vikunja "$@"
}

# "<id> <username>" per account, from the CLI's table output.
list_users() {
  vikunja_cli user list 2>/dev/null | awk -F'│' 'NF > 3 {
    id = $2; name = $3; gsub(/ /, "", id); gsub(/ /, "", name)
    if (id ~ /^[0-9]+$/) print id, name
  }'
}

# HTTP status of a login attempt. The body goes over stdin, not argv.
login_status() {
  printf '{"username":"%s","password":"%s"}' "$2" "$3" \
    | curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      --data-binary @- "$1/api/v1/login"
}

cmd_init() {
  require_running
  local base
  base="http://127.0.0.1:$(env_get VIKUNJA_PORT 3456)"

  # The stack owns this account's password: generated, kept in .env, and used
  # by /login-local so nobody has to remember it. See docs/todo.md.
  local username password users count
  username="$(env_get VIKUNJA_USERNAME)"
  password="$(env_get VIKUNJA_PASSWORD)"
  users="$(list_users)"
  count="$(printf '%s' "$users" | grep -c . || true)"
  if [[ -z "$username" ]]; then
    if [[ "$count" -eq 1 ]]; then
      username="${users#* }"
      log "Adopting the existing Vikunja account '${username}'"
    elif [[ "$count" -eq 0 ]]; then
      username="$(id -un | tr -c 'A-Za-z0-9._-\n' '_')"
    else
      die "Vikunja has ${count} accounts; set VIKUNJA_USERNAME in .env to the one Hermes and /login-local should use"
    fi
  fi
  [[ "$username" =~ ^[A-Za-z0-9._-]+$ ]] || die "VIKUNJA_USERNAME may use letters, digits, '.', '_' and '-'"

  local user_id
  user_id="$(printf '%s\n' "$users" | awk -v u="$username" '$2 == u { print $1 }')"
  if [[ -z "$user_id" ]]; then
    [[ -n "$password" ]] || password="$(random_value hex 24)"
    log "Creating Vikunja account '${username}'"
    vikunja_cli user create -u "$username" -e "${username}@localhost.localdomain" -p "$password" >/dev/null \
      || die "vikunja user create failed (docker compose logs vikunja)"
  elif [[ -z "$password" || "$(login_status "$base" "$username" "$password")" != 200 ]]; then
    # An account made by hand (or a stale .env): give it a managed password.
    password="$(random_value hex 24)"
    log "Setting a generated password on '${username}' (kept in .env)"
    vikunja_cli user reset-password "$user_id" --direct -p "$password" >/dev/null \
      || die "vikunja user reset-password failed (docker compose logs vikunja)"
  else
    log "Using Vikunja account '${username}'"
  fi
  upsert_env .env VIKUNJA_USERNAME "$username"
  upsert_env .env VIKUNJA_PASSWORD "$password"

  # Log in, then mint the tokens. Python keeps the password and the tokens off
  # argv and out of the shell's output. It runs in the Hermes image on
  # Vikunja's network, so the host needs no python3.
  local out api="http://vikunja-proxy:8080" netopt=(--network-of vikunja-proxy)
  if [[ "${STACK_PYTHON:-}" == host ]]; then api="$base"; netopt=(); fi
  out="$(VK_BASE="$api" VK_USER="$username" VK_PASS="$password" \
    VK_TITLE_RW="$TOKEN_TITLE_RW" VK_TITLE_RO="$TOKEN_TITLE_RO" \
    stack_python ${netopt[@]+"${netopt[@]}"} --env VK_BASE --env VK_USER --env VK_PASS \
      --env VK_TITLE_RW --env VK_TITLE_RO "${ROOT}/scripts/lib/vikunja_tokens.py")" \
    || die "token setup failed"

  local line
  while IFS= read -r line; do
    case "$line" in
      LOG\ *) log "${line#LOG }" ;;
      VIKUNJA_MCP_TOKEN_RW=*|VIKUNJA_MCP_TOKEN_RO=*)
        upsert_env .env "${line%%=*}" "${line#*=}"
        ;;
    esac
  done <<< "$out"
  upsert_env .env VIKUNJA_MCP_ENABLED 1
  log "Wrote VIKUNJA_MCP_TOKEN_RW / VIKUNJA_MCP_TOKEN_RO to .env"
  # Picks up VIKUNJA_USERNAME / VIKUNJA_PASSWORD for /login-local.
  docker compose up -d vikunja-proxy >/dev/null 2>&1 \
    || log "WARNING: could not recreate vikunja-proxy; run make up"
  if [[ "$JSON" == 1 ]]; then
    # addon.sh reloads Hermes itself. The username matches [A-Za-z0-9._-].
    printf '{"phase":"done","account":"%s","mcp":["todos"]}\n' "$username"
    return 0
  fi
  cat <<EOF

Open your todo lists (signed in): make todo-open
  or http://localhost:$(env_get VIKUNJA_PORT 3456)/login-local

Register the tokens with Hermes (about two minutes):
  make hermes-reload
EOF
}

case "${2:-}" in
  --json) JSON=1 ;;
  "") ;;
  *) usage >&2; exit 1 ;;
esac
case "${1:-}" in
  prepare) cmd_prepare ;;
  open) cmd_open ;;
  ensure) cmd_ensure ;;
  init) cmd_init ;;
  -h|--help|"") usage ;;
  *) usage >&2; exit 1 ;;
esac
