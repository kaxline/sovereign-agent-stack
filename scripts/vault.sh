#!/usr/bin/env bash
# Bitwarden add-on: the accounts the broker signs in to, local mode and access
# requests. See docs/bitwarden.md.
#
# Test hooks: VAULT_ACCOUNTS_DIR replaces compose/bitwarden/accounts (the folder
# the broker mounts), VAULT_HERMES_DATA_DIR replaces data/hermes,
# VAULT_ADDON_SH replaces scripts/addon.sh and VAULT_ADDON_STORE replaces the
# add-on store (<data root>/addons/bitwarden), so scripts/test-vault-sh.sh can
# run against temp folders.
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
ACCOUNTS_DIR="${VAULT_ACCOUNTS_DIR:-${ROOT}/compose/bitwarden/accounts}"
# Mounted at /opt/data in hermes (docker-compose.yml).
HERMES_DATA_DIR="${VAULT_HERMES_DATA_DIR:-${ROOT}/data/hermes}"
ADDON_SH="${VAULT_ADDON_SH:-${ROOT}/scripts/addon.sh}"
PLUGINS_MARKER=.addon-plugins-changed
# The containers' user (compose/bitwarden-broker/Dockerfile).
CONTAINER_UID=10001
INIT_WAIT_SECONDS=600
# Regexes live in variables: bash 3.2 parses some unquoted ones in [[ =~ ]] differently.
SLUG_RE='^[a-z0-9-]+$'
# The add-on store's rule (scripts/lib/addon_store.py SLUG_RE): addon.sh
# disconnect, and so Boundary, can only remove an account whose slug fits it.
STORE_SLUG_RE='^[a-z][a-z0-9-]{0,30}$'
STORE_SLUG_RULE="start with a letter and have at most 31 characters"
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+$'
URL_RE='^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]?#]*)?$'
ID_RE='^[A-Za-z0-9][A-Za-z0-9-]{0,63}$'
NO_SPACE_RE='^[^[:space:]]+$'
NO_CNTRL_RE='^[^[:cntrl:]]+$'
RETRY_RE='^[0-9]+$'

usage() {
  cat <<'EOF'
Usage: scripts/vault.sh <command> [options]

  init [--owner EMAIL]
        Local mode: Vaultwarden in this stack, the agent account, the
        organization "Assistant" and your Owner membership. EMAIL defaults to
        VAULT_OWNER_EMAIL. Re-running picks up after the last finished step.
  add --slug S --mode cloud|selfhosted [--region us|eu] [--url URL]
      [--scope folder:NAME|collection:ID|all] [--unlock stored|manual] [--force]
        An account on bitwarden.com or your own server. Asks for the API key
        (and, with stored unlock, the master password) in hidden prompts.
        --scope all also needs CONFIRM_SCOPE_ALL=1 in the environment.
  add --slug S --json
        For Boundary's connect: {mode, region, url, scope, unlock, client_id,
        client_secret, master_password} on stdin, unlock manual by default,
        no --force or scope all. JSON lines out; a failed check keeps nothing.
  unlock --slug S [--json]
                    Unlock a manual account in the broker (hidden prompt; with
                    --json, {master_password} on stdin and no prompt)
  lock --slug S [--json]
                    Lock a manual account
  sync [--json]     Re-read every unlocked account's items now
  status --json     One JSON line per account: state, error, retry_after
  requests [--json] The agent's pending access requests
  remove --slug S [--json]
                    Forget an account: its file, secrets and bw data
  open              Open the local web vault
  backup DIR        Copy the local Vaultwarden, secrets and account files to DIR
  restore DIR [--force]
                    Put a backup back; refuses to overwrite data without --force

  pre-up            Create the accounts folder before docker compose up (make up runs it)
  follow-hermes     Recreate the broker in hermes' current network namespace and
                    list the manual accounts to unlock (run after hermes is
                    recreated or restarted; make hermes-reload runs it)
  reload-plugins    Ask the running gateway to reload its plugins for each
                    profile whose add-on login backends a bootstrap changed
                    (make up runs it through up-hook)

pre-up and follow-hermes are silent while BITWARDEN_ENABLED is off.
reload-plugins is silent while no profile is marked.
After add or remove: make up. make doctor shows each account's state.

add and remove without --json, and up-hook for account files it has not
seen, record each account in the add-on store (scripts/addon.sh configure
bitwarden --instance S). addon.sh disconnect, which Boundary runs, removes
only accounts recorded there. A failed record is a warning. add takes only a
slug that starts with a letter and has at most 31 characters, as the store does.

Test hooks: VAULT_ACCOUNTS_DIR replaces compose/bitwarden/accounts,
VAULT_HERMES_DATA_DIR replaces data/hermes, VAULT_ADDON_SH replaces
scripts/addon.sh and VAULT_ADDON_STORE replaces <data root>/addons/bitwarden.
EOF
}

