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
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

SERVICE_FILE="${ROOT}/compose/postgres/services/vikunja.env"
# Tokens are found again by title, so a re-run replaces them instead of piling up.
TOKEN_TITLE_RW="assistant-hermes-rw"
TOKEN_TITLE_RO="assistant-hermes-ro"

usage() {
  cat <<'EOF'
Usage: ./scripts/todo.sh <command>

Commands:
  prepare   Register Vikunja's schema in the shared Postgres
            (compose/postgres/services/vikunja.env) and create the files
            directory. setup.sh --todo runs this.
  ensure    prepare, but only when todo is in COMPOSE_PROFILES and quietly.
            make ensure-local (and so make up) runs this.
  init      With the stack up: create the Vikunja account if it does not
            exist, then mint the MCP tokens Hermes uses (read/write for the
            dashboard and WebUI, read-only for the API profile) and write
            them to .env. Re-running replaces both tokens.

init reads VIKUNJA_USERNAME / VIKUNJA_PASSWORD / VIKUNJA_EMAIL from the
environment when set, and prompts for anything missing.

Examples:
  make todo-init
EOF
}

log() { printf '[todo] %s\n' "$1"; }
die() { printf '[todo] ERROR: %s\n' "$1" >&2; exit 1; }

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

prompt_value() {
  local label="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "${label} [${default}]: " reply
    printf '%s\n' "${reply:-$default}"
  else
    read -r -p "${label}: " reply
    printf '%s\n' "$reply"
  fi
}

prompt_secret() {
  local label="$1" reply
  read -r -s -p "${label}: " reply
  printf '\n' >&2
  printf '%s\n' "$reply"
}

user_exists() {
  # The CLI prints a table; match the username column exactly.
  vikunja_cli user list 2>/dev/null | awk -F'│' -v u="$1" '{gsub(/ /, "", $3); if ($3 == u) found=1} END {exit !found}'
}

cmd_init() {
  require_python3
  require_running
  local base
  base="http://127.0.0.1:$(env_get VIKUNJA_PORT 3456)"

  local username="${VIKUNJA_USERNAME:-}" password="${VIKUNJA_PASSWORD:-}" email="${VIKUNJA_EMAIL:-}"
  [[ -n "$username" ]] || username="$(prompt_value "Vikunja username" "${USER:-me}")"
  [[ "$username" =~ ^[A-Za-z0-9._-]+$ ]] || die "username may use letters, digits, '.', '_' and '-'"

  if user_exists "$username"; then
    log "Using existing Vikunja account '${username}'"
    [[ -n "$password" ]] || password="$(prompt_secret "Password for ${username}")"
  else
    log "Creating Vikunja account '${username}'"
    [[ -n "$email" ]] || email="$(prompt_value "Email (used for login and reminders only)" "${username}@localhost.localdomain")"
    if [[ -z "$password" ]]; then
      password="$(prompt_secret "New password (8+ characters)")"
      local again
      again="$(prompt_secret "Repeat password")"
      [[ "$password" == "$again" ]] || die "passwords do not match"
    fi
    (( ${#password} >= 8 )) || die "password must be at least 8 characters"
    vikunja_cli user create -u "$username" -e "$email" -p "$password" >/dev/null \
      || die "vikunja user create failed (docker compose logs vikunja)"
  fi

  # Log in, then mint the tokens. Python keeps the password and the tokens off
  # argv and out of the shell's output.
  local out
  out="$(VK_BASE="$base" VK_USER="$username" VK_PASS="$password" \
    VK_TITLE_RW="$TOKEN_TITLE_RW" VK_TITLE_RO="$TOKEN_TITLE_RO" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["VK_BASE"]


def call(method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        sys.exit(f"{method} {path} failed: HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach Vikunja at {base}: {exc.reason}")
    return json.loads(raw) if raw else None


jwt = call("POST", "/api/v1/login", {
    "username": os.environ["VK_USER"],
    "password": os.environ["VK_PASS"],
    "long_token": False,
})["token"]

# The server's own presets: "typed" is what the Settings → MCP page offers as
# the everyday read/write set (tasks, projects, labels, comments, assignees).
typed = (call("GET", "/api/v2/mcp/info", token=jwt).get("presets") or {}).get("typed") or {}
if not typed.get("tasks"):
    sys.exit("this Vikunja has no MCP presets; it needs 2.7.0 or later")
rw = {group: list(actions) for group, actions in typed.items()}
ro = {}
for group, actions in typed.items():
    reads = [a for a in actions if a in ("read_all", "read_one")]
    if reads:
        ro[group] = reads
rw["mcp"] = ["access"]
ro["mcp"] = ["access"]

titles = {os.environ["VK_TITLE_RW"], os.environ["VK_TITLE_RO"]}
for tok in call("GET", "/api/v1/tokens", token=jwt) or []:
    if tok.get("title") in titles:
        call("DELETE", f"/api/v1/tokens/{tok['id']}", token=jwt)
        print(f"LOG Replaced token {tok['title']}")

# Vikunja requires an expiry. Far enough out that nothing silently breaks;
# rotate by re-running make todo-init.
expires = "2099-12-31T00:00:00Z"
for key, title, perms in (
    ("VIKUNJA_MCP_TOKEN_RW", os.environ["VK_TITLE_RW"], rw),
    ("VIKUNJA_MCP_TOKEN_RO", os.environ["VK_TITLE_RO"], ro),
):
    tok = call("PUT", "/api/v1/tokens", {"title": title, "expires_at": expires, "permissions": perms}, token=jwt)
    print(f"{key}={tok['token']}")
    print(f"LOG Created token {title} ({', '.join(sorted(perms))})")
PY
)" || die "token setup failed"

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
  cat <<'EOF'

Register the tokens with Hermes:
  docker compose run --rm hermes-api-bootstrap
  docker compose run --rm hermes-browser-bootstrap
  docker compose restart hermes
EOF
}

case "${1:-}" in
  prepare) cmd_prepare ;;
  ensure) cmd_ensure ;;
  init) cmd_init ;;
  -h|--help|"") usage ;;
  *) usage >&2; exit 1 ;;
esac
