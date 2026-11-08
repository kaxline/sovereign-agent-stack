#!/usr/bin/env bash
# Actual Budget (budget and budget-local profiles, or a remote server): the
# budget file the two MCP servers read. See docs/budget.md.
#
# HTTP to the local server goes through host curl; anything that needs
# @actual-app/api runs /opt/actual-admin.mjs in the actual-mcp image, so the
# host needs no node or python3.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

ENV_PATH="${ENV_FILE:-.env}"
DEFAULT_BUDGET_FILE="${ROOT}/compose/actual/budget.env"
BUDGET_FILE="${BUDGET_ENV_PATH:-$DEFAULT_BUDGET_FILE}"
BUDGET_REL="${BUDGET_FILE#"${ROOT}"/}"
# A password generated for a new server is kept here until budget.env is
# written, so a failed init can't lose it.
PENDING_FILE="$(dirname "$BUDGET_FILE")/server-password.pending"
LOCAL_URL="http://actual-server:5006"

usage() {
  cat <<'EOF'
Usage: scripts/budget.sh <command> [options]

  init [--rotate]
        Local server (ACTUAL_LOCAL_ENABLED=1, after make up): set a generated
        server password on a new server, create a budget or adopt the only
        one, check that it opens, and write compose/actual/budget.env.
        ACTUAL_BUDGET_SYNC_ID picks one of several budgets; ACTUAL_PASSWORD
        is the password of a server that already has one. Does nothing when
        budget.env exists, unless --rotate is given: then it sets a new
        generated server password on the server and in the file.
  add --url URL [--force]
        A budget on a remote server. ACTUAL_PASSWORD, ACTUAL_BUDGET_SYNC_ID
        and ACTUAL_BUDGET_ENCRYPTION_PASSWORD come from the environment, or
        are asked for in a terminal (passwords hidden). With no sync id the
        server's only budget is used. Nothing is written unless the budget
        opens with them.
  check   Open the budget in budget.env with actual-admin.mjs. Exit 0 when it
          opens, 2 for a wrong or missing encryption password, 3 when the
          server refuses the password, 1 otherwise. make doctor runs this.
  open    Open the web app and copy the server password to the clipboard
  remove  Delete budget.env and set ACTUAL_MCP_ENABLED=0. Server data stays.
  access read|write
        What chats may do with the budget: look things up (read) or make
        changes (write). Sets ACTUAL_ACCESS in .env.

After init, add, remove or access: make up, then make hermes-reload
EOF
}

