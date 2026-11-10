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
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

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

  init [--rotate | --server-only] [--json]
        Local server (ACTUAL_LOCAL_ENABLED=1, after make up): set a generated
        server password on a new server, create a budget or adopt the only
        one, check that it opens, and write compose/actual/budget.env.
        ACTUAL_BUDGET_SYNC_ID picks one of several budgets; ACTUAL_PASSWORD
        is the password of a server that already has one. Does nothing when
        budget.env exists, unless --rotate is given: then it sets a new
        generated server password on the server and in the file.
        --server-only (the Actual add-on's hooks.init, with --json): set up the
        local server without budget.env. The generated server password goes
        to <data root>/addons/actual/secrets/server_password; a budget named
        Budget is created when the server has none. Prints {"phase":"done"}
        with sync_id and name, and the same two as "result", when the server
        has exactly one budget. With ACTUAL_LOCAL_ENABLED off it does nothing
        and prints {"phase":"done"}; after turning it on, run
        scripts/addon.sh enable actual --reinit.
  add --url URL [--force]
        A budget on a remote server. ACTUAL_PASSWORD, ACTUAL_BUDGET_SYNC_ID
        and ACTUAL_BUDGET_ENCRYPTION_PASSWORD come from the environment, or
        are asked for in a terminal (passwords hidden). With no sync id the
        server's only budget is used. Nothing is written unless the budget
        opens with them.
  check   Open the budget in budget.env with actual-admin.mjs. Exit 0 when it
          opens, 2 for a wrong or missing encryption password, 3 when the
          server refuses the password, 4 when the server doesn't answer within
          ACTUAL_ADMIN_TIMEOUT_S seconds (default 45), 1 otherwise. make doctor
          runs this.
  budgets --json
        The budgets a credential can open, as JSON lines on stdout: one
        {"phase":"budget","sync_id":...,"name":...,"encrypted":...} each, then
        {"phase":"done"}, or one {"phase":"error",...}. Reads a JSON object
        on stdin: server ("local" or an http(s) URL) and session_token or
        password (local falls back to its stored server password).
  connect --slug SLUG [--replace] --json
        A hook addon.sh runs. Reads the budgets input on stdin (server,
        sync_id, session_token or password, optional encryption_password,
        profile "default", access read|write), opens the budget with it, then
        stores the session token and encryption password under
        <data root>/addons/actual/secrets/ (mode 600). Writes nothing when
        any check fails. Prints {"phase":"done","account":...,"mcp":[...]}.
        --replace (addon.sh connect --force) runs the same checks and then
        overwrites the secret files of an existing connection.
  disconnect --slug SLUG --json
        A hook addon.sh runs. Removes SLUG's files from that secrets folder;
        done also when there are none.
  status [--slug SLUG] --json
        Opens each connected budget (addons/actual/instances/*.json) with its
        stored session token. One {"phase":"budget","slug","profile","access",
        "state"} line each, state ok, token_expired, no_access,
        encryption_password, timeout, server_unreachable, bad_access or
        server_off (a local row while ACTUAL_LOCAL_ENABLED is off; no sign-in
        and no Actual check), then {"phase":"done"}. A bad state doesn't
        change the exit code.
  connect-cli --slug SLUG --url URL|local [--access read|write] [--sync-id ID] [--force]
        make budget-connect (FORCE=1 adds --force): asks for the server and
        encryption passwords (or takes ACTUAL_PASSWORD and
        ACTUAL_BUDGET_ENCRYPTION_PASSWORD), sends them on a pipe to addon.sh connect actual --instance SLUG --json and
        prints its result as [budget] lines. With local no server password
        is asked. --force passes --force to addon.sh connect, which replaces
        an existing connection of that name. Refuses while ACTUAL_MCP_ENABLED
        is off.
  disconnect-cli --slug SLUG
        make budget-disconnect: addon.sh disconnect actual --instance SLUG,
        printed as [budget] lines.
  open    Open the web app and copy the server password to the clipboard
  remove  Delete budget.env and clear ACTUAL_BUDGET_FILE; sets ACTUAL_MCP_ENABLED=0
          unless connected budgets remain. Server data stays.
  access read|write
        What chats may do with the budget: look things up (read) or make
        changes (write). Sets ACTUAL_ACCESS in .env.
  access SLUG read|write [--json]
        The same for one connected budget: saves it with addon.sh configure
        and reloads Hermes. Prints {"phase":"done","mcp":[...]}.
  ensure  Create <data root>/addons/actual/instances and secrets (0700). make
          ensure-local runs this so Docker doesn't create them root-owned on
          Linux when it bind-mounts them.

After init, add, remove or access: make up, then make hermes-reload
EOF
}

log() { printf '[budget] %s\n' "$1"; }
die() { printf '[budget] ERROR: %s\n' "$1" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The MCP containers run as uid 1000 and read budget.env and the connected
# budgets' secret files, mode 600, through bind mounts. On Linux that only
# works when the files' owner is uid 1000. Docker Desktop on macOS maps
# ownership, so it isn't affected. require_uid [WHAT] names what they read
# (default budget.env); in --json mode the refusal is a not_found line.
require_uid() {
  [[ "$(uname -s)" == Linux ]] || return 0
  local uid what="${1:-budget.env}"
  uid="$(id -u)"
  [[ "$uid" == 1000 ]] && return 0
  if [[ "$JSON" == 1 ]]; then
    emit_error not_found "on Linux the MCP containers read ${what} as uid 1000 (the image's node user), and the files would be mode 600 owned by uid ${uid}, so they couldn't open them. Run this as the uid 1000 user; nothing written"
  fi
  die "on Linux the MCP containers read ${what} as uid 1000 (the image's node user), and the file would be mode 600 owned by uid ${uid}, so they couldn't open it. Run this as the uid 1000 user; nothing written"
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
http_max=20

# A request to the local server. A POST body comes on stdin, so passwords
# never reach curl's argv; the session token goes in a header file.
http() {
  local hdr="${WORK}/hdr" rc=0
  (umask 077 && printf 'Content-Type: application/json\n' > "$hdr")
  [[ -n "$token" ]] && printf 'X-ACTUAL-TOKEN: %s\n' "$token" >> "$hdr"
  if [[ "$1" == POST ]]; then
    curl -s -m "$http_max" -H @"$hdr" -X POST --data-binary @- "${base}${2}" || rc=$?
  else
    curl -s -m "$http_max" -H @"$hdr" "${base}${2}" < /dev/null || rc=$?
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

# "<sync id>\t<name>\t<1|0>" per budget (1: it has an encryption key) in a /sync/list-user-files answer, leaving
# out deleted ones. A small JSON scanner, since the host may have no jq.
parse_user_files() {
  awk '
    function flush() {
      if (val["groupId"] != "" && val["deleted"] != "1" && val["deleted"] != "true") {
        name = val["name"]; gsub(/[\t\r\n]/, " ", name)
        enc = (val["encryptKeyId"] != "" && val["encryptKeyId"] != "null") ? 1 : 0
        print val["groupId"] "\t" name "\t" enc
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
              if (e == "u") {
                # Express escapes only control characters this way.
                e = " "; j += 4
              }
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

# --- JSON lines (--json) ---

JSON=0

# json_str TEXT: TEXT as a JSON string. Control characters become spaces;
# other characters, non-ASCII included, pass through.
json_str() {
  local s="$1"
  s="$(printf '%s' "$s" | tr '\001-\037' ' ')"
  printf '"%s"' "$(json_escape "$s")"
}

# json_escape_exact TEXT: TEXT escaped for inside a JSON string with every
# character kept: control characters become \uXXXX (or \t, \n, \r).
json_escape_exact() {
  local s i c
  s="$(json_escape "$1")"
  if [[ "$s" == *[[:cntrl:]]* ]]; then
    for i in $(seq 1 31); do
      # shellcheck disable=SC2059
      printf -v c "\\$(printf '%03o' "$i")"
      case "$i" in
        9) s="${s//"$c"/\\t}" ;;
        10) s="${s//"$c"/\\n}" ;;
        13) s="${s//"$c"/\\r}" ;;
        *) s="${s//"$c"/$(printf '\\u%04x' "$i")}" ;;
      esac
    done
  fi
  printf '%s' "$s"
}

# emit_line JSON: one line on stdout in --json mode, nothing otherwise.
emit_line() {
  [[ "$JSON" == 1 ]] && printf '%s\n' "$1"
  return 0
}

# emit_done [KEY JSON_VALUE]...: {"phase":"done",...}. VALUE is already JSON.
# shellcheck disable=SC2120
emit_done() {
  local out='{"phase":"done"'
  while [[ $# -ge 2 ]]; do
    out="${out},$(json_str "$1"):$2"
    shift 2
  done
  emit_line "${out}}"
}

# emit_error NAME MESSAGE: the error line on stdout, "error: MESSAGE" on
# stderr, then exit: 2 for bad_slug, unknown_profile, bad_access, not_found;
# 4 for the rest. Without --json it is today's "[budget] ERROR:" and exit 1.
emit_error() {
  if [[ "$JSON" != 1 ]]; then
    die "$2"
  fi
  printf '{"phase":"error","error":%s,"message":%s}\n' "$(json_str "$1")" "$(json_str "$2")"
  printf 'error: %s\n' "$2" >&2
  case "$1" in
    bad_slug | unknown_profile | bad_access | not_found) exit 2 ;;
    *) exit 4 ;;
  esac
}

# shellcheck disable=SC2034
in_server="" in_sync_id="" in_profile="" in_access=""
# shellcheck disable=SC2034
in_session_token="" in_password="" in_encryption_password=""

# read_input: one JSON object from stdin into the in_* variables. A missing
# or non-string field is empty. The values come back shell-quoted by python,
# never on an argv.
read_input() {
  local assigns
  assigns="$(stack_python -c '
import json, shlex, sys
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError
except ValueError:
    sys.exit(3)
for k in ("server", "sync_id", "profile", "access", "session_token", "password", "encryption_password"):
    v = d.get(k)
    print("in_%s=%s" % (k, shlex.quote(v if isinstance(v, str) else "")))
')" || emit_error not_found "stdin is not a JSON object"
  eval "$assigns"
}

# server_base SERVER: sets base (and local_pw for "local"). The local
# server's password comes from secrets/server_password, or on an install that
# budget.sh init set up (v0.3.12) from budget.env, which is then copied there.
local_pw=""
server_base() {
  case "$1" in
    local)
      base="http://127.0.0.1:$(env_get ACTUAL_PORT 5006)"
      local sdir="" pwf url
      secrets_dir
      pwf="${sdir}/server_password"
      url="$(file_get ACTUAL_SERVER_URL)"
      if [[ -s "$pwf" ]]; then
        local_pw="$(cat "$pwf")"
      elif [[ "${url%/}" == "$LOCAL_URL" && -n "$(file_get ACTUAL_PASSWORD)" ]]; then
        local_pw="$(file_get ACTUAL_PASSWORD)"
        mkdir -p "$sdir" || emit_error not_found "couldn't create ${sdir}"
        write_secret server_password "$local_pw"
      else
        emit_error not_found "the local server's password isn't stored (addons/actual/secrets/server_password is missing); run scripts/addon.sh enable actual --reinit (or make budget-init)"
      fi
      ;;
    http://?* | https://?*)
      base="$1"
      while [[ "$base" == */ ]]; do base="${base%/}"; done
      ;;
    *) emit_error server_unreachable "server must be an http(s) URL or local" ;;
  esac
}