# In --json mode stdout carries JSON lines only, so log goes to stderr.
log() {
  if [[ "$JSON" == 1 ]]; then printf '[vault] %s\n' "$1" >&2; else printf '[vault] %s\n' "$1"; fi
}
# With JSON_DIE=1 (add and remove --json) die prints a command_failed line
# and exits 4.
JSON_DIE=0
die() {
  if [[ "$JSON_DIE" == 1 ]]; then
    JSON_DIE=0
    emit_error command_failed "$1"
  fi
  printf '[vault] ERROR: %s\n' "$1" >&2
  exit 1
}

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# A regex, not a glob: bash 3.2 bracket ranges follow the locale.
valid_slug() { [[ "$1" =~ $SLUG_RE ]]; }
valid_email() { [[ "$1" =~ $EMAIL_RE ]]; }
need_slug() { valid_slug "$1" || die "--slug must use lowercase letters, digits and hyphens"; }
# Options that take a value: need_value "$@" before reading $2.
need_value() { [[ $# -ge 2 ]] || emit_error bad_input "$1 needs a value"; }

# One value from FILE_TEXT (see load_file), without sourcing anything: CR,
# surrounding spaces and one pair of quotes dropped, as the broker's parser does.
text_get() {
  local v
  v="$(printf '%s\n' "$FILE_TEXT" | grep -E "^[[:space:]]*${1}[[:space:]]*=" | head -1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"
  v="$(printf '%s' "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "$v"
}

data_dir() {
  resolve_data_root_path "${BITWARDEN_HOST_DATA_DIR:-$(env_get BITWARDEN_HOST_DATA_DIR ./data/addons/bitwarden)}"
}

account_file() { printf '%s/%s.env' "$ACCOUNTS_DIR" "$1"; }

need_account() {
  [[ -f "$(account_file "$1")" ]] || die "no account '$1' in ${ACCOUNTS_DIR#"${ROOT}"/} — make vault-add or make vault-init"
}

# --- JSON lines (--json) ---

JSON=0

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '%s' "$s"
}

# json_str TEXT: TEXT as a JSON string. Control characters become spaces;
# other characters, non-ASCII included, pass through.
json_str() {
  local s="$1"
  s="$(printf '%s' "$s" | tr '\001-\037' ' ')"
  printf '"%s"' "$(json_escape "$s")"
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

# emit_error NAME MESSAGE [RETRY_AFTER]: the error line on stdout, "error:
# MESSAGE" on stderr, then exit: 2 for bad_input, bad_slug, exists, not_found;
# 4 for the rest. A positive integer RETRY_AFTER adds "retryAfter". Without
# --json it is die.
emit_error() {
  if [[ "$JSON" != 1 ]]; then
    die "$2"
  fi
  local retry=""
  if [[ "${3:-}" =~ $RETRY_RE ]] && (( 10#$3 > 0 )); then
    retry=",\"retryAfter\":$((10#$3))"
  fi
  printf '{"phase":"error","error":%s,"message":%s%s}\n' "$(json_str "$1")" "$(json_str "$2")" "$retry"
  printf 'error: %s\n' "$2" >&2
  case "$1" in
    bad_input | bad_slug | exists | not_found) exit 2 ;;
    *) exit 4 ;;
  esac
}

# read_input KEY...: one JSON object from stdin into in_KEY variables. A
# missing or non-string field is empty. The values come back shell-quoted by
# python, never on an argv.
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
for k in sys.argv[1:]:
    v = d.get(k)
    print("in_%s=%s" % (k, shlex.quote(v if isinstance(v, str) else "")))
' "$@")" || emit_error bad_input "stdin is not a JSON object"
  eval "$assigns"
}

# --- Docker ---------------------------------------------------------------

broker_running() {
  command -v docker >/dev/null 2>&1 || return 1
  compose_with_profile bitwarden ps --status running -q bitwarden-broker 2>/dev/null | grep -q .
}

require_broker() {
  broker_running || die "the Bitwarden broker is not running — set BITWARDEN_ENABLED=1, then make up"
}

broker_exec() { compose_with_profile bitwarden exec -T bitwarden-broker "$@"; }
broker_cli() { broker_exec python3 -m broker.cli "$@"; }

# bitwarden-setup takes the terminal for its hidden prompts (tty: true in compose).
setup_run() { docker compose --profile bitwarden-tools run --rm bitwarden-setup "$@"; }

# "<state> <error_class>" for a slug from the broker, or nothing.
broker_state() {
  local rows="$1" slug="$2"
  printf '%s\n' "$rows" | awk -F '\t' -v s="$slug" '$1 == s { print $4 " " $5; exit }'
}

# True unless the running broker's account list leaves out the slug; a list
# that cannot be read counts as holding it.
broker_may_hold() {
  local rows
  rows="$(broker_cli accounts 2>/dev/null)" || return 0
  printf '%s\n' "$rows" | awk -F '\t' -v s="$1" '$1 == s { f = 1 } END { exit !f }'
}

# A recreate, not a restart: a restart keeps the network namespace of the
# hermes container the broker was created with, which is gone once hermes has
# been recreated, and the broker then stays exited.
restart_broker() {
  compose_with_profile bitwarden up -d --no-deps --force-recreate bitwarden-broker >/dev/null
}

# Polls until the broker answers or VAULT_BROKER_WAIT seconds pass.
wait_for_broker() {
  local wait="${VAULT_BROKER_WAIT:-60}" start
  start="$(date +%s)"
  until broker_cli accounts >/dev/null 2>&1; do
    (( $(date +%s) - start < wait )) || return 1
    sleep 2
  done
}

# Polls the broker until SLUG leaves "locked" (stored accounts unlock at start)
# or VAULT_BROKER_WAIT seconds pass. Prints "<state> <error_class>".
wait_for_account() {
  local slug="$1" unlock="$2" wait="${VAULT_BROKER_WAIT:-60}" start rows got
  start="$(date +%s)"
  while :; do
    rows="$(broker_cli accounts 2>/dev/null || true)"
    got="$(broker_state "$rows" "$slug")"
    if [[ -n "$got" && ( "$unlock" == manual || "${got%% *}" != locked ) ]]; then
      printf '%s' "$got"
      return 0
    fi
    if [[ $(( $(date +%s) - start )) -ge "$wait" ]]; then
      printf '%s' "$got"
      return 0
    fi
    sleep 2
  done
}

# --- Files ----------------------------------------------------------------
#
# On Linux, secrets/<slug>/, local/ and the account files belong to the
# containers' user (uid 10001, modes 700 and 600), and secrets/ and the accounts
# folder stay with you at mode 711. vault.sh never chmods or writes a path it
# does not own: what it cannot read or write it reaches through the
# bitwarden-setup container, which runs as that user, and it says so once per
# path. An unreadable file is never taken as empty.

NOTED=" "
FILE_TEXT=""
LOADED=""

# The path inside bitwarden-setup for a host path under its mounts.
to_container() {
  local p="$1" data
  data="$(data_dir)"
  case "$p" in
    "$ACCOUNTS_DIR"/*) printf '/accounts/%s' "${p#"$ACCOUNTS_DIR"/}" ;;
    "$data"/local/*) printf '/local/%s' "${p#"$data"/local/}" ;;
    "$data"/secrets/*) printf '/secrets/%s' "${p#"$data"/secrets/}" ;;
    *) return 1 ;;
  esac
}

# Runs SCRIPT with sh in bitwarden-setup, with ARGS (container paths) as $1...
# and this process's stdin as its stdin.
container_sh() {
  local script="$1"
  shift
  docker compose --profile bitwarden-tools run --rm --no-deps -T --entrypoint sh bitwarden-setup -c "$script" sh "$@"
}

note_container() {
  [[ "$NOTED" == *" $1 "* ]] && return 0
  NOTED="${NOTED}$1 "
  printf '[vault] Note: %s belongs to the containers'"'"' user (uid %s); using bitwarden-setup to reach it.\n' \
    "$1" "$CONTAINER_UID" >&2
}

# Exit 3: no such file.
# shellcheck disable=SC2016  # expanded by the sh that runs it
READ_SCRIPT='[ -e "$1" ] || exit 3; cat "$1"'

# Sets FILE_TEXT to PATH's contents, through bitwarden-setup when the host
# cannot read it. With "optional", a missing file is empty; otherwise a file
# that cannot be read stops the run. Call it in this shell, not in $(...).
load_file() {
  local path="$1" optional="${2:-}" rc=0 cpath parent
  [[ "$path" == "$LOADED" ]] && return 0
  parent="$(dirname "$path")"
  if [[ -r "$path" ]]; then
    FILE_TEXT="$(cat "$path")"
  elif [[ ! -e "$path" && ( ! -e "$parent" || -x "$parent" ) ]]; then
    [[ -n "$optional" ]] || die "no such file: ${path}"
    FILE_TEXT=""
  else
    cpath="$(to_container "$path")" || die "cannot read ${path}"
    note_container "$path"
    FILE_TEXT="$(container_sh "$READ_SCRIPT" "$cpath")" || rc=$?
    if [[ "$rc" -eq 3 && -n "$optional" ]]; then
      FILE_TEXT=""
    elif [[ "$rc" -ne 0 ]]; then
      FILE_TEXT=""
      LOADED=""
      die "could not read ${path} through bitwarden-setup (docker compose build bitwarden-setup, then try again)"
    fi
  fi
  LOADED="$path"
}

# Operations in secrets/<slug>/, run with sh on the host when it can write
# there and in bitwarden-setup otherwise. $1 is the folder. Values come on
# stdin, so they never appear in any process's argv.
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_WRITE='umask 077; t="$1/.$2.tmp"; cat > "$t" && chmod 600 "$t" && mv -f "$t" "$1/$2"'
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_RM='rm -f "$1/$2"'
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_BACKUP='for n in client_secret master_password; do
  if [ -f "$1/$n" ]; then cp -p "$1/$n" "$1/$n.bak" || exit 1; fi
done'
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_RESTORE='for n in client_secret master_password; do
  if [ -f "$1/$n.bak" ]; then mv -f "$1/$n.bak" "$1/$n"; else rm -f "$1/$n"; fi
done'
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_DROP_BACKUP='rm -f "$1/client_secret.bak" "$1/master_password.bak"'
# shellcheck disable=SC2016  # expanded by the sh that runs it
SECRET_CLEAR='rm -f "$1/client_secret" "$1/master_password" "$1/client_secret.bak" "$1/master_password.bak" \
  "$1/.client_secret.tmp" "$1/.master_password.tmp"'

slug_op() {
  local slug="$1" script="$2" sdir
  shift 2
  sdir="$(data_dir)/secrets/${slug}"
  if [[ -w "$sdir" ]]; then
    sh -c "$script" sh "$sdir" "$@"
  else
    note_container "$sdir"
    container_sh "$script" "/secrets/${slug}" "$@"
  fi
}

# Writes VALUE to secrets/<slug>/NAME, mode 600.
write_secret() {
  printf '%s\n' "$3" | slug_op "$1" "$SECRET_WRITE" "$2"
}

# Removes secrets/<slug>/ and what is in it.
remove_secrets() {
  local sdir
  sdir="$(data_dir)/secrets/$1"
  [[ -d "$sdir" ]] || return 0
  if [[ -w "$sdir" && -x "$sdir" ]]; then
    rm -rf "${sdir:?}"
  else
    slug_op "$1" "$SECRET_CLEAR" < /dev/null || true
    # An empty folder goes with write access to secrets/, which stays yours.
    rmdir "$sdir" 2>/dev/null || log "Could not remove ${sdir}; remove it as root"
  fi
}

# Sets a mode only on a path you own.
chmod_own() {
  [[ -O "$2" ]] && chmod "$1" "$2"
  return 0
}

# On Linux the containers run as uid 10001 and must own their folders and the
# account files. Run as root, chown them; otherwise print the command. secrets/
# itself is never passed here: it stays yours.
linux_owner() {
  [[ $# -gt 0 ]] || return 0
  [[ "$(uname -s)" == Linux ]] || return 0
  if [[ "$(id -u)" == 0 ]]; then
    chown -R "${CONTAINER_UID}:${CONTAINER_UID}" "$@"
  else
    log "The Bitwarden containers run as uid ${CONTAINER_UID}; give them these: sudo chown -R ${CONTAINER_UID}:${CONTAINER_UID} $*"
  fi
}

# Creates the data folders before any container starts, so Docker does not
# create them root-owned. Prints the folders it created. A folder under one you
# cannot enter belongs to the containers, which make their own.
ensure_dirs() {
  local data="$1" d p rest blocked
  shift
  for d in "$@"; do
    blocked=""
    p="$data"
    rest="$d"
    while [[ "$rest" == */* ]]; do
      p="${p}/${rest%%/*}"
      rest="${rest#*/}"
      if [[ -d "$p" && ! -x "$p" ]]; then
        blocked=1
        break
      fi
    done
    [[ -z "$blocked" ]] || continue
    if [[ -e "${data}/${d}" && ! -d "${data}/${d}" ]]; then
      die "${data}/${d} exists and is not a folder"
    fi
    if [[ ! -d "${data}/${d}" ]]; then
      (umask 077 && mkdir -p "${data}/${d}")
      printf '%s\n' "${data}/${d}"
    fi
  done
}