log() { printf '[budget] %s\n' "$1"; }
die() { printf '[budget] ERROR: %s\n' "$1" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The MCP containers run as uid 1000 and read budget.env, mode 600, through a
# bind mount. On Linux that only works when the file's owner is uid 1000.
# Docker Desktop on macOS maps ownership, so it isn't affected.
require_uid() {
  [[ "$(uname -s)" == Linux ]] || return 0
  local uid
  uid="$(id -u)"
  [[ "$uid" == 1000 ]] && return 0
  die "on Linux the MCP containers read budget.env as uid 1000 (the image's node user), and the file would be mode 600 owned by uid ${uid}, so they couldn't open it. Run this as the uid 1000 user; nothing written"
}

require_docker() {
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
}

# One value from budget.env, without sourcing it.
file_get() {
  local v
  v="$(grep -E "^${1}=" "$BUDGET_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  # dotenv drops one pair of matching quotes around a value, and nothing else.
  if [[ ${#v} -ge 2 ]]; then
    case "${v:0:1}" in
      \' | \" | \`)
        if [[ "${v: -1}" == "${v:0:1}" ]]; then
          v="${v#?}"
          v="${v%?}"
        fi
        ;;
    esac
  fi
  printf '%s' "$v"
}

# Prints a value encoded so dotenv 17 (the MCP server's .env loader) reads it
# back unchanged; returns 1 when no encoding does. dotenv has no escapes: it
# strips one pair of surrounding ' " or ` quotes, expands \n and \r only inside
# "...", and in a bare value cuts at # and trims the ends.
dotenv_value() {
  local v="$1" q first last
  case "$v" in
    *$'\n'* | *$'\r'*) return 1 ;;
  esac
  if [[ "$v" =~ ^[A-Za-z0-9._~:/@+=,%-]*$ ]]; then
    printf '%s' "$v"
    return 0
  fi
  for q in "'" '"' '`'; do
    [[ "$v" == *"$q"* ]] && continue
    if [[ "$q" == '"' && ( "$v" == *'\n'* || "$v" == *'\r'* ) ]]; then
      continue
    fi
    printf '%s%s%s' "$q" "$v" "$q"
    return 0
  done
  # All three quotes in it: bare, which dotenv keeps as is unless it has a #,
  # space at either end, or the same quote at both ends.
  if [[ "$v" == *'#'* || "$v" =~ ^[[:space:]] || "$v" =~ [[:space:]]$ ]]; then
    return 1
  fi
  first="${v:0:1}"
  last="${v: -1}"
  if [[ "$first" == "$last" && ( "$first" == \' || "$first" == \" || "$first" == \` ) ]]; then
    return 1
  fi
  printf '%s' "$v"
}

# Dies (in this shell) when budget.env couldn't hold a value.
require_storable() { # name value
  dotenv_value "$2" > /dev/null \
    || die "${1} can't be stored in budget.env: it has a newline, or all of ' \" and \` along with a #, a space at either end or the same quote at both ends; nothing written"
}

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

# Asks in a terminal and prints the answer; prints nothing without one.
# $1 = 1 hides the input.
ask() {
  local v=""
  [[ -t 0 ]] || return 0
  printf '%s: ' "$2" >&2
  if [[ "$1" == 1 ]]; then
    IFS= read -rs v || true
    printf '\n' >&2
  else
    IFS= read -r v || true
  fi
  printf '%s' "$v"
}

# --- Local server over HTTP (host curl) ---

base=""
token=""

# A request to the local server. A POST body comes on stdin, so passwords
# never reach curl's argv; the session token goes in a header file.
http() {
  local hdr="${WORK}/hdr" rc=0
  (umask 077 && printf 'Content-Type: application/json\n' > "$hdr")
  [[ -n "$token" ]] && printf 'X-ACTUAL-TOKEN: %s\n' "$token" >> "$hdr"
  if [[ "$1" == POST ]]; then
    curl -s -m 20 -H @"$hdr" -X POST --data-binary @- "${base}${2}" || rc=$?
  else
    curl -s -m 20 -H @"$hdr" "${base}${2}" < /dev/null || rc=$?
  fi
  rm -f "$hdr"
  return "$rc"
}

json_ok() { [[ "$1" == *'"status":"ok"'* ]]; }
json_field() { printf '%s' "$1" | sed -n "s/.*\"$2\":\"\\([^\"]*\\)\".*/\\1/p"; }

# Sets token, or returns 1 when the server refuses the password.
login() {
  local resp
  resp="$(printf '{"loginMethod":"password","password":"%s"}' "$(json_escape "$1")" | http POST /account/login)" \
    || die "no answer from actual-server on ${base}"
  token="$(json_field "$resp" token)"
  [[ -n "$token" ]] && return 0
  [[ "$(json_field "$resp" reason)" == invalid-password ]] && return 1
  die "login failed: $(json_field "$resp" reason)"
}

# "<sync id>\t<name>" per budget in a /sync/list-user-files answer, leaving
# out deleted ones. A small JSON scanner, since the host may have no jq.
parse_user_files() {
  awk '
    function flush() {
      if (val["groupId"] != "" && val["deleted"] != "1" && val["deleted"] != "true") {
        name = val["name"]; gsub(/[\t\r\n]/, " ", name)
        print val["groupId"] "\t" name
      }
      split("", val)
    }
    { s = s $0 "\n" }
    END {
      n = length(s); depth = 0; key = ""; i = 1
      while (i <= n) {
        c = substr(s, i, 1)
        if (c == "\"") {
          str = ""; j = i + 1
          while (j <= n) {
            d = substr(s, j, 1)
            if (d == "\\") {
              e = substr(s, j + 1, 1)
              if (e == "n" || e == "t" || e == "r") e = " "
              str = str e; j += 2; continue
            }
            if (d == "\"") break
            str = str d; j++
          }
          i = j + 1
          k = i
          while (substr(s, k, 1) ~ /[ \t\r\n]/) k++
          if (substr(s, k, 1) == ":") { key = str; i = k + 1; continue }
          if (depth == 3) val[key] = str
          continue
        }
        if (c == "{" || c == "[") { depth++; if (depth == 3) split("", val); i++; continue }
        if (c == "}" || c == "]") { if (depth == 3 && c == "}") flush(); depth--; i++; continue }
        if (c ~ /[ \t\r\n,:]/) { i++; continue }
        tok = ""
        while (i <= n && substr(s, i, 1) !~ /[ \t\r\n,:}\]]/) { tok = tok substr(s, i, 1); i++ }
        if (depth == 3) val[key] = tok
      }
    }'
}

# --- actual-admin.mjs in the MCP image ---

a_url=""
a_pw=""
a_sync=""
a_enc=""

# Credentials go to the container by name (-e NAME), with the values in
# docker's environment, so they stay out of the process list. It mounts
# no-budget.env, so a run never depends on (or reads) the real budget file.
admin() {
  ACTUAL_BUDGET_FILE=./compose/actual/no-budget.env \
  ACTUAL_SERVER_URL="$a_url" ACTUAL_PASSWORD="$a_pw" \
  ACTUAL_BUDGET_SYNC_ID="$a_sync" ACTUAL_BUDGET_ENCRYPTION_PASSWORD="$a_enc" \
    docker compose run --rm --no-deps -T \
      -e ACTUAL_SERVER_URL -e ACTUAL_PASSWORD -e ACTUAL_BUDGET_SYNC_ID -e ACTUAL_BUDGET_ENCRYPTION_PASSWORD \
      --entrypoint node actual-mcp-rw /opt/actual-admin.mjs "$@"
}

admin_error() {
  local msg
  msg="$(grep -v '^[[:space:]]*$' "${WORK}/admin.err" 2>/dev/null | tail -1 || true)"
  printf '%s' "${msg:-no details; docker compose logs may say more}"
}

refused_password() {
  die "the server refused the server password (ACTUAL_PASSWORD); nothing written"
}

# Dies unless the budget in a_* opens.
check_budget() {
  local rc=0
  admin check > /dev/null 2> "${WORK}/admin.err" || rc=$?
  case "$rc" in
    0) log "Budget ${a_sync} opens" ;;
    2) die "budget ${a_sync} didn't open: wrong or missing encryption password (ACTUAL_BUDGET_ENCRYPTION_PASSWORD); nothing written" ;;
    3) refused_password ;;
    *) die "couldn't open budget ${a_sync} on ${a_url}: $(admin_error); nothing written" ;;
  esac
}