# api_call METHOD PATH: the answer in api_resp, or the matching error line
# when curl fails. A POST sends api_body on curl's stdin. Non-JSON answers are server_unreachable.
api_resp=""
api_body=""
api_call() {
  local rc=0
  http_max=45
  if [[ "$1" == POST ]]; then
    api_resp="$(printf '%s' "$api_body" | http POST "$2")" || rc=$?
  else
    api_resp="$(http "$1" "$2")" || rc=$?
  fi
  api_body=""
  http_max=20
  case "$rc" in
    0) ;;
    28) emit_error timeout "$(safe_url "$base") didn't answer within 45 s" ;;
    *) emit_error server_unreachable "no answer from $(safe_url "$base")" ;;
  esac
  case "$api_resp" in
    "{"*) ;;
    *) emit_error server_unreachable "$(safe_url "$base") didn't answer like an Actual server" ;;
  esac
}

# sign_in: sets token from in_session_token, or from a password login
# (in_password, else the local server's stored one).
sign_in() {
  local pw reason
  if [[ -n "$in_session_token" ]]; then
    token="$in_session_token"
    return 0
  fi
  pw="$in_password"
  [[ -n "$pw" ]] || pw="$local_pw"
  [[ -n "$pw" ]] || emit_error login_refused "no password or session token given"
  api_body="$(printf '{"loginMethod":"password","password":"%s"}' "$(json_escape_exact "$pw")")"
  api_call POST /account/login
  token="$(json_field "$api_resp" token)"
  [[ -n "$token" ]] && return 0
  reason="$(json_field "$api_resp" reason)"
  case "$reason" in
    invalid-password) emit_error login_refused "the server refused the password" ;;
    token-not-found | token-expired) emit_error token_expired "the session token has expired" ;;
    *) emit_error server_unreachable "sign-in failed: ${reason:-unexpected answer}" ;;
  esac
}