# The broker bind-mounts the accounts folder; created here so Docker does not
# create it root-owned before any account exists.
ensure_accounts_dir() {
  if [[ ! -d "$ACCOUNTS_DIR" ]]; then
    mkdir -p "$ACCOUNTS_DIR"
    chmod_own 711 "$ACCOUNTS_DIR"
  fi
}

# --- The add-on store -------------------------------------------------------
#
# addon.sh keeps one instances/<slug>.json of non-secret values per account in
# the add-on store. Boundary lists accounts from status --json, but disconnects
# them through addon.sh disconnect, which refuses an account the store lacks.
# addon.sh connect and disconnect write it around add and remove --json. Terminal add and remove,
# and up-hook for account files the store lacks, write it through addon.sh
# configure. --json mode never calls addon.sh: connect and disconnect hold the
# add-on lock while their hook runs, so the call would end in busy.

store_dir() {
  printf '%s' "${VAULT_ADDON_STORE:-$(resolve_data_root_path)/addons/bitwarden}"
}

# store_values MODE SERVER SCOPE UNLOCK: the instance values as one JSON
# object. A cloud server gives region us or eu, a selfhosted one url; an empty
# scope or unlock is left out, so the manifest's default applies.
store_values() {
  local out
  out="{\"mode\":$(json_str "$1")"
  case "$1" in
    cloud)
      case "$2" in
        https://vault.bitwarden.com) out="${out},\"region\":\"us\"" ;;
        https://vault.bitwarden.eu) out="${out},\"region\":\"eu\"" ;;
      esac
      ;;
    selfhosted) out="${out},\"url\":$(json_str "$2")" ;;
  esac
  [[ -z "$3" ]] || out="${out},\"scope\":$(json_str "$3")"
  [[ -z "$4" ]] || out="${out},\"unlock\":$(json_str "$4")"
  printf '%s}' "$out"
}

# store_record SLUG VALUES: addon.sh configure bitwarden --instance SLUG with
# VALUES on stdin. A failure prints a warning and returns 1.
store_record() {
  [[ "$JSON" != 1 ]] || return 0
  local rc=0
  printf '%s\n' "$2" | "$ADDON_SH" configure bitwarden --instance "$1" > /dev/null || rc=$?
  [[ "$rc" != 0 ]] || return 0
  printf '[vault] Warning: scripts/addon.sh configure exited %s, so Boundary cannot disconnect %s until make up records it in the add-on store\n' \
    "$rc" "$1" >&2
  return 1
}

# store_forget SLUG: addon.sh configure bitwarden --instance SLUG --remove. A
# failure prints a warning. A slug outside the store's rule was never there.
store_forget() {
  [[ "$JSON" != 1 ]] || return 0
  [[ "$1" =~ $STORE_SLUG_RE ]] || return 0
  local rc=0
  "$ADDON_SH" configure bitwarden --instance "$1" --remove < /dev/null > /dev/null || rc=$?
  [[ "$rc" != 0 ]] || return 0
  printf '[vault] Warning: scripts/addon.sh configure exited %s, so the add-on store still holds %s and connecting it again from Boundary fails with exists; scripts/addon.sh configure bitwarden --instance %s --remove drops it\n' \
    "$rc" "$1" "$1" >&2
}

# up-hook: records the account in FILE_TEXT when the store has no entry for
# SLUG. local belongs to make vault-init and is never recorded. A slug the
# store refuses (added before add checked it) gets a line saying what to do.
store_backfill() {
  local slug="$1"
  [[ "$slug" != local ]] || return 0
  if [[ ! "$slug" =~ $STORE_SLUG_RE ]]; then
    printf '[vault] Warning: Boundary cannot disconnect %s: an account slug must %s. Remove it with make vault-remove SLUG=%s and add it again under such a slug\n' \
      "$slug" "$STORE_SLUG_RULE" "$slug" >&2
    return 0
  fi
  [[ ! -e "$(store_dir)/instances/${slug}.json" ]] || return 0
  if store_record "$slug" "$(store_values "$(text_get BW_MODE)" "$(text_get BW_SERVER)" \
    "$(text_get BW_SCOPE)" "$(text_get BW_UNLOCK)")"; then
    log "Recorded ${slug} in the add-on store"
  fi
  return 0
}

# --- add ------------------------------------------------------------------

# Reads one line from stdin without echo into REPLY_SECRET.
REPLY_SECRET=""
ask_hidden() {
  REPLY_SECRET=""
  printf '%s' "$1" >&2
  IFS= read -rs REPLY_SECRET || { printf '\n' >&2; die "no input for: ${1%: }"; }
  printf '\n' >&2
  REPLY_SECRET="${REPLY_SECRET%$'\r'}"
}

UNLOCK_WARNING="With stored unlock the broker keeps this account's master password, which opens the whole vault; \
the scope only limits what the broker lists and fills. A collection: scope with a separate member account \
lets Bitwarden enforce the limit instead (docs/bitwarden.md)."