# --- budget.env ---

write_budget_file() { # server-url web-url password sync-id encryption-password
  local names="ACTUAL_SERVER_URL ACTUAL_WEB_URL ACTUAL_PASSWORD ACTUAL_BUDGET_SYNC_ID ACTUAL_BUDGET_ENCRYPTION_PASSWORD"
  local body="# Written by scripts/budget.sh. The Actual MCP servers read it at /app/.env."$'\n'
  local name value encoded
  # Every value is encoded, and checked, before any file is touched.
  for name in $names; do
    value="$1"
    shift
    [[ "$name" == ACTUAL_BUDGET_ENCRYPTION_PASSWORD && -z "$value" ]] && continue
    require_storable "$name" "$value"
    encoded="$(dotenv_value "$value")" || die "couldn't encode ${name}; nothing written"
    body="${body}${name}=${encoded}"$'\n'
  done

  local dir tmpf
  dir="$(dirname "$BUDGET_FILE")"
  mkdir -p "$dir" || die "couldn't create ${dir}; nothing written"
  tmpf="$(umask 077 && mktemp "${dir}/.budget.XXXXXX")" || die "couldn't create a file in ${dir}; nothing written"
  if ! printf '%s' "$body" > "$tmpf" || ! chmod 600 "$tmpf"; then
    rm -f "$tmpf"
    die "couldn't write ${BUDGET_REL}; nothing written"
  fi
  if [[ -f "$BUDGET_FILE" ]]; then
    # In place: running containers hold the file's inode through their mount.
    if ! chmod 600 "$BUDGET_FILE" || ! cat "$tmpf" > "$BUDGET_FILE"; then
      rm -f "$tmpf"
      die "couldn't write ${BUDGET_REL}"
    fi
    rm -f "$tmpf"
  elif ! mv "$tmpf" "$BUDGET_FILE"; then
    rm -f "$tmpf"
    die "couldn't write ${BUDGET_REL}; nothing written"
  fi
  log "Wrote ${BUDGET_REL} (mode 600)"
}

budget_file_ref() {
  if [[ "$BUDGET_FILE" == "$DEFAULT_BUDGET_FILE" ]]; then
    printf './compose/actual/budget.env'
  else
    printf '%s' "$BUDGET_FILE"
  fi
}