cmd_budgets() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --json) JSON=1 ;;
      *) die "usage: budget.sh budgets --json < input" ;;
    esac
  done
  [[ "$JSON" == 1 ]] || die "usage: budget.sh budgets --json < input"
  read_input
  server_base "$in_server"
  sign_in
  api_call GET /sync/list-user-files
  json_ok "$api_resp" || case "$(json_field "$api_resp" reason)" in
    token-not-found | token-expired | unauthorized) emit_error token_expired "the session token has expired" ;;
    *) emit_error server_unreachable "couldn't list the budgets: $(json_field "$api_resp" reason)" ;;
  esac
  local rows id name enc
  rows="$(printf '%s' "$api_resp" | parse_user_files)"
  while IFS=$'\t' read -r id name enc; do
    [[ -n "$id" ]] || continue
    emit_line "$(printf '{"phase":"budget","sync_id":%s,"name":%s,"encrypted":%s}' \
      "$(json_str "$id")" "$(json_str "$name")" "$([[ "$enc" == 1 ]] && echo true || echo false)")"
  done <<< "$rows"
  # shellcheck disable=SC2119
  emit_done
}

# hook_args CMD ARGS...: sets slug and JSON=1 from "--slug SLUG --json", and
# for connect replace=1 from --replace (addon.sh connect --force).
hook_args() {
  local cmd="$1" usage
  shift
  slug=""
  usage="usage: budget.sh $cmd --slug SLUG [--replace] --json"
  [[ "$cmd" == connect ]] || usage="usage: budget.sh $cmd --slug SLUG --json"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) JSON=1 ;;
      --slug) [[ $# -ge 2 ]] || die "$usage"; slug="$2"; shift ;;
      --replace) [[ "$cmd" == connect ]] || die "$usage"; replace=1 ;;
      *) die "$usage" ;;
    esac
    shift
  done
  [[ "$JSON" == 1 ]] || die "$usage"
  [[ "$slug" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || emit_error bad_slug "slug must be 1-31 characters of a-z, 0-9 and -, starting with a letter"
}

# secrets_dir: sets sdir to <data root>/addons/actual/secrets.
secrets_dir() {
  local root
  root="$(resolve_data_root_path "${BUDGET_DATA_ROOT:-}")"
  validate_data_root_path "$root" 2> "${WORK}/root.err" \
    || emit_error not_found "$(cat "${WORK}/root.err")"
  sdir="${root}/addons/actual/secrets"
}

# write_secret NAME VALUE: sdir/NAME at mode 600, through a temp file and mv.
write_secret() {
  local t
  t="$(umask 077; mktemp "${sdir}/.write.XXXXXX")"
  printf '%s' "$2" > "$t"
  chmod 600 "$t"
  mv -f "$t" "${sdir}/$1"
}

# admin_rc_name RC LOGIN_NAME: the error name for a nonzero exit of
# actual-admin.mjs check. Exit 3 is whatever LOGIN_NAME says (connect:
# login_refused or token_expired; status: token_expired). Anything not listed
# is server_unreachable.
admin_rc_name() {
  case "$1" in
    2) printf 'encryption_password' ;;
    3) printf '%s' "$2" ;;
    4) printf 'timeout' ;;
    5) printf 'no_access' ;;
    *) printf 'server_unreachable' ;;
  esac
}

