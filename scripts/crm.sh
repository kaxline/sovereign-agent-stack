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
# Workspace files from before 0.3.13; migrate moves them into the store.
WS_DIR="${CRM_WORKSPACES_DIR:-${ROOT}/compose/twenty/workspaces}"
LOCAL_URL="http://twenty-server:3000"
ADDONS="${ADDONS_DIR:-${ROOT}/addons}"
# ASSISTANT_DATA_ROOT from the environment wins over the env file, as it does
# for compose.
DATA_ROOT="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-$(env_get ASSISTANT_DATA_ROOT)}")"
# The Twenty add-on's instance store (scripts/lib/addon_store.py): one
# instances/<slug>.json per workspace, keys in secrets/<slug>.key_ro|key_rw.
STORE="${DATA_ROOT}/addons/twenty"
# Set by --json: stdout (fd 3) carries only the hook's JSON lines.
JSON_MODE=""
DIE_ERROR=""

usage() {
  cat <<'EOF'
Usage: scripts/crm.sh <command> [options]

  init [--slug S] [--display-name N] [--rotate] [--json]
        Local instance: owner account, workspace S (default main), the two
        Hermes roles and keys, stored as Twenty add-on instance S.
        Re-running for an existing slug does nothing unless --rotate is given.
        --json is addon.sh's init hook: JSON lines on stdout.
  add --slug S --url URL [--force]
        A workspace on a remote Twenty. Keys come from TWENTY_KEY_RW and
        TWENTY_KEY_RO, or are asked for (hidden) when run in a terminal.
        TWENTY_ACCESS=read gives Hermes chats the read-only key. Each key is
        checked with an MCP initialize; nothing is written if one fails.
  remove --slug S   Delete a workspace and its keys from the store
                    (make hermes-reload drops it)
  access --slug S read|write
        What chats may do in a workspace: look things up (read) or make
        changes (write). crm init keeps it.
  open [--slug S]   Open a workspace in the browser, signed in when it's local
  login-link [--slug S]
        Local workspace: print a URL that signs a browser in as the owner,
        good for 15 minutes. Only the URL goes to stdout.
  mode 0|1          Single- (0) or multi-workspace (1) local instance
  ensure            Create data dirs, sync flags and migrate (run by make ensure-local)
  migrate           Move compose/twenty/workspaces/<slug>.env files into the
                    add-on instance store, renaming each to <slug>.env.migrated.
                    init, add, remove, access, login-link, open and ensure run it.
  connect --slug S --json
                    addon.sh's connect hook: checks the values on stdin (JSON)
                    and each key with an MCP initialize. Stores nothing.
  disconnect --slug S --json
                    addon.sh's disconnect hook. Stores nothing.

After init, add or mode: make hermes-reload
EOF
}

log() { printf '[crm] %s\n' "$1"; }
warn() { printf '[crm] %s\n' "$1" >&2; }

# JSON string content: quotes and backslashes become ', control characters go.
json_text() {
  printf '%s' "$1" | tr "\"\\\\" "''" | tr -d '\000-\037'
}

# --json: everything a command prints goes to stderr, and fd 3 (the real
# stdout) gets only the hook's JSON lines.
json_mode() {
  JSON_MODE=1
  exec 3>&1 1>&2
}
phase() { # phase running|done MESSAGE
  printf '{"phase":"setup","status":"%s","message":"%s"}\n' "$1" "$(json_text "$2")" >&3
}
fail_with() { # fail_with ERROR-NAME MESSAGE
  printf '[crm] ERROR: %s\n' "$2" >&2
  printf '{"phase":"error","error":"%s","message":"%s"}\n' "$1" "$(json_text "$2")" >&3
  exit 1
}

die() {
  if [[ -n "$JSON_MODE" ]]; then
    fail_with "${DIE_ERROR:-command_failed}" "$1"
  fi
  printf '[crm] ERROR: %s\n' "$1" >&2
  exit 1
}

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# A regex, not a glob: bash 3.2 bracket ranges follow the locale, where [a-z]
# also matches uppercase letters. addon_store's rule: a letter first, at most
# 31 characters.
valid_slug() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,30}$ ]]
}

# True when a value can go into the JSON crm.sh builds: no quote, backslash
# or control character. URLs, workspace ids and Twenty's keys never have one.
json_safe() {
  case "$1" in
    *\"* | *\\*) return 1 ;;
  esac
  [[ "$1" != *[[:cntrl:]]* ]]
}

# A key goes into a curl header file and a secret file as is, so it may not
# hold whitespace or a control character.
key_ok() {
  [[ "$1" != *[[:space:][:cntrl:]]* ]]
}