enable_mcp() {
  upsert_env "$ENV_PATH" ACTUAL_BUDGET_FILE "$(budget_file_ref)"
  upsert_env "$ENV_PATH" ACTUAL_MCP_ENABLED 1
  log "Set ACTUAL_BUDGET_FILE=$(budget_file_ref) and ACTUAL_MCP_ENABLED=1"
}

# The MCP servers load budget.env at startup; recreate running ones.
refresh_mcps() {
  if docker compose ps -q --status running actual-mcp-rw actual-mcp-ro 2>/dev/null | grep -q .; then
    if docker compose up -d --force-recreate actual-mcp-rw actual-mcp-ro >/dev/null 2>&1; then
      log "Recreated actual-mcp-rw and actual-mcp-ro"
    else
      log "WARNING: couldn't recreate the MCP servers; run make up"
    fi
  fi
}

# --- Commands ---

cmd_init() {
  local rotate=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --rotate) rotate=1; shift ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  require_uid
  if [[ -n "$rotate" && ! -f "$BUDGET_FILE" ]]; then
    die "no ${BUDGET_REL}, so there is no password to rotate; run make budget-init first"
  fi
  if [[ -f "$BUDGET_FILE" && -z "$rotate" ]]; then
    log "${BUDGET_REL} exists; nothing to do (--rotate sets a new server password; make budget-remove starts over)"
    return 0
  fi
  base="http://127.0.0.1:$(env_get ACTUAL_PORT 5006)"
  local resp
  resp="$(http GET /account/needs-bootstrap)" || resp=""
  json_ok "$resp" \
    || die "actual-server isn't answering on ${base}; ./scripts/setup.sh --budget (or ACTUAL_LOCAL_ENABLED=1), then make up"

  if [[ -n "$rotate" ]]; then
    rotate_password
    return 0
  fi
  require_docker

  local pw=""
  if [[ "$resp" == *'"bootstrapped":false'* ]]; then
    if [[ -s "$PENDING_FILE" ]]; then
      pw="$(cat "$PENDING_FILE")"
    else
      pw="$(openssl rand -hex 24)"
      mkdir -p "$(dirname "$PENDING_FILE")"
      (umask 077 && printf '%s' "$pw" > "$PENDING_FILE")
    fi
    resp="$(printf '{"password":"%s"}' "$pw" | http POST /account/bootstrap)" || resp=""
    json_ok "$resp" || die "bootstrap failed: $(json_field "$resp" reason)"
    log "Set a generated server password on actual-server"
  else
    pw="${ACTUAL_PASSWORD:-}"
    [[ -n "$pw" || ! -s "$PENDING_FILE" ]] || pw="$(cat "$PENDING_FILE")"
    [[ -n "$pw" ]] || pw="$(ask 1 "actual-server already has a password; enter it")"
    [[ -n "$pw" ]] || die "actual-server already has a password; export ACTUAL_PASSWORD or run this in a terminal"
  fi
  require_storable ACTUAL_PASSWORD "$pw"
  login "$pw" || refused_password

  local files sync="" want="${ACTUAL_BUDGET_SYNC_ID:-}" n
  resp="$(http GET /sync/list-user-files)" || resp=""
  json_ok "$resp" || die "couldn't list the server's budgets: $(json_field "$resp" reason)"
  files="$(printf '%s' "$resp" | parse_user_files)"
  n="$(printf '%s' "$files" | grep -c . || true)"
  if [[ -n "$want" ]]; then
    printf '%s\n' "$files" | awk -F'\t' -v w="$want" '$1 == w { found = 1 } END { exit !found }' \
      || die "actual-server has no budget with sync id ${want}"
    sync="$want"
  elif [[ "$n" -eq 0 ]]; then
    log "Creating a budget named Budget"
    local rc=0 created
    a_url="$LOCAL_URL" a_pw="$pw" a_sync="" a_enc=""
    created="$(admin create Budget 2> "${WORK}/admin.err")" || rc=$?
    [[ "$rc" -eq 0 ]] || die "couldn't create a budget: $(admin_error)"
    sync="$(printf '%s\n' "$created" | grep -v '^[[:space:]]*$' | tail -1)"
    [[ -n "$sync" ]] || die "creating a budget printed no sync id"
  elif [[ "$n" -eq 1 ]]; then
    sync="$(printf '%s' "$files" | cut -f1)"
    log "Adopting the existing budget '$(printf '%s' "$files" | cut -f2-)'"
  else
    printf '%s\n' "$files" | awk -F'\t' '{ printf "  %s  %s\n", $1, $2 }' >&2
    die "actual-server has ${n} budgets; pick one with make budget-init SYNC_ID=<sync id>"
  fi

  a_url="$LOCAL_URL" a_pw="$pw" a_sync="$sync" a_enc="${ACTUAL_BUDGET_ENCRYPTION_PASSWORD:-}"
  check_budget
  write_budget_file "$LOCAL_URL" "http://localhost:$(env_get ACTUAL_PORT 5006)/" "$pw" "$sync" "$a_enc"
  rm -f "$PENDING_FILE"
  enable_mcp
  upsert_env "$ENV_PATH" ACTUAL_LOCAL_ENABLED 1
  log "Set ACTUAL_LOCAL_ENABLED=1"
  refresh_mcps
  log "Next: make up, then make hermes-reload. make budget-open opens the web app."
}