# connect --slug SLUG [--replace] --json. --replace (addon.sh connect --force)
# runs the same checks in the same order as a new connect; the secret files
# are written only after they all pass, so a failed run leaves the old ones.
# The writes already replace a file of the same name.
cmd_connect() {
  # replace is set by hook_args and changes nothing below: see above.
  # shellcheck disable=SC2034
  local slug="" sdir="" rc=0 mcp name replace=0
  hook_args connect "$@"
  read_input
  [[ -n "$in_profile" ]] || in_profile=default
  [[ -n "$in_access" ]] || in_access="read"
  [[ "$in_profile" == default ]] || emit_error unknown_profile "profile \"$in_profile\" doesn't exist; only \"default\" does"
  case "$in_access" in
    read | write) ;;
    *) emit_error bad_access "access must be read or write" ;;
  esac
  [[ -n "$in_sync_id" ]] || emit_error no_access "no budget sync id given"
  require_uid "the connected budgets' secret files"
  secrets_dir
  server_base "$in_server"
  sign_in
  a_url="$base"
  [[ "$in_server" == local ]] && a_url="$LOCAL_URL"
  a_pw="" a_token="$token" a_sync="$in_sync_id" a_enc="$in_encryption_password"
  admin check > /dev/null 2> "${WORK}/admin.err" || rc=$?
  a_token=""
  if [[ "$rc" != 0 ]]; then
    local login_name=login_refused
    [[ -z "$in_session_token" ]] || login_name=token_expired
    name="$(admin_rc_name "$rc" "$login_name")"
    case "$name" in
      encryption_password) emit_error "$name" "the budget's encryption password is missing or wrong" ;;
      login_refused) emit_error "$name" "the server refused the password" ;;
      token_expired) emit_error "$name" "the session token has expired" ;;
      timeout) emit_error "$name" "$(safe_url "$a_url") didn't answer within ${ACTUAL_ADMIN_TIMEOUT_S:-45} s" ;;
      no_access) emit_error "$name" "this account can't open that budget" ;;
      *) if [[ "$rc" == 6 ]]; then
           emit_error "$name" "no answer from $(safe_url "$a_url")"
         else
           emit_error "$name" "opening the budget failed: $(admin_error)"
         fi ;;
    esac
  fi
  mkdir -p "$sdir"
  write_secret "${slug}.session_token" "$token"
  if [[ -n "$in_encryption_password" ]]; then
    write_secret "${slug}.encryption_password" "$in_encryption_password"
  else
    rm -f "${sdir}/${slug}.encryption_password"
  fi
  mcp="$(json_str "budget-${slug}-ro")"
  [[ "$in_access" == write ]] && mcp="${mcp},$(json_str "budget-${slug}")"
  emit_done account "$(json_str "$slug")" mcp "[${mcp}]"
}

# disconnect: done with or without files to remove. addon.sh reports an
# unknown slug itself, and must still drop a row whose secrets are gone.
cmd_disconnect() {
  local slug="" sdir="" f
  hook_args disconnect "$@"
  secrets_dir
  for f in "${sdir}/${slug}".*; do
    [[ -e "$f" ]] || continue
    rm -f "$f"
  done
  # shellcheck disable=SC2119
  emit_done
}

# --- actual-admin.mjs in the MCP image ---

a_url=""
a_pw=""
a_sync=""
a_enc=""
a_token=""

# Credentials go to the container by name (-e NAME), with the values in
# docker's environment, so they stay out of the process list. It mounts
# no-budget.env, so a run never depends on (or reads) the real budget file.
admin() {
  ACTUAL_BUDGET_FILE=./compose/actual/no-budget.env \
  ACTUAL_SERVER_URL="$a_url" ACTUAL_PASSWORD="$a_pw" ACTUAL_SESSION_TOKEN="$a_token" \
  ACTUAL_BUDGET_SYNC_ID="$a_sync" ACTUAL_BUDGET_ENCRYPTION_PASSWORD="$a_enc" \
  ACTUAL_ADMIN_TIMEOUT_S="${ACTUAL_ADMIN_TIMEOUT_S:-}" \
    docker compose run --rm --no-deps -T \
      -e ACTUAL_SERVER_URL -e ACTUAL_PASSWORD -e ACTUAL_SESSION_TOKEN -e ACTUAL_BUDGET_SYNC_ID -e ACTUAL_BUDGET_ENCRYPTION_PASSWORD \
      -e ACTUAL_ADMIN_TIMEOUT_S \
      --entrypoint node actual-mcp-rw /opt/actual-admin.mjs "$@"
}

admin_error() {
  local msg
  msg="$(grep -v '^[[:space:]]*$' "${WORK}/admin.err" 2>/dev/null | tail -1 || true)"
  printf '%s' "${msg:-no details; docker compose logs may say more}"
}

# A URL for messages: any user:password@ in it is masked.
safe_url() {
  printf '%s' "$1" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@#\1***@#'
}

# actual-admin.mjs exits 4 when the server doesn't answer within
# ACTUAL_ADMIN_TIMEOUT_S seconds (default 45).
timed_out() {
  die "no answer from $(safe_url "$a_url") within ${ACTUAL_ADMIN_TIMEOUT_S:-45} s (ACTUAL_ADMIN_TIMEOUT_S); nothing written"
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
    4) timed_out ;;
    *) die "couldn't open budget ${a_sync} on $(safe_url "$a_url"): $(admin_error); nothing written" ;;
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

# note TEXT: a [budget] log line, except in --json mode (stdout is JSON only).
note() {
  [[ "$JSON" == 1 ]] || log "$1"
  return 0
}

# probe_server: sets base and resp (the needs-bootstrap answer), or fails.
probe_server() {
  base="http://127.0.0.1:$(env_get ACTUAL_PORT 5006)"
  resp="$(http GET /account/needs-bootstrap)" || resp=""
  json_ok "$resp" \
    || emit_error server_unreachable "actual-server isn't answering on ${base}; ./scripts/setup.sh --budget (or ACTUAL_LOCAL_ENABLED=1), then make up"
}

# bootstrap_server PASSWORD: sets the server password of a new server.
bootstrap_server() {
  resp="$(printf '{"password":"%s"}' "$1" | http POST /account/bootstrap)" || resp=""
  json_ok "$resp" || emit_error server_unreachable "bootstrap failed: $(json_field "$resp" reason)"
  note "Set a generated server password on actual-server"
}