# add --json reads {mode, region, url, scope, unlock, client_id,
# client_secret, master_password} from stdin and never prompts, writes .env or
# offers bitwarden-setup. scope defaults to folder:Agent and unlock to manual.
# Any outcome but a sign-in the broker confirms removes what it wrote.
cmd_add() {
  local slug="" mode="" region="" url="" scope="folder:Agent" unlock="stored" force="" a
  for a in "$@"; do
    if [[ "$a" == --json ]]; then
      JSON=1
      JSON_DIE=1
      unlock=manual
    fi
  done
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) shift ;;
      --slug) need_value "$@"; slug="$2"; shift 2 ;;
      --mode | --region | --url | --scope | --unlock)
        [[ "$JSON" != 1 ]] || emit_error bad_input "add --json reads $1 from stdin"
        need_value "$@"
        case "$1" in
          --mode) mode="$2" ;;
          --region) region="$2" ;;
          --url) url="$2" ;;
          --scope) scope="$2" ;;
          --unlock) unlock="$2" ;;
        esac
        shift 2
        ;;
      --force)
        [[ "$JSON" != 1 ]] || emit_error bad_input "--force does not work with --json; disconnect the account first"
        force=1; shift ;;
      *) emit_error bad_input "unknown option for add: $1" ;;
    esac
  done
  if [[ "$JSON" == 1 ]]; then
    valid_slug "$slug" || emit_error bad_slug "--slug must use lowercase letters, digits and hyphens"
  else
    need_slug "$slug"
  fi
  [[ "$slug" != local ]] || emit_error bad_input "the slug local belongs to make vault-init"
  [[ "$slug" =~ $STORE_SLUG_RE ]] \
    || emit_error bad_slug "--slug must ${STORE_SLUG_RULE}, so Boundary can disconnect the account"
  local in_mode="" in_region="" in_url="" in_scope="" in_unlock="" in_client_id="" in_client_secret="" in_master_password=""
  if [[ "$JSON" == 1 ]]; then
    read_input mode region url scope unlock client_id client_secret master_password
    mode="$in_mode"
    region="$in_region"
    url="$in_url"
    [[ -z "$in_scope" ]] || scope="$in_scope"
    [[ -z "$in_unlock" ]] || unlock="$in_unlock"
  fi
  local server
  case "$mode" in
    cloud)
      [[ -z "$url" ]] || emit_error bad_input "--url is for --mode selfhosted; cloud takes --region us or eu"
      case "$(lower "$region")" in
        "" | us) server="https://vault.bitwarden.com" ;;
        eu) server="https://vault.bitwarden.eu" ;;
        *) emit_error bad_input "--region must be us or eu" ;;
      esac
      ;;
    selfhosted)
      [[ -z "$region" ]] || emit_error bad_input "--region is for --mode cloud"
      while [[ "$url" == */ ]]; do url="${url%/}"; done
      [[ "$url" =~ $URL_RE ]] \
        || emit_error bad_input "--url must be an https:// URL with no user, query or fragment"
      server="$url"
      ;;
    local) emit_error bad_input "local accounts come from make vault-init" ;;
    *) emit_error bad_input "--mode must be cloud or selfhosted" ;;
  esac
  case "$scope" in
    all)
      [[ "$JSON" != 1 ]] \
        || emit_error bad_input "scope all lets the agent reach every item in the vault; it is only available from make vault-add with CONFIRM_SCOPE_ALL=1"
      # A separate variable: make vault-add SCOPE=all exports SCOPE, so SCOPE itself confirms nothing.
      [[ "${CONFIRM_SCOPE_ALL:-}" == 1 ]] \
        || die "--scope all lets the agent reach every item in the vault; set CONFIRM_SCOPE_ALL=1 (make vault-add SCOPE=all CONFIRM_SCOPE_ALL=1) to confirm"
      ;;
    folder:*)
      local name="${scope#folder:}"
      [[ -n "$name" && "$name" != " "* && "$name" != *" " ]] || emit_error bad_input "--scope folder:NAME needs a name without spaces at either end"
      [[ "$name" != *\"* && "$name" =~ $NO_CNTRL_RE ]] || emit_error bad_input "the folder name must not contain \" or control characters"
      ;;
    collection:*)
      [[ "${scope#collection:}" =~ $ID_RE ]] || emit_error bad_input "--scope collection:ID needs a Bitwarden collection id"
      ;;
    *) emit_error bad_input "--scope must be folder:<name>, collection:<id> or all" ;;
  esac
  case "$unlock" in
    stored | manual) ;;
    *) emit_error bad_input "--unlock must be stored or manual" ;;
  esac
  local file
  file="$(account_file "$slug")"
  if [[ "$JSON" == 1 && -e "$file" ]]; then
    emit_error exists "an account named ${slug} exists"
  fi
  if [[ -e "$file" && -z "$force" ]]; then
    die "${file#"${ROOT}"/} exists; pass --force (make vault-add FORCE=1) to replace it"
  fi

  local client_id secret="" password=""
  if [[ "$JSON" == 1 ]]; then
    client_id="$in_client_id"
    secret="$in_client_secret"
    [[ "$unlock" == stored ]] && password="$in_master_password"
    in_client_id=""
    in_client_secret=""
    in_master_password=""
  else
    log "The agent signs in with this account's personal API key (web vault: Settings > Security > Keys > API key)."
    ask_hidden "client_id: "
    client_id="$REPLY_SECRET"
  fi
  [[ "$client_id" =~ $NO_SPACE_RE ]] || emit_error bad_input "the client_id must not be empty or contain spaces"
  if [[ "$JSON" != 1 ]]; then
    ask_hidden "client_secret: "
    secret="$REPLY_SECRET"
  fi
  [[ -n "$secret" ]] || emit_error bad_input "the client_secret must not be empty"
  if [[ "$unlock" == stored ]]; then
    if [[ "$JSON" != 1 ]]; then
      ask_hidden "Master password (stored for the broker): "
      password="$REPLY_SECRET"
    fi
    [[ -n "$password" ]] || emit_error bad_input "the master password must not be empty with --unlock stored"
  fi
  REPLY_SECRET=""
  if [[ "$unlock" == stored && "$scope" != collection:* ]]; then
    printf '[vault] Warning: %s\n' "$UNLOCK_WARNING" >&2
  fi
  if [[ "$JSON" == 1 ]]; then
    require_broker_json
    empty_bw_dir "$slug"
    emit_line '{"phase":"setup","step":"write","status":"running","message":"writing the account file and secrets"}'
  fi

  local data sdir new
  data="$(data_dir)"
  ADD_SLUG="$slug"
  trap add_cleanup EXIT
  trap 'exit 130' INT TERM
  [[ -d "${data}/secrets/${slug}" ]] || ADD_NEW_SDIR="${data}/secrets/${slug}"
  new="$(ensure_dirs "$data" bw broker secrets "secrets/${slug}")"
  if printf '%s\n' "$new" | grep -qxF "${data}/secrets"; then
    chmod_own 711 "${data}/secrets"
  fi
  if [[ -e "$file" ]]; then
    # --force: the old file and secrets stay as .bak until the check passes.
    mv -f "$file" "${file}.bak"
    slug_op "$slug" "$SECRET_BACKUP" < /dev/null || die "could not back up secrets/${slug}/"
    ADD_STAGE=backed
  fi
  write_secret "$slug" client_secret "$secret"
  if [[ "$unlock" == stored ]]; then
    write_secret "$slug" master_password "$password"
  else
    slug_op "$slug" "$SECRET_RM" master_password < /dev/null
  fi
  secret=""
  password=""

  local scope_line="BW_SCOPE=${scope}"
  [[ "$scope" == *"#"* ]] && scope_line="BW_SCOPE=\"${scope}\""
  ensure_accounts_dir
  local tmp
  tmp="$(umask 077 && mktemp "${ACCOUNTS_DIR}/.${slug}.XXXXXX")" || die "could not write in ${ACCOUNTS_DIR}"
  ADD_TMP="$tmp"
  {
    printf '# Written by make vault-add. Credentials are in secrets/%s/ under BITWARDEN_HOST_DATA_DIR. See docs/bitwarden.md.\n' "$slug"
    printf 'BW_MODE=%s\n' "$mode"
    printf 'BW_SERVER=%s\n' "$server"
    printf '%s\n' "$scope_line"
    printf 'BW_UNLOCK=%s\n' "$unlock"
    printf 'BW_CLIENTID=%s\n' "$client_id"
  } > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
  ADD_TMP=""
  [[ "$ADD_STAGE" == backed ]] && ADD_STAGE=replaced || ADD_STAGE=written
  log "Wrote ${file#"${ROOT}"/} and secrets/${slug}/"
  local owned
  owned="$(printf '%s\n' "$new" | grep -vxF "${data}/secrets" || true)"
  # shellcheck disable=SC2086  # one folder per line, no spaces in the names we make
  linux_owner $owned "$file"
  if [[ "$JSON" == 1 ]]; then
    emit_line '{"phase":"setup","step":"write","status":"done"}'
    check_account_json "$slug" "$unlock"
    return 0
  fi
  if ! is_true "$(env_get BITWARDEN_ENABLED 0)"; then
    upsert_env "$ENV_PATH" BITWARDEN_ENABLED 1
    log "Set BITWARDEN_ENABLED=1"
  fi

  if [[ "$scope" != all ]]; then
    offer_setup "$slug" "$scope"
  fi
  check_account "$slug" "$unlock"
  ADD_STAGE=finished
  # --force: the old entry goes first, so a value the new account leaves out
  # (region after a move to selfhosted, say) does not stay behind.
  [[ -z "$force" ]] || store_forget "$slug"
  store_record "$slug" "$(store_values "$mode" "$server" "$scope" "$unlock")" || true
}

ADD_SLUG=""
ADD_STAGE=""
ADD_TMP=""
ADD_NEW_SDIR=""
# add --json: 1 once the broker was recreated with the new account file.
ADD_RESTARTED=0

# The old account file and secrets back in place (add --force failed).
restore_backup() {
  local file
  file="$(account_file "$1")"
  if [[ -e "${file}.bak" ]]; then
    mv -f "${file}.bak" "$file"
  else
    rm -f "$file"
  fi
  slug_op "$1" "$SECRET_RESTORE" < /dev/null || log "Could not restore secrets/$1/ from its .bak files"
}

drop_backup() {
  rm -f "$(account_file "$1").bak"
  slug_op "$1" "$SECRET_DROP_BACKUP" < /dev/null || true
}

# EXIT trap of add: no temp file stays, a run stopped before the account file
# was written leaves no new secrets folder, and a --force stopped there puts
# the old files back. With --json, any exit before the check passed (an error,
# SIGTERM) removes the account file, secrets/<slug>/ and bw/<slug>/.
add_cleanup() {
  [[ -z "$ADD_TMP" ]] || rm -f "$ADD_TMP"
  if [[ "$JSON" == 1 ]]; then
    [[ "$ADD_STAGE" == finished ]] || add_rollback "$ADD_SLUG"
    return 0
  fi
  case "$ADD_STAGE" in
    backed) restore_backup "$ADD_SLUG" ;;
    "") [[ -z "$ADD_NEW_SDIR" ]] || remove_secrets "$ADD_SLUG" ;;
  esac
}

# bw/<slug>/ holds the bw CLI's login. bw login --apikey takes an existing
# login as success, so a leftover one would skip the check of the new key.
empty_bw_dir() {
  remove_bw_dir "$1" \
    || emit_error command_failed "could not empty $(data_dir)/bw/$1 (owned by the broker's user); remove it as root, then connect again"
}

# Removes bw/<slug>/ and returns 0 only once it is confirmed gone. On Linux
# bw/ belongs to the containers' user (mode 700), so the host cannot even see
# whether the folder exists; then the running broker, which mounts it at
# /data/bw, removes it. Returns 1 when neither can confirm it.
# shellcheck disable=SC2016  # expanded by the sh that runs it
BW_RM='rm -rf -- "$1" && [ ! -e "$1" ]'
remove_bw_dir() {
  local bw d
  bw="$(data_dir)/bw"
  d="${bw}/$1"
  [[ -e "$bw" || -L "$bw" ]] || return 0
  if [[ -d "$bw" && -x "$bw" ]]; then
    [[ -e "$d" || -L "$d" ]] || return 0
    rm -rf "${d:?}" 2>/dev/null || true
    [[ -e "$d" || -L "$d" ]] || return 0
  fi
  broker_running || return 1
  note_container "$d"
  broker_exec sh -c "$BW_RM" sh "/data/bw/$1" < /dev/null > /dev/null 2>&1
}