# POST /account/change-password takes the current password and a new one.
rotate_password() {
  local url old new resp
  url="$(file_get ACTUAL_SERVER_URL)"
  [[ "${url%/}" == "$LOCAL_URL" ]] \
    || die "${BUDGET_REL} points at ${url}, not the local server; change that server's password there, then make budget-add FORCE=1"
  old="$(file_get ACTUAL_PASSWORD)"
  if ! login "$old"; then
    # A rotation that changed the server but didn't get to write the file.
    [[ -s "$PENDING_FILE" ]] || refused_password
    old="$(cat "$PENDING_FILE")"
    login "$old" || refused_password
  fi
  if [[ -s "$PENDING_FILE" && "$(cat "$PENDING_FILE")" != "$old" ]]; then
    new="$(cat "$PENDING_FILE")"
  else
    new="$(openssl rand -hex 24)"
    (umask 077 && printf '%s' "$new" > "$PENDING_FILE")
  fi
  resp="$(printf '{"password":"%s","currentPassword":"%s"}' "$new" "$(json_escape "$old")" \
    | http POST /account/change-password)" || resp=""
  json_ok "$resp" || die "changing the server password failed: $(json_field "$resp" reason)"
  log "Set a new generated server password on actual-server"
  write_budget_file "$url" "$(file_get ACTUAL_WEB_URL)" "$new" "$(file_get ACTUAL_BUDGET_SYNC_ID)" \
    "$(file_get ACTUAL_BUDGET_ENCRYPTION_PASSWORD)"
  rm -f "$PENDING_FILE"
  refresh_mcps
  log "Next: make up if the MCP servers aren't running. Sign in to the web app again with make budget-open."
}

cmd_add() {
  local url="" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --url) url="${2:-}"; shift 2 ;;
      --force) force=1; shift ;;
      *) die "unknown option for add: $1" ;;
    esac
  done
  case "$url" in
    http://* | https://*) ;;
    *) die "--url must start with http:// or https://" ;;
  esac
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  require_uid
  if [[ -e "$BUDGET_FILE" && -z "$force" ]]; then
    die "${BUDGET_REL} exists; pass --force (make budget-add FORCE=1) to replace it"
  fi

  # With the values in the environment nothing is asked.
  local pw="${ACTUAL_PASSWORD:-}" sync="${ACTUAL_BUDGET_SYNC_ID:-}" enc="${ACTUAL_BUDGET_ENCRYPTION_PASSWORD:-}"
  [[ -n "$pw" ]] || pw="$(ask 1 "Server password for ${url}")"
  if [[ -z "$pw" ]]; then
    [[ -t 0 ]] || die "set ACTUAL_PASSWORD (no terminal to ask in)"
    die "a server password is required"
  fi
  if [[ -z "$sync" && -z "${ACTUAL_BUDGET_SYNC_ID+x}" ]]; then
    sync="$(ask 0 "Sync ID (Settings > Advanced settings; Enter to use the server's only budget)")"
  fi
  if [[ -z "$enc" && -z "${ACTUAL_BUDGET_ENCRYPTION_PASSWORD+x}" ]]; then
    enc="$(ask 1 "Encryption password (Enter if the budget isn't end-to-end encrypted)")"
  fi
  require_storable ACTUAL_PASSWORD "$pw"
  [[ -z "$enc" ]] || require_storable ACTUAL_BUDGET_ENCRYPTION_PASSWORD "$enc"
  require_docker

  a_url="$url" a_pw="$pw" a_sync="" a_enc=""
  if [[ -z "$sync" ]]; then
    local list rc=0 n
    list="$(admin list 2> "${WORK}/admin.err")" || rc=$?
    case "$rc" in
      0) ;;
      3) refused_password ;;
      *) die "couldn't list the budgets on ${url}: $(admin_error); nothing written" ;;
    esac
    list="$(printf '%s\n' "$list" | grep "$(printf '\t')" || true)"
    n="$(printf '%s' "$list" | grep -c . || true)"
    if [[ "$n" -eq 0 ]]; then
      die "${url} has no budgets; create one in its web app first"
    elif [[ "$n" -gt 1 ]]; then
      printf '%s\n' "$list" | awk -F'\t' '{ printf "  %s  %s\n", $1, $2 }' >&2
      die "${url} has ${n} budgets; pick one with ACTUAL_BUDGET_SYNC_ID (make budget-add SYNC_ID=<sync id>)"
    fi
    sync="$(printf '%s' "$list" | cut -f1)"
    log "Using the server's only budget, '$(printf '%s' "$list" | cut -f2-)'"
  fi

  a_sync="$sync" a_enc="$enc"
  check_budget
  write_budget_file "$url" "${url}/" "$pw" "$sync" "$enc"
  enable_mcp
  refresh_mcps
  log "Next: make up, then make hermes-reload"
}