# create_budget PASSWORD: creates a budget named Budget; sets created_sync.
created_sync=""
create_budget() {
  local rc=0 created
  note "Creating a budget named Budget"
  a_url="$LOCAL_URL" a_pw="$1" a_sync="" a_enc=""
  created="$(admin create Budget 2> "${WORK}/admin.err")" || rc=$?
  if [[ "$rc" -eq 4 ]]; then
    emit_error timeout "no answer from $(safe_url "$a_url") within ${ACTUAL_ADMIN_TIMEOUT_S:-45} s (ACTUAL_ADMIN_TIMEOUT_S); nothing written"
  fi
  [[ "$rc" -eq 0 ]] || emit_error server_unreachable "couldn't create a budget: $(admin_error)"
  created_sync="$(printf '%s\n' "$created" | grep -v '^[[:space:]]*$' | tail -1)"
  [[ -n "$created_sync" ]] || emit_error server_unreachable "creating a budget printed no sync id"
}

cmd_init() {
  local rotate="" server_only=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --rotate) rotate=1; shift ;;
      --server-only) server_only=1; shift ;;
      --json) JSON=1; shift ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  if [[ -n "$server_only" ]]; then
    [[ -z "$rotate" ]] || die "--rotate doesn't go with --server-only"
    init_server_only
    return 0
  fi
  require_uid
  if [[ -n "$rotate" && ! -f "$BUDGET_FILE" ]]; then
    die "no ${BUDGET_REL}, so there is no password to rotate; run make budget-init first"
  fi
  if [[ -f "$BUDGET_FILE" && -z "$rotate" ]]; then
    log "${BUDGET_REL} exists; nothing to do (--rotate sets a new server password; make budget-remove starts over)"
    return 0
  fi
  local resp
  probe_server

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
    bootstrap_server "$pw"
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
    create_budget "$pw"
    sync="$created_sync"
  elif [[ "$n" -eq 1 ]]; then
    sync="$(printf '%s' "$files" | cut -f1)"
    log "Adopting the existing budget '$(printf '%s' "$files" | cut -f2)'"
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

# password_accepted PASSWORD: whether the server's login accepts it.
password_accepted() {
  api_body="$(printf '{"loginMethod":"password","password":"%s"}' "$(json_escape_exact "$1")")"
  api_call POST /account/login
  [[ -n "$(json_field "$api_resp" token)" ]]
}

# truthy VALUE: 1, true, yes or on (any case), as addon.sh reads .env flags.
truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# init --server-only: the local server's password lives in
# <data root>/addons/actual/secrets/server_password (mode 600), a budget is
# created when the server has none, and budget.env, ACTUAL_BUDGET_FILE and
# ACTUAL_MCP_ENABLED stay untouched. Safe to run again. With the local server
# off it does nothing, so enabling the add-on on a remote-only install works;
# turning the server on later takes addon.sh enable actual --reinit.
init_server_only() {
  local sdir="" pwf pw="" alt="" alt_pw="" resp files n sync="" name=""
  if ! truthy "$(env_get ACTUAL_LOCAL_ENABLED 0)"; then
    note "ACTUAL_LOCAL_ENABLED is off, so there is no local server to set up; after turning it on, run scripts/addon.sh enable actual --reinit"
    # shellcheck disable=SC2119
    emit_done
    return 0
  fi
  docker info > /dev/null 2>&1 || emit_error server_unreachable "Docker daemon is not running"
  secrets_dir
  mkdir -p "$sdir" || emit_error not_found "couldn't create ${sdir}"
  pwf="${sdir}/server_password"
  probe_server
  if [[ "$resp" == *'"bootstrapped":false'* ]]; then
    # The password is stored before the server is told, so a failed run can't lose it.
    if [[ -s "$pwf" ]]; then
      pw="$(cat "$pwf")"
    else
      pw="$(openssl rand -hex 24)"
      write_secret server_password "$pw"
    fi
    bootstrap_server "$pw"
  else
    [[ -s "$pwf" ]] && pw="$(cat "$pwf")"
    alt="$(file_get ACTUAL_PASSWORD)"
    [[ -n "$pw" ]] || pw="$alt"
    [[ -z "$alt" || "$alt" == "$pw" ]] || alt_pw="$alt"
    if [[ -z "$pw" ]]; then
      [[ -n "$pw" ]] || emit_error login_refused "actual-server already has a password and none is stored (addons/actual/secrets/server_password is missing)"
    fi
  fi
  in_session_token="" in_password="$pw"
  if [[ -n "$alt_pw" ]] && ! password_accepted "$pw"; then
    # The stored copy is stale (a rotation changed budget.env): retry once.
    pw="$alt_pw"
    in_password="$pw"
  fi
  sign_in
  # A password adopted from budget.env is kept where the add-on looks for it.
  [[ "$(cat "$pwf" 2> /dev/null)" == "$pw" ]] || write_secret server_password "$pw"

  api_call GET /sync/list-user-files
  json_ok "$api_resp" || emit_error server_unreachable "couldn't list the server's budgets: $(json_field "$api_resp" reason)"
  files="$(printf '%s' "$api_resp" | parse_user_files)"
  n="$(printf '%s' "$files" | grep -c . || true)"
  if [[ "$n" -eq 0 ]]; then
    create_budget "$pw"
    sync="$created_sync"
    name=Budget
  elif [[ "$n" -eq 1 ]]; then
    sync="$(printf '%s' "$files" | cut -f1)"
    name="$(printf '%s' "$files" | cut -f2)"
    note "The server has the budget '${name}'"
  else
    note "The server has ${n} budgets"
  fi
  if [[ -n "$sync" ]]; then
    # With exactly one budget, also as result: what addon.sh enable returns to
    # its caller.
    emit_done sync_id "$(json_str "$sync")" name "$(json_str "$name")" \
      result "{\"sync_id\":$(json_str "$sync"),\"name\":$(json_str "$name")}"
  else
    # shellcheck disable=SC2119
    emit_done
  fi
  note "Set up actual-server (no budget.env written)"
}