# add --json failed or was stopped: the account file, secrets/<slug>/ and
# bw/<slug>/ go, and a broker that loaded the account is recreated without it.
# bw/<slug>/ goes last: until the recreate, the old broker may still write
# its sign-in there.
add_rollback() {
  local slug="$1"
  ADD_STAGE=finished
  [[ -n "$slug" ]] || return 0
  rm -f "$(account_file "$slug")" || true
  remove_secrets "$slug" || true
  if [[ "$ADD_RESTARTED" == 1 ]]; then
    ADD_RESTARTED=0
    restart_broker 2>/dev/null || log "The broker did not restart; it holds ${slug} until make up"
  fi
  remove_bw_dir "$slug" \
    || log "Could not remove $(data_dir)/bw/${slug} (owned by the broker's user); remove it as root"
  return 0
}

# Polls the broker in this shell (so a signal stops it within one poll) until
# SLUG has signed in: unlocked for stored unlock, logged_in or no longer
# locked for manual. Sets W_STATE, W_CLASS (empty for none), W_LOGGED and
# W_RETRY from the last row seen. Returns 1 when VAULT_BROKER_WAIT seconds
# pass first.
W_STATE=""
W_CLASS=""
W_LOGGED=""
W_RETRY=""
wait_for_login() {
  local slug="$1" unlock="$2" wait="${VAULT_BROKER_WAIT:-60}" start rows row
  start="$(date +%s)"
  while :; do
    rows="$(broker_cli accounts 2>/dev/null || true)"
    row="$(printf '%s\n' "$rows" | awk -F '\t' -v s="$slug" '$1 == s { print; exit }')"
    if [[ -n "$row" ]]; then
      IFS=$'\t' read -r _ _ _ W_STATE W_CLASS W_LOGGED W_RETRY <<< "$row"
      [[ "$W_CLASS" != "-" ]] || W_CLASS=""
      if [[ "$W_STATE" != locked ]] || [[ "$unlock" == manual && "$W_LOGGED" == 1 ]]; then
        return 0
      fi
    fi
    [[ $(( $(date +%s) - start )) -lt "$wait" ]] || return 1
    sleep 2
  done
}

# add --json: recreate the broker so it signs in with the new key, and keep
# the account only when it has: unlocked (stored unlock), or locked with
# logged_in=1 and no error (manual). Anything else rolls back and ends with an
# error line.
check_account_json() {
  local slug="$1" unlock="$2" wait="${VAULT_BROKER_WAIT:-60}" timed_out=0
  emit_line '{"phase":"setup","step":"check","status":"running","message":"the broker is signing in"}'
  ADD_RESTARTED=1
  if ! restart_broker; then
    add_rollback "$slug"
    emit_error command_failed "the broker did not restart (docker compose --profile core --profile bitwarden up -d --no-deps bitwarden-broker); nothing was kept"
  fi
  W_STATE=""; W_CLASS=""; W_LOGGED=""; W_RETRY=""
  wait_for_login "$slug" "$unlock" || timed_out=1
  if [[ "$timed_out" == 0 && -z "$W_CLASS" ]]; then
    if [[ "$unlock" == stored && "$W_STATE" == unlocked ]] \
      || [[ "$unlock" == manual && "$W_STATE" == locked && "$W_LOGGED" == 1 ]]; then
      ADD_STAGE=finished
      emit_line '{"phase":"setup","step":"check","status":"done"}'
      LOCKED_MANUAL=""
      note_locked_manual "$slug"
      local msg
      if [[ "$unlock" == stored ]]; then
        msg="${slug} is signed in and unlocked."
      else
        msg="${slug} is signed in and stays locked until you unlock it."
      fi
      [[ -z "$LOCKED_MANUAL" ]] || msg="${msg} $(locked_sentence)"
      emit_done account "$(json_str "$slug")" message "$(json_str "$msg")"
      return 0
    fi
  fi
  add_rollback "$slug"
  case "$W_CLASS" in
    auth) emit_error login_refused "Bitwarden refused the API key or master password for ${slug}; nothing was kept" ;;
    network) emit_error server_unreachable "the broker could not reach the Bitwarden server for ${slug}; nothing was kept" ;;
    rate_limited)
      if [[ "$W_RETRY" =~ $RETRY_RE ]]; then
        emit_error rate_limited "Bitwarden is limiting sign-ins for ${slug}; try again in ${W_RETRY} s. Nothing was kept" "$W_RETRY"
      fi
      emit_error rate_limited "Bitwarden is limiting sign-ins for ${slug}; try again later. Nothing was kept"
      ;;
  esac
  if [[ "$timed_out" == 1 ]]; then
    emit_error timeout "the broker did not finish signing in to ${slug} within ${wait} s; nothing was kept"
  fi
  emit_error command_failed "the broker reports ${slug} as ${W_STATE:-not loaded}${W_CLASS:+ (${W_CLASS})}; nothing was kept"
}

# Creating the folder or collection and moving items into it need your own
# master password, so they run in bitwarden-setup (spec: Setup and seeding).
offer_setup() {
  local slug="$1" scope="$2" extra=""
  [[ "$scope" == collection:* ]] && extra=" --agent-email AGENT_EMAIL [--org ORG_ID]"
  if [[ ! -t 0 || ! -t 1 ]]; then
    log "If ${scope} does not exist yet, create it and choose its items with bitwarden-setup:"
    log "  docker compose run --rm bitwarden-setup create --slug ${slug} --email YOUR_EMAIL${extra}"
    log "  docker compose run --rm bitwarden-setup seed --slug ${slug} --email YOUR_EMAIL"
    return 0
  fi
  local answer email agent="" org=""
  printf 'Create %s if it does not exist yet, and choose the items the agent can use, with bitwarden-setup? It asks for your own master password. [y/N] ' "$scope"
  IFS= read -r answer || answer=""
  case "$(lower "$answer")" in y | yes) ;; *) return 0 ;; esac
  printf 'Your Bitwarden email: '
  IFS= read -r email
  valid_email "$email" || die "not an email address: ${email}"
  local args=(create --slug "$slug" --email "$email")
  if [[ "$scope" == collection:* ]]; then
    printf "The agent member account's email: "
    IFS= read -r agent
    valid_email "$agent" || die "not an email address: ${agent}"
    printf 'Organization id (only when the collection does not exist yet; Enter to skip): '
    IFS= read -r org
    args+=(--agent-email "$agent")
    [[ -z "$org" ]] || args+=(--org "$org")
  fi
  setup_run "${args[@]}" || die "bitwarden-setup create failed; the account file is kept, run it again when ready"
  setup_run seed --slug "$slug" --email "$email" || log "bitwarden-setup seed stopped; move items into the agent's folder or collection in a Bitwarden app"
}

# With the broker up, restart it so it loads the new file, and report what it
# makes of the credentials. An auth error removes what add wrote.
check_account() {
  local slug="$1" unlock="$2" got state class
  if ! broker_running; then
    log "The broker checks the API key when it starts: make up, then make doctor"
    if [[ "$ADD_STAGE" == replaced ]]; then
      log "The previous file is kept as ${slug}.env.bak (and secrets/${slug}/*.bak) until then; make vault-add FORCE=1 again replaces them"
      ADD_STAGE=finished
    fi
    return 0
  fi
  log "Restarting the broker so it loads ${slug}"
  restart_broker
  got="$(wait_for_account "$slug" "$unlock")"
  note_locked_manual "$slug"
  state="${got%% *}"
  class="${got#* }"
  case "$state" in
    unlocked) log "${slug}: signed in and unlocked" ;;
    locked)
      if [[ "$unlock" == manual ]]; then
        log "${slug}: added; it stays locked until make vault-unlock SLUG=${slug}"
      else
        log "${slug}: still locked; make doctor shows whether the unlock finished"
      fi
      ;;
    error)
      if [[ "$class" == auth ]]; then
        if [[ "$ADD_STAGE" == replaced ]]; then
          restore_backup "$slug"
          ADD_STAGE=finished
          restart_broker
          die "Bitwarden refused the API key or master password for ${slug} (auth); the previous account file and secrets are back"
        fi
        rm -f "$(account_file "$slug")"
        remove_secrets "$slug"
        ADD_STAGE=finished
        restart_broker
        die "Bitwarden refused the API key or master password for ${slug} (auth); nothing was kept"
      fi
      log "${slug}: the broker reports an error (${class}); make doctor"
      ;;
    offline) log "${slug}: the server is not reachable from the broker (${class}); make doctor" ;;
    *) log "${slug}: the broker has not loaded it; make doctor shows why" ;;
  esac
  if [[ "$ADD_STAGE" == replaced ]]; then
    drop_backup "$slug"
    ADD_STAGE=written
  fi
}