# True when an http(s) URL carries user:password@ before the host. Such URLs
# are refused, so no credential reaches a message or the store.
has_userinfo() {
  [[ "$1" =~ ^[A-Za-z]+://[^/?#]*@ ]]
}

# Renames a leftover pre-0.3.13 workspace file for SLUG, so migrate doesn't
# bring back a workspace that remove or add --force replaced.
retire_ws_file() {
  local f="${WS_DIR}/${1}.env"
  [[ -f "$f" ]] || return 0
  if mv "$f" "${f}.migrated"; then
    warn "renamed the old workspace file ${f} to ${f}.migrated"
  else
    warn "couldn't rename ${f}; delete it, or crm.sh migrate will warn about it"
  fi
}

# A Twenty URL as the store keeps it: no trailing slash, no /mcp.
norm_url() {
  local url="$1"
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  url="${url%/mcp}"
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  printf '%s' "$url"
}

# One value from a workspace file, without sourcing it.
ws_get() {
  local v
  v="$(grep -E "^${2}=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true)"
  v="${v%\"}"; v="${v#\"}"
  v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

# scripts/lib/addon_cli.py, called as scripts/addon.sh's cli() calls it. It
# takes no lock.
addon_cli() {
  local mounts=(--mount "$DATA_ROOT")
  path_is_inside "$ADDONS" "$ROOT" || mounts+=(--mount "$ADDONS")
  stack_python "${mounts[@]}" "${ROOT}/scripts/lib/addon_cli.py" \
    --addons-dir "$ADDONS" --data-root "$DATA_ROOT" "$@"
}

# The JSON object addon_cli configure takes for one instance, on stdout.
# Args: url access local(true|false) workspace_url workspace_id key_ro key_rw;
# empty optional values are left out. Callers check each value with json_safe.
instance_json() {
  local out
  out="{\"url\":\"$1\",\"access\":\"$2\",\"local\":$3"
  [[ -n "$4" ]] && out="${out},\"workspace_url\":\"$4\""
  [[ -n "$5" ]] && out="${out},\"workspace_id\":\"$5\""
  [[ -n "$6" ]] && out="${out},\"key_ro\":\"$6\""
  [[ -n "$7" ]] && out="${out},\"key_rw\":\"$7\""
  printf '%s}' "$out"
}

stored() {
  [[ -f "${STORE}/instances/${1}.json" ]]
}

# The stored workspaces, one line each from twenty_admin.py list: slug, local
# (1/0), access, url, workspace_url, workspace_id, has key_ro, has key_rw,
# tab-separated, "-" for empty. Loaded once per run, in the main shell, so
# command substitutions reuse it. No instance files: no container.
ROWS=""
ROWS_LOADED=""
load_rows() {
  [[ -z "$ROWS_LOADED" ]] || return 0
  local f any=""
  for f in "${STORE}/instances"/*.json; do
    [[ -f "$f" ]] && { any=1; break; }
  done
  if [[ -n "$any" ]]; then
    local mounts=(--mount "$DATA_ROOT")
    path_is_inside "$ADDONS" "$ROOT" || mounts+=(--mount "$ADDONS")
    ROWS="$(ADDONS_DATA_ROOT="$DATA_ROOT" ADDONS_DIR="$ADDONS" stack_python "${mounts[@]}" \
      --env ADDONS_DATA_ROOT --env ADDONS_DIR -I "${ROOT}/scripts/twenty_admin.py" list)" \
      || die "can't read the workspaces in ${STORE}"
  fi
  ROWS_LOADED=1
}

# row_get SLUG N: field N (1-based) of SLUG's row, empty for "-" or no row.
row_get() {
  local v
  v="$(printf '%s\n' "$ROWS" | awk -F'\t' -v s="$1" -v n="$2" '$1 == s { print $n; exit }')"
  [[ "$v" == - ]] && v=""
  printf '%s' "$v"
}

has_row() {
  printf '%s\n' "$ROWS" | awk -F'\t' -v s="$1" '$1 == s { f = 1 } END { exit !f }'
}

# Slugs of workspaces on the local instance.
local_slugs() {
  printf '%s\n' "$ROWS" | awk -F'\t' '$2 == "1" { print $1 }'
}

is_local_slug() {
  [[ "$(row_get "$1" 2)" == 1 ]]
}

# Moves each $WS_DIR/<slug>.env into the store with addon_cli configure, then
# renames it <slug>.env.migrated. A slug already in the store keeps its stored
# values and its file stays, with a warning. Prints to stderr only, never a
# key. Returns 1 when a store write failed (that file stays for the next run).
migrate_workspaces() {
  local f any=""
  for f in "$WS_DIR"/*.env; do
    [[ -f "$f" ]] && { any=1; break; }
  done
  [[ -n "$any" ]] || return 0
  local failed=0 slug url access local_ ws_url ws_id ro rw
  for f in "$WS_DIR"/*.env; do
    [[ -f "$f" ]] || continue
    slug="$(basename "$f" .env)"
    if ! valid_slug "$slug"; then
      warn "skipped ${f}: '${slug}' isn't a workspace name (lowercase letters, digits and hyphens, a letter first, at most 31); rename it and run crm.sh migrate"
      continue
    fi
    if stored "$slug"; then
      warn "${slug} is already in ${STORE}; kept the stored workspace and left ${f}. Delete the file once you've checked it."
      continue
    fi
    url="$(norm_url "$(ws_get "$f" TWENTY_URL)")"
    access="$(ws_get "$f" TWENTY_ACCESS)"
    [[ -n "$access" ]] || access="write"
    ws_url="$(ws_get "$f" TWENTY_WORKSPACE_URL)"
    ws_id="$(ws_get "$f" TWENTY_WORKSPACE_ID)"
    ro="$(ws_get "$f" TWENTY_KEY_RO)"
    rw="$(ws_get "$f" TWENTY_KEY_RW)"
    case "$url" in
      http://* | https://*) ;;
      *) warn "skipped ${f}: TWENTY_URL isn't an http(s) URL"; continue ;;
    esac
    if has_userinfo "$url"; then
      warn "skipped ${f}: TWENTY_URL holds a user name or password before the host; remove it"
      continue
    fi
    if ! { key_ok "$ro" && key_ok "$rw"; }; then
      warn "skipped ${f}: a key holds whitespace or a control character"
      continue
    fi
    case "$access" in
      read | write) ;;
      *) warn "skipped ${f}: TWENTY_ACCESS must be read or write"; continue ;;
    esac
    if ! { json_safe "$url" && json_safe "$ws_url" && json_safe "$ws_id" && json_safe "$ro" && json_safe "$rw"; }; then
      warn "skipped ${f}: a value holds a quote, backslash or control character"
      continue
    fi
    local_=false
    [[ "$url" == "$LOCAL_URL" ]] && local_=true
    if instance_json "$url" "$access" "$local_" "$ws_url" "$ws_id" "$ro" "$rw" \
      | addon_cli configure twenty --instance "$slug" > /dev/null; then
      if mv "$f" "${f}.migrated"; then
        warn "migrated ${slug} to ${STORE} (the old file is ${f}.migrated)"
      else
        warn "stored ${slug} but couldn't rename ${f}; delete it"
        failed=1
      fi
    else
      # Undo a partial write, so the next run sees no stored slug and retries.
      addon_cli remove-instance twenty "$slug" > /dev/null 2>&1 || true
      warn "couldn't store ${slug}; left ${f} for the next run"
      failed=1
    fi
  done
  return "$failed"
}

# Migration ahead of another command: a failure is reported, not fatal.
migrate_first() {
  migrate_workspaces || warn "some workspace files weren't migrated; see above"
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

# Run twenty_admin.py in the Hermes image on the crm network, so the host
# needs no python3. Reads the caller's username, password and password_file
# (bash's dynamic scope); the values reach the container as environment, not
# argv.
crm_admin() {
  local base="http://twenty-server:3000" data pf="$password_file" name
  data="$(data_dir)"
  local opts=(--network-of twenty-server --mount "$data" --mount "$STORE")
  path_is_inside "$ADDONS" "$ROOT" || opts+=(--mount "$ADDONS")
  if [[ -n "$pf" ]]; then
    [[ "$pf" == /* ]] || pf="${ROOT}/${pf}"
    opts+=(--mount "$(dirname "$pf")")
  fi
  for name in TWENTY_BASE_URL TWENTY_USERNAME TWENTY_PASSWORD TWENTY_PASSWORD_FILE \
    TWENTY_MULTIWORKSPACE ADDONS_DATA_ROOT ADDONS_DIR TWENTY_DATA_DIR; do
    opts+=(--env "$name")
  done
  [[ "${STACK_PYTHON:-}" == host ]] && base="http://127.0.0.1:$(env_get TWENTY_PORT 3000)"
  TWENTY_BASE_URL="$base" \
  TWENTY_USERNAME="$username" \
  TWENTY_PASSWORD="$password" \
  TWENTY_PASSWORD_FILE="$pf" \
  TWENTY_MULTIWORKSPACE="$(env_get TWENTY_MULTIWORKSPACE 0)" \
  ADDONS_DATA_ROOT="$DATA_ROOT" \
  ADDONS_DIR="$ADDONS" \
  TWENTY_DATA_DIR="$data" \
    stack_python "${opts[@]}" -I "${ROOT}/scripts/twenty_admin.py" "$@"
}

# Turns twenty_admin.py's "[crm] <step>: running|done ..." lines into setup
# phase lines on fd 3; every line also goes to stderr.
to_phases() {
  local line msg
  while IFS= read -r line; do
    printf '%s\n' "$line" >&2
    msg="${line#\[crm\] }"
    case "$msg" in
      *": running") phase running "$msg" ;;
      *": done" | *": done "*) phase "done" "$msg" ;;
    esac
  done
}

cmd_init() {
  local slug="main" display="" rotate="" json=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --display-name) display="${2:-}"; shift 2 ;;
      --rotate) rotate="--rotate"; shift ;;
      --json) json=1; shift ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  [[ -n "$json" ]] && json_mode
  if [[ -n "$json" ]] && ! valid_slug "$slug"; then
    fail_with bad_slug "slug '${slug}' must use lowercase letters, digits and hyphens"
  fi
  valid_slug "$slug" || die "slug '${slug}' must use lowercase letters, digits and hyphens"
  migrate_first
  if [[ -n "$json" ]] && ! is_true "$(env_get TWENTY_LOCAL_ENABLED 0)"; then
    printf '{"phase":"done","message":"local instance off"}\n' >&3
    exit 0
  fi
  DIE_ERROR=server_unreachable
  require_running
  DIE_ERROR=""

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
  # The server-admin grant runs here, between a pass that only creates or
  # signs in to the owner and the full one, because twenty_admin.py's
  # container has no docker CLI. Both passes are safe to repeat.
  if [[ -n "$json" ]]; then
    crm_admin "${args[@]}" --owner-only | to_phases || die "owner account setup failed; see the log above"
    phase running "server admin: running"
    cmd_grant_admin "$username" >/dev/null || die "the server-admin grant failed"
    phase "done" "server admin: done"
    crm_admin "${args[@]}" | to_phases || die "workspace setup failed; see the log above"
  else
    crm_admin "${args[@]}" --owner-only
    printf '[crm] server admin: running\n'
    cmd_grant_admin "$username" >/dev/null
    printf '[crm] server admin: done\n'
    crm_admin "${args[@]}"
  fi

  if ! is_true "$(env_get TWENTY_MCP_ENABLED 0)"; then
    upsert_env "$ENV_PATH" TWENTY_MCP_ENABLED 1
    log "Set TWENTY_MCP_ENABLED=1"
  fi
  log "Next: make hermes-reload, then ask Hermes about your CRM. make crm-open opens it."
  if [[ -n "$json" ]]; then
    printf '{"phase":"done","account":"%s","mcp":["crm-%s"]}\n' "$slug" "$slug" >&3
  fi
}

# HTTP status of an MCP initialize with one key, or "timeout" when curl gave
# up (exit 28). The key goes to curl in a 0600 header file, not argv, so ps
# never shows it. Call it in $(...): the EXIT trap that removes the header
# file belongs to that subshell.
mcp_status() {
  local hdr code rc=0
  hdr="$(umask 077 && mktemp)"
  # shellcheck disable=SC2064  # the path is fixed now
  trap "rm -f '$hdr'" EXIT
  printf 'Authorization: Bearer %s\nContent-Type: application/json\nAccept: application/json, text/event-stream\n' "$2" > "$hdr"
  code="$(curl -s -o /dev/null -m 20 -w '%{http_code}' -H @"$hdr" -X POST "${1}/mcp" \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"crm.sh","version":"0"}}}' 2>/dev/null)" || rc=$?
  rm -f "$hdr"
  if [[ "$rc" -eq 28 ]]; then
    printf 'timeout'
  else
    printf '%s' "${code:-000}"
  fi
}

# Values for addon_cli configure, from stdin JSON, parsed in the Hermes image
# (stack_python) so the host needs no python3. Prints "name<TAB>value" lines
# with shell-safe values only: url ("-" when unusable), access, local (1, 0,
# "-" when absent, "bad" when not a bool), has_ro and has_rw (1/0), or one
# "error<TAB>message" line. Each key is written to a 0600 file in the
# directory given as argv[1]; no key is printed.
# shellcheck disable=SC2016
CONNECT_PY='
import json, os, re, sys
out = sys.argv[1]
try:
    v = json.loads(sys.stdin.buffer.read().decode("utf-8"))
except (UnicodeDecodeError, ValueError):
    v = None
if not isinstance(v, dict):
    print("error\tstdin is not a JSON object")
    sys.exit(0)
url = v.get("url")
safe = re.compile(r"^https?://[^\s\"\\\\`$\x27]+$")
if isinstance(url, str) and re.match(r"^[A-Za-z]+://[^/?#]*@", url):
    print("error\turl holds a user name or password before the host")
    sys.exit(0)
for name in ("key_ro", "key_rw"):
    key = v.get(name)
    if key is not None and not (isinstance(key, str) and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key)):
        print("error\t" + name + " must be text without whitespace or control characters")
        sys.exit(0)
print("url\t" + (url if isinstance(url, str) and safe.match(url) else "-"))
access = v.get("access", "read")
print("access\t" + (access if access in ("read", "write") else "-"))
local = v.get("local")
print("local\t" + ("-" if local is None else "1" if local is True else "0" if local is False else "bad"))
for name in ("key_ro", "key_rw"):
    key = v.get(name)
    ok = isinstance(key, str) and key != ""
    if ok:
        fd = os.open(os.path.join(out, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
    print("has_" + name[4:] + "\t" + ("1" if ok else "0"))
'

# addon.sh's connect hook. Checks the values on stdin and each key against
# <url>/mcp; stores nothing (addon.sh writes the instance after this passes).
cmd_connect() {
  local slug="" json=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --json) json=1; shift ;;
      *) die "unknown option for connect: $1" ;;
    esac
  done
  [[ -n "$json" ]] || die "connect is addon.sh's hook: crm.sh connect --slug S --json, values on stdin"
  json_mode
  valid_slug "$slug" || fail_with bad_slug "slug '${slug}' must use lowercase letters, digits and hyphens, a letter first"

  local tmpd
  tmpd="$(umask 077 && mktemp -d)"
  # shellcheck disable=SC2064  # the path is fixed now
  trap "rm -rf '$tmpd'" EXIT
  local parsed
  parsed="$(stack_python --mount "$tmpd" -I -c "$CONNECT_PY" "$tmpd")" \
    || fail_with bad_input "couldn't read the values on stdin"
  local name value url="" access="" local_="" has_ro="" has_rw=""
  while IFS="$(printf '\t')" read -r name value; do
    case "$name" in
      error) fail_with bad_input "$value" ;;
      url) url="$value" ;;
      access) access="$value" ;;
      local) local_="$value" ;;
      has_ro) has_ro="$value" ;;
      has_rw) has_rw="$value" ;;
    esac
  done <<EOF
$parsed
EOF
  [[ "$url" != - && -n "$url" ]] || fail_with bad_input "url must be an http(s) URL"
  [[ "$access" != - && -n "$access" ]] || fail_with bad_input "access must be read or write"
  [[ "$local_" != bad ]] || fail_with bad_input "local must be true or false"
  [[ "$local_" == 1 ]] || local_=0

  url="$(norm_url "$url")"
  local is_local_url=0
  [[ "$url" == "$LOCAL_URL" ]] && is_local_url=1
  if [[ "$local_" == 1 && "$is_local_url" == 0 ]]; then
    fail_with local_mismatch "local is true, but the URL isn't ${LOCAL_URL}"
  fi
  if [[ "$local_" == 0 && "$is_local_url" == 1 ]]; then
    fail_with local_mismatch "${LOCAL_URL} is the local instance; send local: true"
  fi
  if [[ "$local_" == 1 ]] && ! is_true "$(env_get TWENTY_LOCAL_ENABLED 0)"; then
    fail_with local_off "the local instance is off (TWENTY_LOCAL_ENABLED=0)"
  fi
  if [[ "$access" == write && "$has_rw" != 1 ]]; then
    fail_with missing_key "write access needs key_rw"
  fi
  if [[ "$access" == read && "$has_ro" != 1 ]]; then
    fail_with missing_key "read access needs key_ro"
  fi

  # The host reaches the local instance on its published port.
  local check="$url"
  [[ "$local_" == 1 ]] && check="http://127.0.0.1:$(env_get TWENTY_PORT 3000)"
  local kind key code
  for kind in key_rw key_ro; do
    [[ -f "${tmpd}/${kind}" ]] || continue
    key="$(cat "${tmpd}/${kind}")"
    phase running "${kind}: checking"
    code="$(mcp_status "$check" "$key")"
    key=""
    case "$code" in
      200) phase "done" "${kind}: Twenty accepts it" ;;
      401) fail_with login_refused "Twenty rejects ${kind} (HTTP 401)" ;;
      403) fail_with no_access "Twenty refuses ${kind} (HTTP 403)" ;;
      timeout) fail_with timeout "no answer from ${url}/mcp within 20 seconds" ;;
      000) fail_with server_unreachable "no answer from ${url}/mcp; check the URL" ;;
      *) fail_with server_unreachable "${url}/mcp answered HTTP ${code} for ${kind}" ;;
    esac
  done
  printf '{"phase":"done","account":"%s","mcp":["crm-%s"]}\n' "$slug" "$slug" >&3
}

# addon.sh's disconnect hook. addon.sh removes the instance files afterwards;
# for a local workspace the note says the workspace stays in Twenty.
cmd_disconnect() {
  local slug="" json=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --json) json=1; shift ;;
      *) die "unknown option for disconnect: $1" ;;
    esac
  done
  [[ -n "$json" ]] || die "disconnect is addon.sh's hook: crm.sh disconnect --slug S --json (crm.sh remove forgets a workspace)"
  json_mode
  valid_slug "$slug" || fail_with bad_slug "slug '${slug}' must use lowercase letters, digits and hyphens, a letter first"
  # Read straight from the file (addon_store writes json.dumps(indent=2)), so
  # an unreadable store can't fail the hook.
  if grep -Eq '"local": *true' "${STORE}/instances/${slug}.json" 2>/dev/null; then
    printf '{"phase":"done","account":"%s","message":"%s"}\n' "$slug" \
      "The workspace and its keys still exist in Twenty; delete them there if you're done with them" >&3
  else
    printf '{"phase":"done","account":"%s"}\n' "$slug" >&3
  fi
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
  has_userinfo "$url" && die "--url must not hold a user name or password before the host"
  url="$(norm_url "$url")"
  local access="${TWENTY_ACCESS:-write}"
  case "$access" in
    read | write) ;;
    *) die "TWENTY_ACCESS must be read or write" ;;
  esac
  migrate_first
  if stored "$slug" && [[ -z "$force" ]]; then
    die "workspace '${slug}' exists (${STORE}/instances/${slug}.json); pass --force (make crm-add FORCE=1) to replace it"
  fi

  # With the keys in the environment nothing is asked, so addon.sh can call this.
  local rw="${TWENTY_KEY_RW:-}" ro="${TWENTY_KEY_RO:-}"
  if [[ -z "$rw" && -z "${TWENTY_KEY_RW+x}" && -t 0 ]]; then
    printf 'Read/write API key for %s%s: ' "$slug" "$( [[ "$access" == read ]] && echo ' (Enter to skip)')" >&2
    IFS= read -rs rw
    printf '\n' >&2
  fi
  if [[ -z "$ro" && -z "${TWENTY_KEY_RO+x}" && -t 0 ]]; then
    printf 'Read-only API key%s: ' "$( [[ "$access" == write ]] && echo ' (Enter to keep this CRM off unattended runs)')" >&2
    IFS= read -rs ro
    printf '\n' >&2
  fi
  if [[ "$access" == write && -z "$rw" ]]; then
    [[ -t 0 ]] || die "set TWENTY_KEY_RW (no terminal to ask in)"
    die "a read/write key is required with TWENTY_ACCESS=write"
  fi
  if [[ "$access" == read && -z "$ro" ]]; then
    die "TWENTY_ACCESS=read needs TWENTY_KEY_RO"
  fi
  local ws_url="${TWENTY_WORKSPACE_URL:-${url}/}"
  if ! { json_safe "$url" && json_safe "$ws_url"; }; then
    die "the URL holds a quote, backslash or control character"
  fi
  if ! { json_safe "$rw" && json_safe "$ro" && key_ok "$rw" && key_ok "$ro"; }; then
    die "a key holds whitespace, a quote, a backslash or a control character"
  fi

  # Check every key before writing anything.
  local kind key code
  for kind in RW RO; do
    key="$rw"; [[ "$kind" == RO ]] && key="$ro"
    [[ -n "$key" ]] || continue
    code="$(mcp_status "$url" "$key")"
    case "$code" in
      200) log "TWENTY_KEY_${kind}: Twenty accepts it" ;;
      000 | timeout) die "no answer from ${url}/mcp — check the URL; nothing written" ;;
      401 | 403) die "Twenty rejects TWENTY_KEY_${kind} (HTTP ${code}); nothing written" ;;
      *) die "${url}/mcp answered HTTP ${code} for TWENTY_KEY_${kind}; nothing written" ;;
    esac
  done

  local local_=false
  [[ "$url" == "$LOCAL_URL" ]] && local_=true
  # --force: the old instance is copied aside and restored if configure
  # fails, so a failed replace keeps the old workspace and its keys.
  local backup="" name
  if stored "$slug"; then
    backup="$(umask 077 && mktemp -d)"
    # shellcheck disable=SC2064  # the path is fixed now
    trap "rm -rf '$backup'" EXIT
    cp -p "${STORE}/instances/${slug}.json" "${backup}/instance.json"
    for name in key_ro key_rw; do
      [[ -f "${STORE}/secrets/${slug}.${name}" ]] && cp -p "${STORE}/secrets/${slug}.${name}" "${backup}/${name}"
    done
  fi
  if ! instance_json "$url" "$access" "$local_" "$ws_url" "" "$ro" "$rw" \
    | addon_cli configure twenty --instance "$slug" > /dev/null; then
    if [[ -n "$backup" ]]; then
      cp -p "${backup}/instance.json" "${STORE}/instances/${slug}.json" || true
      for name in key_ro key_rw; do
        if [[ -f "${backup}/${name}" ]]; then
          cp -p "${backup}/${name}" "${STORE}/secrets/${slug}.${name}" 2>/dev/null || true
        else
          rm -f "${STORE}/secrets/${slug}.${name}" 2>/dev/null || true
        fi
      done
      die "couldn't store workspace '${slug}' in ${STORE}; kept the old one"
    fi
    addon_cli remove-instance twenty "$slug" > /dev/null 2>&1 || true
    die "couldn't store workspace '${slug}' in ${STORE}"
  fi
  # configure merges, so drop a key the new values don't set.
  [[ -n "$ro" ]] || rm -f "${STORE}/secrets/${slug}.key_ro"
  [[ -n "$rw" ]] || rm -f "${STORE}/secrets/${slug}.key_rw"
  retire_ws_file "$slug"
  log "Wrote ${STORE}/instances/${slug}.json (crm-${slug})"
  if ! is_true "$(env_get TWENTY_MCP_ENABLED 0)"; then
    upsert_env "$ENV_PATH" TWENTY_MCP_ENABLED 1
    log "Set TWENTY_MCP_ENABLED=1"
  fi
  log "Next: make hermes-reload"
}

cmd_remove() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for remove: $1" ;;
    esac
  done
  valid_slug "$slug" || die "--slug must use lowercase letters, digits and hyphens"
  migrate_first
  stored "$slug" || die "no workspace '${slug}' in ${STORE}"
  load_rows
  local was_local=""
  is_local_slug "$slug" && was_local=1
  addon_cli remove-instance twenty "$slug" || die "couldn't remove workspace '${slug}'"
  log "Removed workspace ${slug} and its keys from ${STORE}"
  retire_ws_file "$slug"
  if [[ -n "$was_local" ]]; then
    log "The workspace and its keys still exist in Twenty; delete them there if you're done with them"
  fi
  log "Next: make hermes-reload (drops crm-${slug} from Hermes)"
}

cmd_access() {
  local slug="" access=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      read | write) access="$1"; shift ;;
      *) die "usage: crm.sh access --slug S read|write" ;;
    esac
  done
  valid_slug "$slug" || die "--slug must use lowercase letters, digits and hyphens"
  [[ -n "$access" ]] || die "usage: crm.sh access --slug S read|write"
  migrate_first
  stored "$slug" || die "no workspace '${slug}' in ${STORE}"
  load_rows
  if [[ "$access" == read && "$(row_get "$slug" 7)" != 1 ]]; then
    die "${slug} has no TWENTY_KEY_RO; read access needs one"
  fi
  if [[ "$access" == write && "$(row_get "$slug" 8)" != 1 ]]; then
    die "${slug} has no TWENTY_KEY_RW; write access needs one"
  fi
  printf '{"access":"%s"}' "$access" | addon_cli configure twenty --instance "$slug" > /dev/null \
    || die "couldn't store the access setting for ${slug}"
  log "Set TWENTY_ACCESS=${access} for ${slug}"
  log "Next: make hermes-reload"
}

# The slug open and login-link use without --slug: main, else the only
# local workspace. Needs load_rows first.
default_slug() {
  if has_row main; then
    printf 'main'
    return
  fi
  local all
  all="$(local_slugs)"
  [[ "$(printf '%s' "$all" | grep -c .)" -eq 1 ]] || die "several workspaces; pass SLUG=name"
  printf '%s' "$all"
}

# Prints a URL that signs a browser in to a local workspace as the owner, and
# nothing else on stdout. Twenty accepts it for LOGIN_TOKEN_EXPIRES_IN (15
# minutes by default).
cmd_login_link() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for login-link: $1" ;;
    esac
  done
  [[ -z "$slug" ]] || valid_slug "$slug" || die "--slug must use lowercase letters, digits and hyphens"
  migrate_first
  load_rows
  [[ -n "$slug" ]] || slug="$(default_slug)"
  valid_slug "$slug" || die "--slug must use lowercase letters, digits and hyphens"
  has_row "$slug" || die "no workspace '${slug}' — make crm-init"
  is_local_slug "$slug" || die "'${slug}' isn't on the local instance; sign in on its own Twenty"
  require_running >&2

  local username password password_file
  username="$(env_get TWENTY_USERNAME)"
  password="$(env_get TWENTY_PASSWORD)"
  password_file="$(env_get TWENTY_PASSWORD_FILE)"
  [[ -n "$username" && -n "${password}${password_file}" ]] \
    || die "no owner account in ${ENV_PATH} — make crm-init"
  crm_admin login-link --slug "$slug"
}

cmd_open() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for open: $1" ;;
    esac
  done
  migrate_first
  load_rows
  [[ -n "$slug" ]] || slug="$(default_slug)"
  local url
  has_row "$slug" || die "no workspace '${slug}' — make crm-init or make crm-add"
  url="$(row_get "$slug" 5)"
  [[ -n "$url" ]] || url="http://localhost:$(env_get TWENTY_PORT 3000)/"
  if is_local_slug "$slug"; then
    local link
    # Signed in when it works; otherwise the sign-in page and where the password is.
    if link="$(cmd_login_link --slug "$slug" 2>/dev/null)" && [[ "$link" == http* ]]; then
      url="$link"
    else
      local where="TWENTY_PASSWORD in .env"
      [[ -n "$(env_get TWENTY_PASSWORD_FILE)" ]] && where="in $(env_get TWENTY_PASSWORD_FILE)"
      log "Couldn't sign you in (./scripts/crm.sh login-link says why)"
      log "Sign in as $(env_get TWENTY_USERNAME owner@crm.localhost); the password is ${where}"
    fi
  fi
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
  else
    printf '%s\n' "$url"
  fi
}

# After a mode change, point each local workspace's browser URL at the right
# host: <subdomain>.localhost in multi-workspace mode.
update_workspace_urls() {
  local port multi slug id sub url
  port="$(env_get TWENTY_PORT 3000)"
  multi="$1"
  load_rows
  for slug in $(local_slugs); do
    url="http://localhost:${port}/"
    if [[ "$multi" == 1 ]]; then
      id="$(row_get "$slug" 6)"
      [[ -n "$id" ]] || continue
      sub="$(printf "SELECT subdomain FROM core.workspace WHERE id = :'id';\n" | twenty_psql -v id="$id" || true)"
      [[ -n "$sub" ]] || continue
      url="http://${sub}.localhost:${port}/"
    fi
    json_safe "$url" || continue
    printf '{"workspace_url":"%s"}' "$url" | addon_cli configure twenty --instance "$slug" > /dev/null \
      || { warn "${slug}: couldn't store ${url}"; continue; }
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
    load_rows
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

cmd_migrate() {
  [[ $# -eq 0 ]] || die "usage: crm.sh migrate"
  migrate_workspaces || die "some workspace files weren't migrated; fix the errors above and run crm.sh migrate again"
}

cmd_ensure() {
  mkdir -p "$WS_DIR"
  sync_bool
  migrate_first
  is_true "$(env_get TWENTY_LOCAL_ENABLED 0)" || return 0
  local dir
  dir="$(data_dir)"
  # Created here so Docker doesn't create them root-owned on Linux.
  mkdir -p "${dir}/db" "${dir}/storage"
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add) shift; cmd_add "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  access) shift; cmd_access "$@" ;;
  login-link) shift; cmd_login_link "$@" ;;
  open) shift; cmd_open "$@" ;;
  mode) shift; cmd_mode "$@" ;;
  ensure) cmd_ensure ;;
  migrate) shift; cmd_migrate "$@" ;;
  connect) shift; cmd_connect "$@" ;;
  disconnect) shift; cmd_disconnect "$@" ;;
  grant-admin) shift; cmd_grant_admin "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