# POST /account/change-password takes the current password and a new one.
rotate_password() {
  local url old new resp sdir
  url="$(file_get ACTUAL_SERVER_URL)"
  [[ "${url%/}" == "$LOCAL_URL" ]] \
    || die "${BUDGET_REL} points at $(safe_url "$url"), not the local server; change that server's password there, then make budget-add FORCE=1"
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
  # Keep the add-on's copy of the password in step, when there is one.
  local root
  root="$(resolve_data_root_path "${BUDGET_DATA_ROOT:-}")"
  if validate_data_root_path "$root" 2> /dev/null && [[ -f "${root}/addons/actual/secrets/server_password" ]]; then
    sdir="${root}/addons/actual/secrets"
    write_secret server_password "$new"
  fi
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
  [[ -n "$pw" ]] || pw="$(ask 1 "Server password for $(safe_url "$url")")"
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
      4) timed_out ;;
      *) die "couldn't list the budgets on $(safe_url "$url"): $(admin_error); nothing written" ;;
    esac
    list="$(printf '%s\n' "$list" | grep "$(printf '\t')" || true)"
    n="$(printf '%s' "$list" | grep -c . || true)"
    if [[ "$n" -eq 0 ]]; then
      die "$(safe_url "$url") has no budgets; create one in its web app first"
    elif [[ "$n" -gt 1 ]]; then
      printf '%s\n' "$list" | awk -F'\t' '{ printf "  %s  %s\n", $1, $2 }' >&2
      die "$(safe_url "$url") has ${n} budgets; pick one with ACTUAL_BUDGET_SYNC_ID (make budget-add SYNC_ID=<sync id>)"
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
    4) log "No answer from $(safe_url "$a_url") within ${ACTUAL_ADMIN_TIMEOUT_S:-45} s (ACTUAL_ADMIN_TIMEOUT_S)" >&2 ;;
    *) log "Couldn't open budget ${a_sync} on $(safe_url "$a_url"): $(admin_error)" >&2; rc=1 ;;
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
  # Connected budgets (instances/*.json) keep the MCP servers on.
  local root rows=""
  root="$(resolve_data_root_path "${BUDGET_DATA_ROOT:-}")"
  if validate_data_root_path "$root" 2> /dev/null; then
    rows="$(find "${root}/addons/actual/instances" -maxdepth 1 -name '*.json' 2> /dev/null | head -1)"
  fi
  if [[ -n "$rows" ]]; then
    log "Removed ${BUDGET_REL}; cleared ACTUAL_BUDGET_FILE. ACTUAL_MCP_ENABLED stays 1 while budgets are connected"
  else
    upsert_env "$ENV_PATH" ACTUAL_MCP_ENABLED 0
    log "Removed ${BUDGET_REL}; cleared ACTUAL_BUDGET_FILE and set ACTUAL_MCP_ENABLED=0"
  fi
  if [[ "${url%/}" == "$LOCAL_URL" ]]; then
    log "actual-server and its budgets are untouched (ACTUAL_DATA_DIR); ACTUAL_LOCAL_ENABLED is unchanged"
  fi
  log "Next: make hermes-reload (drops budget from every profile), then"
  log "  docker compose stop actual-mcp-rw actual-mcp-ro; running ones keep the old credentials until they stop"
}

# instances_dir: sets idir to <data root>/addons/actual/instances.
instances_dir() {
  secrets_dir
  idir="${sdir%/secrets}/instances"
}

# status_row SLUG: one {"phase":"budget",...} line for the instance.
status_row() {
  local s="$1" assigns state rc=0 tf ef
  local i_server="" i_sync="" i_profile="" i_access=""
  assigns="$(stack_python --mount "$idir" -c '
import json, shlex, sys
try:
    d = json.load(open(sys.argv[1]))
    if not isinstance(d, dict):
        d = {}
except (OSError, ValueError):
    d = {}
def val(k, missing):
    v = d.get(k, missing)
    return v if isinstance(v, str) else ""
print("i_server=%s" % shlex.quote(val("server", "")))
print("i_sync=%s" % shlex.quote(val("sync_id", "")))
print("i_profile=%s" % shlex.quote(val("profile", "default")))
print("i_access=%s" % shlex.quote(val("access", "read")))
' "${idir}/${s}.json" 2> /dev/null)" || assigns=""
  eval "$assigns"
  tf="${sdir}/${s}.session_token"
  ef="${sdir}/${s}.encryption_password"
  case "$i_access" in
    read | write) ;;
    *) state=bad_access ;;
  esac
  # A local row while the local server is off: no sign-in, no Actual check.
  if [[ -z "${state:-}" && "$i_server" == local ]] && ! truthy "$(env_get ACTUAL_LOCAL_ENABLED 0)"; then
    state=server_off
  fi
  if [[ -z "${state:-}" && ! -s "$tf" ]]; then
    state=token_expired
  fi
  if [[ -z "${state:-}" ]]; then
    case "$i_server" in
      local) a_url="$LOCAL_URL" ;;
      *) a_url="$i_server"; while [[ "$a_url" == */ ]]; do a_url="${a_url%/}"; done ;;
    esac
    a_pw="" a_token="$(cat "$tf")" a_sync="$i_sync" a_enc=""
    [[ ! -s "$ef" ]] || a_enc="$(cat "$ef")"
    admin check > /dev/null 2> "${WORK}/admin.err" < /dev/null || rc=$?
    a_token="" a_enc=""
    if [[ "$rc" == 0 ]]; then
      state=ok
    else
      state="$(admin_rc_name "$rc" token_expired)"
    fi
  fi
  emit_line "$(printf '{"phase":"budget","slug":%s,"profile":%s,"access":%s,"state":%s}' \
    "$(json_str "$s")" "$(json_str "$i_profile")" "$(json_str "$i_access")" "$(json_str "$state")")"
}