# After a broker restart: the manual accounts it locked, except SKIP. Each is
# logged and added to LOCKED_MANUAL (space-separated). In --json mode a file
# that cannot be read is skipped instead of stopping the run.
LOCKED_MANUAL=""
# The sentence for a done message that names LOCKED_MANUAL.
locked_sentence() {
  local names="${LOCKED_MANUAL# }"
  printf 'The broker restart locked %s; unlock them again.' "${names// /, }"
}
note_locked_manual() {
  local skip="$1" rows s st f
  rows="$(broker_cli accounts 2>/dev/null || true)"
  while IFS=$'\t' read -r s _ _ st _; do
    [[ -n "$s" && "$s" != "$skip" && "$st" == locked ]] || continue
    f="$(account_file "$s")"
    [[ -f "$f" ]] || continue
    if [[ "$JSON" == 1 ]]; then
      FILE_TEXT="$(JSON_DIE=0; load_file "$f" 2>/dev/null && printf '%s' "$FILE_TEXT")" || continue
      LOADED=""
    else
      load_file "$f"
    fi
    if [[ "$(text_get BW_UNLOCK)" == manual ]]; then
      log "${s} is locked after the restart: make vault-unlock SLUG=${s}"
      LOCKED_MANUAL="${LOCKED_MANUAL} ${s}"
    fi
  done <<< "$rows"
  return 0
}

# --- unlock, lock, sync, requests, grant -----------------------------------

need_manual() {
  valid_slug "$1" || emit_error bad_slug "--slug must use lowercase letters, digits and hyphens"
  [[ -f "$(account_file "$1")" ]] \
    || emit_error not_found "no account '$1' in ${ACCOUNTS_DIR#"${ROOT}"/} — make vault-add or make vault-init"
  load_file "$(account_file "$1")"
  [[ "$(text_get BW_UNLOCK)" == manual ]] \
    || emit_error bad_input "$1 uses stored unlock; the broker unlocks it by itself"
}

# require_broker for --json: the broker_down error line.
require_broker_json() {
  broker_running || emit_error broker_down "the Bitwarden broker is not running"
}

# unlock --json reads {"master_password"} from stdin and makes one attempt.
cmd_unlock() {
  local slug="" json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) need_value "$@"; slug="$2"; shift 2 ;;
      --json) json=1; shift ;;
      *) die "unknown option for unlock: $1" ;;
    esac
  done
  [[ "$json" == 1 ]] && JSON=1
  need_manual "$slug"
  if [[ "$JSON" == 1 ]]; then
    local in_master_password=""
    read_input master_password
    [[ -n "$in_master_password" ]] || emit_error bad_input "master_password is empty"
    require_broker_json
    local jrc=0
    printf '%s\n' "$in_master_password" | broker_exec python3 -m broker.unlock "$slug" >/dev/null 2>&1 || jrc=$?
    in_master_password=""
    case "$jrc" in
      0) emit_done; return 0 ;;
      2) emit_error login_refused "the master password was refused" ;;
      3) emit_error not_found "the broker has no manual account ${slug}; it reads account files when it starts (make up)" ;;
      *) emit_error command_failed "the broker could not unlock ${slug}; make doctor" ;;
    esac
  fi
  require_broker
  local tries=1 rc password
  [[ -t 0 ]] && tries=3
  for _ in $(seq 1 "$tries"); do
    ask_hidden "Master password for ${slug}: "
    password="$REPLY_SECRET"
    REPLY_SECRET=""
    [[ -n "$password" ]] || die "no password entered"
    rc=0
    printf '%s\n' "$password" | broker_exec python3 -m broker.unlock "$slug" >/dev/null 2>&1 || rc=$?
    password=""
    case "$rc" in
      0) log "${slug} is unlocked until the broker restarts or make vault-lock SLUG=${slug}"; return 0 ;;
      2) printf '[vault] Wrong master password.\n' >&2 ;;
      3) die "the broker has no manual account ${slug}; it reads account files when it starts (make up)" ;;
      *) die "the broker could not unlock ${slug}; make doctor" ;;
    esac
  done
  die "${slug} is still locked"
}

cmd_lock() {
  local slug="" json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) need_value "$@"; slug="$2"; shift 2 ;;
      --json) json=1; shift ;;
      *) die "unknown option for lock: $1" ;;
    esac
  done
  [[ "$json" == 1 ]] && JSON=1
  need_manual "$slug"
  if [[ "$JSON" == 1 ]]; then
    require_broker_json
  else
    require_broker
  fi
  local rc=0
  broker_exec python3 -m broker.unlock --lock "$slug" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) if [[ "$JSON" == 1 ]]; then emit_done; else log "${slug} is locked"; fi ;;
    3) emit_error not_found "the broker has no manual account ${slug}" ;;
    *) emit_error command_failed "the broker could not lock ${slug}; make doctor" ;;
  esac
}