cmd_check() {
  [[ $# -eq 0 ]] || die "check takes no options"
  [[ -f "$BUDGET_FILE" ]] || die "no ${BUDGET_REL}; make budget-init or make budget-add URL=..."
  require_docker
  a_url="$(file_get ACTUAL_SERVER_URL)"
  a_pw="$(file_get ACTUAL_PASSWORD)"
  a_sync="$(file_get ACTUAL_BUDGET_SYNC_ID)"
  a_enc="$(file_get ACTUAL_BUDGET_ENCRYPTION_PASSWORD)"
  local rc=0
  admin check > /dev/null 2> "${WORK}/admin.err" || rc=$?
  case "$rc" in
    0) log "Budget ${a_sync} opens" ;;
    2) log "Budget ${a_sync}: wrong or missing encryption password (ACTUAL_BUDGET_ENCRYPTION_PASSWORD)" >&2 ;;
    3) log "The server refused the server password (ACTUAL_PASSWORD)" >&2 ;;
    *) log "Couldn't open budget ${a_sync} on ${a_url}: $(admin_error)" >&2; rc=1 ;;
  esac
  return "$rc"
}

cmd_open() {
  [[ $# -eq 0 ]] || die "open takes no options"
  [[ -f "$BUDGET_FILE" ]] || die "no ${BUDGET_REL}; make budget-init or make budget-add URL=..."
  local url pw
  url="$(file_get ACTUAL_WEB_URL)"
  [[ -n "$url" ]] || url="http://localhost:$(env_get ACTUAL_PORT 5006)/"
  pw="$(file_get ACTUAL_PASSWORD)"
  if [[ -n "$pw" ]] && command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$pw" | pbcopy
    log "Copied the server password to the clipboard"
  else
    log "The server password is ACTUAL_PASSWORD in ${BUDGET_REL}"
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
  else
    printf '%s\n' "$url"
  fi
}

cmd_remove() {
  [[ $# -eq 0 ]] || die "remove takes no options"
  [[ -f "$BUDGET_FILE" ]] || die "no ${BUDGET_REL} to remove"
  local url
  url="$(file_get ACTUAL_SERVER_URL)"
  rm -f "$BUDGET_FILE"
  upsert_env "$ENV_PATH" ACTUAL_BUDGET_FILE ""
  upsert_env "$ENV_PATH" ACTUAL_MCP_ENABLED 0
  log "Removed ${BUDGET_REL}; cleared ACTUAL_BUDGET_FILE and set ACTUAL_MCP_ENABLED=0"
  if [[ "${url%/}" == "$LOCAL_URL" ]]; then
    log "actual-server and its budgets are untouched (ACTUAL_DATA_DIR); ACTUAL_LOCAL_ENABLED is unchanged"
  fi
  log "Next: make up, then make hermes-reload"
}

cmd_access() {
  [[ $# -eq 1 ]] || die "usage: budget.sh access read|write"
  case "$1" in
    read | write) ;;
    *) die "usage: budget.sh access read|write" ;;
  esac
  upsert_env "$ENV_PATH" ACTUAL_ACCESS "$1"
  log "Set ACTUAL_ACCESS=$1"
  log "Next: make hermes-reload"
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add) shift; cmd_add "$@" ;;
  check) shift; cmd_check "$@" ;;
  open) shift; cmd_open "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  access) shift; cmd_access "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