cmd_status() {
  local slug="" idir="" sdir="" f s
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) JSON=1 ;;
      --slug) [[ $# -ge 2 ]] || die "usage: budget.sh status [--slug SLUG] --json"; slug="$2"; shift ;;
      *) die "usage: budget.sh status [--slug SLUG] --json" ;;
    esac
    shift
  done
  [[ "$JSON" == 1 ]] || die "usage: budget.sh status [--slug SLUG] --json"
  slug_check() { [[ "$1" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; }
  if [[ -n "$slug" ]]; then
    slug_check "$slug" || emit_error bad_slug "slug must be 1-31 characters of a-z, 0-9 and -, starting with a letter"
  fi
  instances_dir
  if [[ -n "$slug" ]]; then
    [[ -f "${idir}/${slug}.json" ]] || emit_error not_found "no budget connected as ${slug}"
    status_row "$slug"
  else
    for f in "${idir}"/*.json; do
      [[ -e "$f" ]] || continue
      s="$(basename "$f" .json)"
      slug_check "$s" || continue
      status_row "$s"
    done
  fi
  # shellcheck disable=SC2119
  emit_done
}

# access SLUG read|write [--json]: one connected budget. addon.sh configure
# stores the value but doesn't reload Hermes, so this runs both bootstraps and
# vault.sh reload-plugins itself. BUDGET_ADDON_SH and BUDGET_VAULT_SH are test
# hooks.
cmd_access_budget() {
  local slug="" want="" arg n=0 idir="" sdir=""
  for arg in "$@"; do
    case "$arg" in
      --json) JSON=1 ;;
      *)
        n=$((n + 1))
        case "$n" in
          1) slug="$arg" ;;
          2) want="$arg" ;;
          *) die "usage: budget.sh access SLUG read|write [--json]" ;;
        esac
        ;;
    esac
  done
  [[ "$n" == 2 ]] || die "usage: budget.sh access SLUG read|write [--json]"
  [[ "$slug" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || emit_error bad_slug "slug must be 1-31 characters of a-z, 0-9 and -, starting with a letter"
  case "$want" in
    read | write) ;;
    *) emit_error bad_access "access must be read or write" ;;
  esac
  instances_dir
  [[ -f "${idir}/${slug}.json" ]] || emit_error not_found "no budget connected as ${slug}"
  printf '{"access":"%s"}' "$want" \
    | "${BUDGET_ADDON_SH:-${ROOT}/scripts/addon.sh}" configure actual --instance "$slug" > /dev/null 2> "${WORK}/addon.err" \
    || emit_error server_unreachable "couldn't save the access: $(grep -v '^[[:space:]]*$' "${WORK}/addon.err" | tail -1 || true)"
  local rrc=0
  docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap > /dev/null 2> "${WORK}/reload.err" < /dev/null || rrc=1
  if [[ "$rrc" == 0 ]]; then
    docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap > /dev/null 2>> "${WORK}/reload.err" < /dev/null || rrc=1
  fi
  [[ "$rrc" == 0 ]] \
    || emit_error server_unreachable "saved the access, but a Hermes bootstrap failed: $(grep -v '^[[:space:]]*$' "${WORK}/reload.err" | tail -1 || true)"
  "${BUDGET_VAULT_SH:-${ROOT}/scripts/vault.sh}" reload-plugins > /dev/null 2>&1 < /dev/null \
    || log "WARNING: vault.sh reload-plugins failed; run make hermes-reload"
  local mcp
  mcp="$(json_str "budget-${slug}-ro")"
  [[ "$want" == write ]] && mcp="${mcp},$(json_str "budget-${slug}")"
  [[ "$JSON" == 1 ]] || log "Set access ${want} for ${slug}"
  emit_done mcp "[${mcp}]"
}

cmd_access() {
  [[ $# -le 1 ]] || { cmd_access_budget "$@"; return; }
  [[ $# -eq 1 ]] || die "usage: budget.sh access read|write"
  case "$1" in
    read | write) ;;
    *) die "usage: budget.sh access read|write" ;;
  esac
  upsert_env "$ENV_PATH" ACTUAL_ACCESS "$1"
  log "Set ACTUAL_ACCESS=$1"
  log "Next: make hermes-reload"
}

# The actual-mcp containers and the Hermes bootstraps bind-mount the store.
# Not fatal: on a store Docker already created root-owned, make up goes on and
# the warning says how to fix it.
cmd_ensure() {
  [[ $# -eq 0 ]] || die "ensure takes no options"
  local root store
  root="$(resolve_data_root_path "${BUDGET_DATA_ROOT:-}")"
  if ! validate_data_root_path "$root" 2> "${WORK}/root.err"; then
    printf '[budget] warning: %s; addons/actual not created\n' "$(cat "${WORK}/root.err")" >&2
    return 0
  fi
  store="${root}/addons/actual"
  if ! mkdir -p "${store}/instances" "${store}/secrets" 2> /dev/null \
      || ! chmod 700 "$store" "${store}/instances" "${store}/secrets" 2> /dev/null; then
    local fix="sudo chown -R \"\$(id -u):\$(id -g)\" '${store}'"
    printf '[budget] warning: could not create %s, its instances and its secrets at 0700 (another user, often root, owns one); run %s\n' \
      "$store" "$fix" >&2
  fi
}

# connect-cli --slug SLUG --url URL|local [--access read|write] [--sync-id ID]
# (make budget-connect): asks for the passwords in a terminal, builds the
# connect JSON, and pipes it to addon.sh connect. The passwords travel on
# pipes only. ACTUAL_PASSWORD and ACTUAL_BUDGET_ENCRYPTION_PASSWORD in the
# environment are used instead of asking, as for add. Without --sync-id the
# server's only budget is used. BUDGET_ADDON_SH is a test hook.
cmd_connect_cli() {
  local slug="" url="" access="" sync="" force="" usage="usage: budget.sh connect-cli --slug SLUG --url URL|local [--access read|write] [--sync-id ID] [--force]"
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == --force ]]; then
      force=--force
      shift
      continue
    fi
    [[ $# -ge 2 ]] || die "$usage"
    case "$1" in
      --slug) slug="$2" ;;
      --url) url="$2" ;;
      --access) access="$2" ;;
      --sync-id) sync="$2" ;;
      *) die "$usage" ;;
    esac
    shift 2
  done
  [[ -n "$slug" && -n "$url" ]] || die "$usage"
  # addon.sh connect stores the row either way, but Hermes registers nothing
  # for it while the add-on is off.
  truthy "$(env_get ACTUAL_MCP_ENABLED 0)" \
    || emit_error not_found "the Actual add-on is off; run scripts/addon.sh enable actual first"
  local pw="" enc=""
  if [[ "$url" != local ]]; then
    pw="${ACTUAL_PASSWORD:-}"
    [[ -n "$pw" ]] || pw="$(ask 1 "Server password for $(safe_url "$url")")"
    [[ -n "$pw" ]] || die "set ACTUAL_PASSWORD (no terminal to ask in)"
  fi
  enc="${ACTUAL_BUDGET_ENCRYPTION_PASSWORD:-}"
  [[ -n "$enc" ]] || enc="$(ask 1 "Encryption password (Enter if the budget isn't end-to-end encrypted)")"
  if [[ -z "$sync" ]]; then
    local lines n=0 id="" ph
    lines="$(printf '{"server":"%s","password":"%s"}' "$(json_escape_exact "$url")" "$(json_escape_exact "$pw")" \
      | "${BASH_SOURCE[0]}" budgets --json || true)"
    while IFS= read -r ph; do
      case "$ph" in
        '{"phase":"budget"'*) n=$((n + 1)); id="$(printf '%s' "$ph" | stack_python -c 'import json,sys; print(json.load(sys.stdin)["sync_id"])')" ;;
        '{"phase":"error"'*) die "couldn't list the budgets: $ph" ;;
      esac
    done <<< "$lines"
    [[ "$n" == 1 ]] || die "the server has ${n} budgets; run again with SYNC_ID=... (budget.sh budgets --json lists them)"
    sync="$id"
  fi
  local body
  body="$(printf '{"server":"%s","sync_id":"%s"' "$(json_escape_exact "$url")" "$(json_escape_exact "$sync")")"
  [[ -z "$access" ]] || body="${body},\"access\":\"$(json_escape_exact "$access")\""
  [[ -z "$pw" ]] || body="${body},\"password\":\"$(json_escape_exact "$pw")\""
  [[ -z "$enc" ]] || body="${body},\"encryption_password\":\"$(json_escape_exact "$enc")\""
  printf '%s}' "$body" | addon_human connect "$slug" ${force:+"$force"}
}

# addon_human ACTION SLUG [--force]: runs addon.sh ACTION actual --instance
# SLUG [--force] --json with this stdin and turns its JSON lines into [budget] lines for a person.
# Exits with addon.sh's code. BUDGET_ADDON_SH is a test hook.
addon_human() {
  local rc=0 lines
  lines="$("${BUDGET_ADDON_SH:-${ROOT}/scripts/addon.sh}" "$1" actual --instance "$2" ${3:+"$3"} --json 2> "${WORK}/addon.err")" || rc=$?
  printf '%s\n' "$lines" | stack_python -c '
import json, sys
action, slug = sys.argv[1], sys.argv[2]
seen = 0
for line in sys.stdin:
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if not isinstance(d, dict):
        continue
    if d.get("phase") == "done":
        seen = 1
        if action == "connect":
            mcp = ", ".join(m for m in d.get("mcp") or [] if isinstance(m, str))
            print("[budget] Connected %s%s" % (slug, "; Hermes entries: " + mcp if mcp else ""))
        else:
            print("[budget] Disconnected %s" % slug)
    elif d.get("phase") == "error":
        seen = 1
        print("[budget] ERROR: %s (%s)" % (d.get("message") or "addon.sh failed", d.get("error")), file=sys.stderr)
sys.exit(0 if seen else 3)
' "$1" "$2" || grep -v '^[[:space:]]*$' "${WORK}/addon.err" >&2 || true
  return "$rc"
}

# disconnect-cli --slug SLUG (make budget-disconnect): addon.sh disconnect,
# with [budget] lines instead of JSON.
cmd_disconnect_cli() {
  local slug="" usage="usage: budget.sh disconnect-cli --slug SLUG"
  [[ $# -eq 2 && "$1" == --slug && -n "$2" ]] || die "$usage"
  slug="$2"
  addon_human disconnect "$slug" < /dev/null
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add) shift; cmd_add "$@" ;;
  check) shift; cmd_check "$@" ;;
  open) shift; cmd_open "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  access) shift; cmd_access "$@" ;;
  status) shift; cmd_status "$@" ;;
  budgets) shift; cmd_budgets "$@" ;;
  connect) shift; cmd_connect "$@" ;;
  disconnect) shift; cmd_disconnect "$@" ;;
  connect-cli) shift; cmd_connect_cli "$@" ;;
  disconnect-cli) shift; cmd_disconnect_cli "$@" ;;
  ensure) shift; cmd_ensure "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