# sync --json: the broker's reply on one line. With the broker down it exits
# 1 with stderr only, as requests --json does.
cmd_sync() {
  if [[ $# -eq 1 && "$1" == --json ]]; then
    broker_running || { printf '[vault] ERROR: the Bitwarden broker is not running\n' >&2; exit 1; }
    broker_cli sync --json
    return
  fi
  [[ $# -eq 0 ]] || die "sync takes no options"
  require_broker
  broker_cli sync
}

# status --json: one line per account file, sorted by slug, for Boundary.
cmd_status() {
  local json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json=1; shift ;;
      *) usage >&2; exit 1 ;;
    esac
  done
  [[ "$json" == 1 ]] || { usage >&2; exit 1; }
  JSON=1
  local rows="" up=0
  if broker_running && rows="$(broker_cli accounts 2>/dev/null)"; then
    up=1
  fi
  local refused=" " a b
  while IFS=$'\t' read -r a b _; do
    [[ "$a" == refused ]] && refused="${refused}${b%%:*} "
  done <<< "$rows"
  local f name slug row mode scope unlock st ec retry err
  for f in "$ACCOUNTS_DIR"/*.env; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f")"
    slug="${name%.env}"
    if [[ "$up" == 1 && "$refused" == *" ${name} "* ]]; then
      if [[ "$slug" =~ $SLUG_RE ]]; then
        slug="$(json_str "$slug")"
      else
        slug=null
      fi
      emit_line "{\"file\":$(json_str "$name"),\"slug\":${slug},\"state\":\"refused\",\"error\":\"bad_file\"}"
      continue
    fi
    # A file that cannot be read is reported like a refused one; load_file dies, so it runs in a subshell.
    if ! FILE_TEXT="$(load_file "$f" && printf '%s' "$FILE_TEXT")"; then
      if [[ "$slug" =~ $SLUG_RE ]]; then
        slug="$(json_str "$slug")"
      else
        slug=null
      fi
      emit_line "{\"file\":$(json_str "$name"),\"slug\":${slug},\"state\":\"refused\",\"error\":\"bad_file\"}"
      continue
    fi
    mode="$(text_get BW_MODE)"
    scope="$(text_get BW_SCOPE)"
    [[ -n "$scope" ]] || scope="folder:Agent"
    unlock="$(text_get BW_UNLOCK)"
    row=""
    [[ "$up" == 1 ]] && row="$(printf '%s\n' "$rows" | awk -F '\t' -v s="$slug" '$1 == s && $1 != "refused" { print; exit }')"
    if [[ -z "$row" ]]; then
      st=offline; retry=null
      if [[ "$up" == 1 ]]; then err='"not_loaded"'; else err='"broker_down"'; fi
    else
      IFS=$'\t' read -r _ _ _ st ec _ retry <<< "$row"
      [[ "$ec" == "-" ]] && ec=""
      case "$ec" in
        auth) err='"login_refused"' ;;
        network) err='"server_unreachable"' ;;
        rate_limited) err='"rate_limited"' ;;
        unknown) err='"unknown"' ;;
        *) err=null ;;
      esac
      if [[ "$retry" =~ $RETRY_RE ]]; then :; else retry=null; fi
    fi
    emit_line "{\"slug\":$(json_str "$slug"),\"mode\":$(json_str "$mode"),\"scope\":$(json_str "$scope"),\"unlock\":$(json_str "$unlock"),\"state\":$(json_str "$st"),\"error\":${err},\"retry_after\":${retry}}"
  done
  return 0
}

# --json is for Boundary: the broker's JSON on stdout and nothing else.
cmd_requests() {
  local form=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) form="--json"; shift ;;
      *) die "unknown option for requests: $1" ;;
    esac
  done
  if [[ -n "$form" ]]; then
    broker_running || { printf '[vault] ERROR: the Bitwarden broker is not running\n' >&2; exit 1; }
    broker_cli requests --json
  else
    require_broker
    broker_cli requests
  fi
}

# A value from local/state, which local-init writes; empty when there is none.
# local/ belongs to the containers' user on Linux, so it may come through
# bitwarden-setup. Call it in this shell, then read STATE_VALUE.
STATE_VALUE=""
state_get() {
  load_file "$(data_dir)/local/state" optional
  STATE_VALUE="$(text_get "$1")"
}

# --- remove, open -----------------------------------------------------------

REVOKE_REMINDER="The API key still exists in Bitwarden; revoke it there (Settings > Security > Keys) if you are done with it"

# remove --json: the same removal, ending with a done line whose message is
# the revoke reminder.
cmd_remove() {
  local slug="" a
  for a in "$@"; do
    if [[ "$a" == --json ]]; then
      JSON=1
      JSON_DIE=1
    fi
  done
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) shift ;;
      --slug) need_value "$@"; slug="$2"; shift 2 ;;
      *) emit_error bad_input "unknown option for remove: $1" ;;
    esac
  done
  if [[ "$JSON" == 1 ]]; then
    valid_slug "$slug" || emit_error bad_slug "--slug must use lowercase letters, digits and hyphens"
  else
    need_slug "$slug"
  fi
  [[ "$slug" != local ]] \
    || emit_error bad_input "the local account is part of make vault-init; BITWARDEN_LOCAL_ENABLED=0 and make up turn local mode off"
  # --json with no account file: an earlier remove got as far as deleting it (its
  # broker restart failed, say), so this one removes what is left and ends with
  # done, which lets addon.sh disconnect drop the store entry.
  local data had_file=1
  data="$(data_dir)"
  if [[ ! -f "$(account_file "$slug")" ]]; then
    [[ "$JSON" == 1 ]] || need_account "$slug"
    had_file=0
  fi
  rm -f "$(account_file "$slug")"
  remove_secrets "$slug"
  if [[ "$had_file" == 1 ]]; then
    log "Removed ${slug}: its account file and secrets"
  else
    log "${slug} has no account file; removing its secrets and bw/${slug} if they are left"
  fi
  if broker_running && { [[ "$had_file" == 1 ]] || broker_may_hold "$slug"; }; then
    if [[ "$JSON" == 1 ]]; then
      if ! restart_broker; then
        remove_bw_dir "$slug" \
          || log "Could not remove ${data}/bw/${slug} (owned by the broker's user); remove it as root"
        emit_error command_failed "removed the files of ${slug}, but the broker did not restart; it holds ${slug} until make up"
      fi
    else
      restart_broker
    fi
    log "Restarted the broker, which no longer holds ${slug}"
    note_locked_manual "$slug"
  fi
  # After the restart, so the broker no longer writes there.
  remove_bw_dir "$slug" \
    || log "Could not remove ${data}/bw/${slug} (owned by the broker's user); remove it as root"
  store_forget "$slug"
  log "$REVOKE_REMINDER"
  if [[ "$JSON" == 1 ]]; then
    local msg="${REVOKE_REMINDER}."
    [[ "$had_file" == 1 ]] || msg="${slug} had no account file; removed its secrets and bw/${slug} if they were left. ${msg}"
    [[ -z "$LOCKED_MANUAL" ]] || msg="${msg} $(locked_sentence)"
    emit_done account "$(json_str "$slug")" message "$(json_str "$msg")"
  fi
}

local_url() { printf 'http://localhost:%s' "$(env_get VAULTWARDEN_PORT 8222)"; }

cmd_open() {
  [[ $# -eq 0 ]] || die "open takes no options"
  is_true "$(env_get BITWARDEN_LOCAL_ENABLED 0)" \
    || die "local mode is off (BITWARDEN_LOCAL_ENABLED=0); make vault-init sets it up"
  local url
  url="$(local_url)"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1
  else
    printf '%s\n' "$url"
  fi
}

# Run by make up before docker compose up. Silent while BITWARDEN_ENABLED is off.
cmd_pre_up() {
  is_true "$(env_get BITWARDEN_ENABLED 0)" || return 0
  ensure_accounts_dir
}

# Run after hermes is recreated or restarted (scripts/lib/bitwarden.sh). The
# broker shares hermes' network namespace; up recreates it when the namespace
# it joined belongs to a removed container, and leaves it alone otherwise.
# Silent while BITWARDEN_ENABLED is off or the broker container does not exist.
cmd_follow_hermes() {
  is_true "$(env_get BITWARDEN_ENABLED 0)" || return 0
  command -v docker >/dev/null 2>&1 || return 0
  compose_with_profile bitwarden ps -a -q bitwarden-broker 2>/dev/null | grep -q . || return 0
  compose_with_profile bitwarden up -d --no-deps bitwarden-broker >/dev/null \
    || die "bitwarden-broker did not start — docker compose --profile core --profile bitwarden up -d --no-deps bitwarden-broker"
  if ! wait_for_broker; then
    log "The Bitwarden broker does not answer yet; make doctor"
    return 0
  fi
  note_locked_manual ""
}

# The bootstraps leave $PLUGINS_MARKER in a profile's home when they change its
# add-on login backends (bootstrap-api-profile.sh register_addon_vault_backends).
# Turning the add-on on or off does not recreate hermes, so the running gateway
# is asked to reload its plugins for each marked profile, through Hermes' own
# reload-plugins control verb. Tools reach new sessions; open ones keep theirs.
# A marker stays until a reload succeeds. With hermes stopped there is nothing
# to reload: it loads the config when it starts, and the marker is cleared later.
cmd_reload_plugins() {
  local m found=""
  for m in "$HERMES_DATA_DIR/$PLUGINS_MARKER" "$HERMES_DATA_DIR"/profiles/*/"$PLUGINS_MARKER"; do
    [[ -e "$m" ]] && found=1
  done
  [[ -n "$found" ]] || return 0
  command -v docker >/dev/null 2>&1 || return 0
  docker compose ps --status running -q hermes 2>/dev/null | grep -q . || return 0
  local result
  if ! result="$(docker compose exec -T hermes /opt/hermes/.venv/bin/python - "$PLUGINS_MARKER" <<'PY' 2>&1
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from gateway.control_socket import reload_gateway_plugins

marker = sys.argv[1]
root = Path(os.environ.get("HERMES_HOME") or "/opt/data")
homes = [root] + sorted(p for p in (root / "profiles").glob("*") if p.is_dir())
for home in homes:
    flag = home / marker
    if not flag.exists():
        continue
    name = "default" if home == root else home.name
    answer = reload_gateway_plugins(home)
    if answer is None and home != root:  # a profile the multiplexer serves
        answer = reload_gateway_plugins(root, profile_home=home)
    if answer and answer.get("reloaded"):
        flag.unlink()
        print(f"ok\t{name}")
    else:
        print(f"fail\t{name}\t{(answer or {}).get('error') or 'no gateway answered'}")
PY
)"; then
    log "Could not ask the gateway to reload its plugins; make restart applies the add-on change"
    return 0
  fi
  local status name reason
  while IFS=$'\t' read -r status name reason; do
    case "$status" in
      ok) log "Reloaded plugins for the ${name} profile" ;;
      fail) log "The gateway did not reload plugins for ${name} (${reason}); make restart applies the add-on change" ;;
    esac
  done <<< "$result"
}

# Run by make up after docker compose up. The Bitwarden steps are silent while
# BITWARDEN_ENABLED is off. Each account file the add-on store lacks, other
# than local, is recorded there (accounts added before the store kept them).
cmd_up_hook() {
  cmd_reload_plugins
  is_true "$(env_get BITWARDEN_ENABLED 0)" || return 0
  local f slug
  for f in "$ACCOUNTS_DIR"/*.env; do
    [[ -f "$f" ]] || continue
    slug="$(basename "$f" .env)"
    load_file "$f"
    store_backfill "$slug"
  done
  return 0
}

# --- init -------------------------------------------------------------------

cmd_init() {
  local owner="" wait="$INIT_WAIT_SECONDS"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --owner) need_value "$@"; owner="$2"; shift 2 ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  local data new
  data="$(data_dir)"
  # Step 1 of the order: every bind-mount source exists before any container runs.
  ensure_accounts_dir
  new="$(ensure_dirs "$data" bw broker secrets secrets/local secrets/local/tls \
    local local/tls local/tls/server local/vaultwarden)"
  ensure_dirs "$data" vaultwarden >/dev/null
  # Only folders this run made: on Linux the rest may belong to the containers.
  if printf '%s\n' "$new" | grep -qxF "${data}/secrets"; then
    chmod_own 711 "${data}/secrets"
  fi
  local owned
  owned="$(printf '%s\n' "$new" | grep -vxF "${data}/secrets" || true)"
  # shellcheck disable=SC2086  # one folder per line, no spaces in the names we make
  [[ -z "$owned" ]] || linux_owner $owned

  local step recorded
  state_get STEP
  step="$STATE_VALUE"
  state_get OWNER_EMAIL
  recorded="$(lower "$STATE_VALUE")"
  if ! is_true "$(env_get BITWARDEN_LOCAL_ENABLED 0)"; then
    upsert_env "$ENV_PATH" BITWARDEN_LOCAL_ENABLED 1
    log "Set BITWARDEN_LOCAL_ENABLED=1"
  fi
  if [[ "$step" == role ]]; then
    log "Local mode is already set up for ${recorded:-its owner}."
    finish_init
    return 0
  fi

  [[ -n "$owner" ]] || owner="${VAULT_OWNER_EMAIL:-$(env_get VAULT_OWNER_EMAIL)}"
  [[ -n "$owner" ]] || owner="$recorded"
  if [[ -z "$owner" ]]; then
    [[ -t 0 ]] || die "pass --owner or set VAULT_OWNER_EMAIL: the address you will sign in to the web vault with"
    printf 'Your email address for the web vault (it becomes the organization Owner): '
    IFS= read -r owner
  fi
  owner="$(lower "$owner")"
  valid_email "$owner" || die "not an email address: ${owner}"
  if [[ -n "$recorded" && "$owner" != "$recorded" ]]; then
    die "make vault-init already invited ${recorded}; run it with VAULT_OWNER_EMAIL=${recorded}"
  fi
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"

  local url
  url="$(local_url)"
  if [[ "$step" != confirmed ]]; then
    setup_run tls || die "bitwarden-setup tls failed"
    setup_run admin-token || die "bitwarden-setup admin-token failed"
  fi
  log "Starting the local Vaultwarden"
  docker compose --profile vault up -d --wait vaultwarden vaultwarden-tls \
    || die "Vaultwarden did not become healthy — docker compose logs vaultwarden vaultwarden-tls"

  if [[ "$step" != confirmed ]]; then
    local rc=0
    setup_run local-init --owner "$owner" --signup-url "$url" --wait "$wait" || rc=$?
    case "$rc" in
      0) ;;
      4) die "Register ${owner} at ${url}, then run make vault-init again; it picks up from here" ;;
      *) die "bitwarden-setup local-init stopped (see above); make vault-init again picks up from the last finished step" ;;
    esac
  fi

  log "Last step: you sign in once so the agent account becomes a plain User with access to the Agent collection only"
  setup_run role --slug local --email "$owner" \
    || die "bitwarden-setup role failed; make vault-init again retries this step"
  log "Local mode is set up."
  finish_init
}

finish_init() {
  if ! is_true "$(env_get BITWARDEN_ENABLED 0)"; then
    upsert_env "$ENV_PATH" BITWARDEN_ENABLED 1
    log "Set BITWARDEN_ENABLED=1"
  fi
  log "The local Vaultwarden holds what you store in local mode, and nothing else has a copy unless you back it up: scripts/vault.sh backup DIR."
  log "Web vault: $(local_url) (make vault-open). Next: make up"
}

# --- backup and restore -------------------------------------------------------

vaultwarden_cid() {
  command -v docker >/dev/null 2>&1 || return 0
  docker compose --profile vault ps --status running -q vaultwarden 2>/dev/null | head -1
}

# The sqlite3 online backup of db.sqlite3 into DEST/db.sqlite3.
backup_db() {
  local vw="$1" dest="$2" cid image
  cid="$(vaultwarden_cid)"
  image="$(env_get IMAGE_PREFIX assistant)-bitwarden-broker:local"
  local script='import os, sqlite3
src = sqlite3.connect("file:/data/db.sqlite3?mode=ro", uri=True)
dst = sqlite3.connect("/backup/db.sqlite3")
src.backup(dst)
dst.close(); src.close()
os.chmod("/backup/db.sqlite3", 0o600)
os.chown("/backup/db.sqlite3", int(os.environ["OWNER_UID"]), int(os.environ["OWNER_GID"]))'
  if [[ -n "$cid" ]]; then
    # In a container that shares Vaultwarden's mounts, so sqlite's locks are
    # the ones Vaultwarden takes.
    docker run --rm --network none --user 0:0 --volumes-from "$cid" -v "${dest}:/backup" \
      -e OWNER_UID="$(id -u)" -e OWNER_GID="$(id -g)" --entrypoint python3 "$image" -c "$script"
  elif command -v sqlite3 >/dev/null 2>&1; then
    (cd "$dest" && sqlite3 "${vw}/db.sqlite3" ".backup db.sqlite3")
    chmod 600 "${dest}/db.sqlite3"
  else
    docker run --rm --network none --user 0:0 -v "${vw}:/data" -v "${dest}:/backup" \
      -e OWNER_UID="$(id -u)" -e OWNER_GID="$(id -g)" --entrypoint python3 "$image" -c "$script"
  fi
}

cmd_backup() {
  local dir="${1:-}"
  [[ -n "$dir" && $# -eq 1 ]] || die "usage: scripts/vault.sh backup DIR"
  if [[ -d "$dir" && -n "$(ls -A "$dir" 2>/dev/null)" ]]; then
    die "${dir} is not empty; back up into a new folder"
  fi
  (umask 077 && mkdir -p "$dir")
  dir="$(cd "$dir" && pwd -P)"
  local data vw f
  data="$(data_dir)"
  vw="${data}/vaultwarden"
  mkdir -p "${dir}/vaultwarden"
  if [[ -f "${vw}/db.sqlite3" ]]; then
    backup_db "$vw" "${dir}/vaultwarden" || die "the database backup failed"
    for f in "${vw}"/rsa_key* "${vw}/config.json"; do
      [[ -e "$f" ]] && cp -p "$f" "${dir}/vaultwarden/"
    done
    for f in attachments sends; do
      [[ -d "${vw}/${f}" ]] && cp -Rp "${vw}/${f}" "${dir}/vaultwarden/"
    done
  else
    log "No local Vaultwarden database; copying the secrets and account files only"
  fi
  for f in secrets local; do
    [[ -d "${data}/${f}" ]] && cp -Rp "${data}/${f}" "${dir}/"
  done
  [[ -d "$ACCOUNTS_DIR" ]] && cp -Rp "$ACCOUNTS_DIR" "${dir}/accounts"
  log "Backed up to ${dir}. It holds master passwords and API keys; keep it as safe as the vault."
}

cmd_restore() {
  local dir="" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force) force=1; shift ;;
      -*) die "unknown option for restore: $1" ;;
      *) [[ -z "$dir" ]] || die "usage: scripts/vault.sh restore DIR [--force]"; dir="$1"; shift ;;
    esac
  done
  [[ -n "$dir" ]] || die "usage: scripts/vault.sh restore DIR [--force]"
  [[ -d "${dir}/vaultwarden" || -d "${dir}/secrets" ]] || die "${dir} is not a scripts/vault.sh backup"
  [[ -z "$(vaultwarden_cid)" ]] \
    || die "stop Vaultwarden first: docker compose stop vaultwarden vaultwarden-tls"
  local data vw existing="" f
  data="$(data_dir)"
  vw="${data}/vaultwarden"
  [[ -e "${vw}/db.sqlite3" ]] && existing="${existing} vaultwarden/db.sqlite3"
  [[ -n "$(ls -A "${data}/secrets" 2>/dev/null)" ]] && existing="${existing} secrets/"
  [[ -n "$(ls -A "${data}/local" 2>/dev/null)" ]] && existing="${existing} local/"
  [[ -n "$(ls -A "$ACCOUNTS_DIR" 2>/dev/null)" ]] && existing="${existing} accounts/"
  if [[ -n "$existing" && -z "$force" ]]; then
    die "restoring would overwrite${existing}; pass --force to replace them"
  fi
  mkdir -p "$vw" "$ACCOUNTS_DIR"
  if [[ -f "${dir}/vaultwarden/db.sqlite3" ]]; then
    rm -f "${vw}/db.sqlite3" "${vw}/db.sqlite3-wal" "${vw}/db.sqlite3-shm"
    for f in "${dir}"/vaultwarden/*; do
      [[ -e "$f" ]] || continue
      rm -rf "${vw:?}/$(basename "$f")"
      cp -Rp "$f" "${vw}/"
    done
  fi
  for f in secrets local; do
    if [[ -d "${dir}/${f}" ]]; then
      (umask 077 && mkdir -p "${data}/${f}")
      cp -Rp "${dir}/${f}/." "${data}/${f}/"
    fi
  done
  [[ -d "${dir}/accounts" ]] && cp -Rp "${dir}/accounts/." "${ACCOUNTS_DIR}/"
  log "Restored from ${dir}. Next: make up"
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  add) shift; cmd_add "$@" ;;
  unlock) shift; cmd_unlock "$@" ;;
  lock) shift; cmd_lock "$@" ;;
  sync) shift; cmd_sync "$@" ;;
  status) shift; cmd_status "$@" ;;
  requests) shift; cmd_requests "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  open) shift; cmd_open "$@" ;;
  up-hook) cmd_up_hook ;;
  pre-up) cmd_pre_up ;;
  follow-hermes) cmd_follow_hermes ;;
  reload-plugins) cmd_reload_plugins ;;
  backup) shift; cmd_backup "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
