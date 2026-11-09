#!/usr/bin/env bash
# Bitwarden add-on: the accounts the broker signs in to, local mode, access
# requests and stack secrets. See docs/bitwarden.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

ENV_PATH="${ENV_FILE:-.env}"
ACCOUNTS_DIR="${VAULT_ACCOUNTS_DIR:-${ROOT}/compose/bitwarden/accounts}"
# The containers' user (compose/bitwarden-broker/Dockerfile).
CONTAINER_UID=10001
INIT_WAIT_SECONDS=600
# Regexes live in variables: bash 3.2 parses some unquoted ones in [[ =~ ]] differently.
SLUG_RE='^[a-z0-9-]+$'
EMAIL_RE='^[^@[:space:]]+@[^@[:space:]]+$'
URL_RE='^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^[:space:]?#]*)?$'
ID_RE='^[A-Za-z0-9][A-Za-z0-9-]{0,63}$'
NO_SPACE_RE='^[^[:space:]]+$'
NO_CNTRL_RE='^[^[:cntrl:]]+$'
NUMBER_RE='^[0-9]+$'

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
        --scope all also needs SCOPE=all in the environment.
  unlock --slug S   Unlock a manual account in the broker (hidden prompt)
  lock --slug S     Lock a manual account
  sync              Re-read every unlocked account's items now
  requests [--json] The agent's pending access requests
  grant [--site H] [--slug S] [--email E] [--item ID]
        Move an item for a requested site into the agent's scope, with your
        own unlock in bitwarden-setup. Without --site, pick a pending request.
  remove --slug S   Forget an account: its file, secrets and bw data
  open              Open the local web vault
  secrets-sync      Write stack secrets from the vault into the stack's files
  backup DIR        Copy the local Vaultwarden, secrets and account files to DIR
  restore DIR [--force]
                    Put a backup back; refuses to overwrite data without --force

After add or remove: make up. make doctor shows each account's state.
EOF
}

log() { printf '[vault] %s\n' "$1"; }
die() { printf '[vault] ERROR: %s\n' "$1" >&2; exit 1; }

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

# One value from a KEY=value file, without sourcing it: CR, surrounding
# spaces and one pair of quotes dropped, as the broker's parser does.
file_get() {
  local v
  v="$(grep -E "^[[:space:]]*${2}[[:space:]]*=" "$1" 2>/dev/null | head -1 | cut -d= -f2- || true)"
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

# --- Docker ---------------------------------------------------------------

broker_running() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose --profile bitwarden ps --status running -q bitwarden-broker 2>/dev/null | grep -q .
}

require_broker() {
  broker_running || die "the Bitwarden broker is not running — set BITWARDEN_ENABLED=1, then make up"
}

broker_exec() { docker compose --profile bitwarden exec -T bitwarden-broker "$@"; }
broker_cli() { broker_exec python3 -m broker.cli "$@"; }

# bitwarden-setup takes the terminal for its hidden prompts (tty: true in compose).
setup_run() { docker compose --profile bitwarden-tools run --rm bitwarden-setup "$@"; }

# "<state> <error_class>" for a slug from the broker, or nothing.
broker_state() {
  local rows="$1" slug="$2"
  printf '%s\n' "$rows" | awk -F '\t' -v s="$slug" '$1 == s { print $4 " " $5; exit }'
}

restart_broker() {
  docker compose --profile bitwarden restart bitwarden-broker >/dev/null
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

# Writes VALUE to PATH, mode 600, through a temp file in the same folder.
# printf is a builtin, so the value never appears in any process's argv.
write_private() {
  local path="$1" value="$2" tmp
  tmp="$(umask 077 && mktemp "$(dirname "$path")/.$(basename "$path").XXXXXX")"
  printf '%s\n' "$value" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$path"
}

# On Linux the containers run as uid 10001 and must own their folders. Run as
# root, chown them; otherwise print the command.
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
# create them root-owned. Prints the folders it created.
ensure_dirs() {
  local data="$1" d
  shift
  for d in "$@"; do
    if [[ -e "${data}/${d}" && ! -d "${data}/${d}" ]]; then
      die "${data}/${d} exists and is not a folder"
    fi
    if [[ ! -d "${data}/${d}" ]]; then
      (umask 077 && mkdir -p "${data}/${d}")
      printf '%s\n' "${data}/${d}"
    fi
  done
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

cmd_add() {
  local slug="" mode="" region="" url="" scope="folder:Agent" unlock="stored" force=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      --mode) mode="${2:-}"; shift 2 ;;
      --region) region="${2:-}"; shift 2 ;;
      --url) url="${2:-}"; shift 2 ;;
      --scope) scope="${2:-}"; shift 2 ;;
      --unlock) unlock="${2:-}"; shift 2 ;;
      --force) force=1; shift ;;
      *) die "unknown option for add: $1" ;;
    esac
  done
  need_slug "$slug"
  [[ "$slug" != local ]] || die "the slug local belongs to make vault-init"
  local server
  case "$mode" in
    cloud)
      [[ -z "$url" ]] || die "--url is for --mode selfhosted; cloud takes --region us or eu"
      case "$(lower "$region")" in
        "" | us) server="https://vault.bitwarden.com" ;;
        eu) server="https://vault.bitwarden.eu" ;;
        *) die "--region must be us or eu" ;;
      esac
      ;;
    selfhosted)
      [[ -z "$region" ]] || die "--region is for --mode cloud"
      while [[ "$url" == */ ]]; do url="${url%/}"; done
      [[ "$url" =~ $URL_RE ]] \
        || die "--url must be an https:// URL with no user, query or fragment"
      server="$url"
      ;;
    local) die "local accounts come from make vault-init" ;;
    *) die "--mode must be cloud or selfhosted" ;;
  esac
  case "$scope" in
    all)
      [[ "${SCOPE:-}" == all ]] \
        || die "--scope all lets the agent reach every item in the vault; pass SCOPE=all to make vault-add (or set it in the environment) to confirm"
      ;;
    folder:*)
      local name="${scope#folder:}"
      [[ -n "$name" && "$name" != " "* && "$name" != *" " ]] || die "--scope folder:NAME needs a name without spaces at either end"
      [[ "$name" != *\"* && "$name" =~ $NO_CNTRL_RE ]] || die "the folder name must not contain \" or control characters"
      ;;
    collection:*)
      [[ "${scope#collection:}" =~ $ID_RE ]] || die "--scope collection:ID needs a Bitwarden collection id"
      ;;
    *) die "--scope must be folder:<name>, collection:<id> or all" ;;
  esac
  case "$unlock" in
    stored | manual) ;;
    *) die "--unlock must be stored or manual" ;;
  esac
  local file
  file="$(account_file "$slug")"
  if [[ -e "$file" && -z "$force" ]]; then
    die "${file#"${ROOT}"/} exists; pass --force (make vault-add FORCE=1) to replace it"
  fi

  local client_id secret="" password=""
  log "The agent signs in with this account's personal API key (web vault: Settings > Security > Keys > API key)."
  ask_hidden "client_id: "
  client_id="$REPLY_SECRET"
  [[ "$client_id" =~ $NO_SPACE_RE ]] || die "the client_id must not be empty or contain spaces"
  ask_hidden "client_secret: "
  secret="$REPLY_SECRET"
  [[ -n "$secret" ]] || die "the client_secret must not be empty"
  if [[ "$unlock" == stored ]]; then
    ask_hidden "Master password (stored for the broker): "
    password="$REPLY_SECRET"
    [[ -n "$password" ]] || die "the master password must not be empty with --unlock stored"
  fi
  REPLY_SECRET=""
  if [[ "$unlock" == stored && "$scope" != collection:* ]]; then
    printf '[vault] Warning: %s\n' "$UNLOCK_WARNING" >&2
  fi

  local data sdir new
  data="$(data_dir)"
  new="$(ensure_dirs "$data" bw broker secrets "secrets/${slug}")"
  sdir="${data}/secrets/${slug}"
  chmod 700 "$sdir"
  write_private "${sdir}/client_secret" "$secret"
  if [[ "$unlock" == stored ]]; then
    write_private "${sdir}/master_password" "$password"
  else
    rm -f "${sdir}/master_password"
  fi
  secret=""
  password=""

  local scope_line="BW_SCOPE=${scope}"
  [[ "$scope" == *"#"* ]] && scope_line="BW_SCOPE=\"${scope}\""
  mkdir -p "$ACCOUNTS_DIR"
  local tmp
  tmp="$(umask 077 && mktemp "${ACCOUNTS_DIR}/.${slug}.XXXXXX")"
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
  log "Wrote ${file#"${ROOT}"/} and secrets/${slug}/"
  # shellcheck disable=SC2086  # one folder per line, no spaces in the names we make
  [[ -z "$new" ]] || linux_owner $new "$file"
  if ! is_true "$(env_get BITWARDEN_ENABLED 0)"; then
    upsert_env "$ENV_PATH" BITWARDEN_ENABLED 1
    log "Set BITWARDEN_ENABLED=1"
  fi

  if [[ "$scope" != all ]]; then
    offer_setup "$slug" "$scope"
  fi
  check_account "$slug" "$unlock"
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
  setup_run seed --slug "$slug" --email "$email" || log "bitwarden-setup seed stopped; make vault-grant moves items later"
}

# With the broker up, restart it so it loads the new file, and report what it
# makes of the credentials. An auth error removes what add wrote.
check_account() {
  local slug="$1" unlock="$2" got state class
  if ! broker_running; then
    log "The broker checks the API key when it starts: make up, then make doctor"
    return 0
  fi
  log "Restarting the broker so it loads ${slug}"
  restart_broker
  got="$(wait_for_account "$slug" "$unlock")"
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
        local data
        data="$(data_dir)"
        rm -f "$(account_file "$slug")"
        rm -rf "${data:?}/secrets/${slug:?}"
        restart_broker
        die "Bitwarden refused the API key or master password for ${slug} (auth); nothing was kept"
      fi
      log "${slug}: the broker reports an error (${class}); make doctor"
      ;;
    offline) log "${slug}: the server is not reachable from the broker (${class}); make doctor" ;;
    *) log "${slug}: the broker has not loaded it; make doctor shows why" ;;
  esac
}

# --- unlock, lock, sync, requests, grant -----------------------------------

need_manual() {
  need_slug "$1"
  need_account "$1"
  [[ "$(file_get "$(account_file "$1")" BW_UNLOCK)" == manual ]] \
    || die "$1 uses stored unlock; the broker unlocks it by itself"
}

cmd_unlock() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for unlock: $1" ;;
    esac
  done
  need_manual "$slug"
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
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for lock: $1" ;;
    esac
  done
  need_manual "$slug"
  require_broker
  local rc=0
  broker_exec python3 -m broker.unlock --lock "$slug" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) log "${slug} is locked" ;;
    3) die "the broker has no manual account ${slug}" ;;
    *) die "the broker could not lock ${slug}; make doctor" ;;
  esac
}

cmd_sync() {
  [[ $# -eq 0 ]] || die "sync takes no options"
  require_broker
  broker_cli sync
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

# The local account's owner, as local-init recorded it.
state_get() {
  local state
  state="$(data_dir)/local/state"
  [[ -e "$state" ]] || return 0
  if [[ -r "$state" ]]; then
    file_get "$state" "$1"
  else
    # On Linux the file belongs to uid 10001; bitwarden-setup can read it.
    docker compose --profile bitwarden-tools run --rm --no-deps -T --entrypoint cat bitwarden-setup /local/state 2>/dev/null \
      | grep -E "^${1}=" | head -1 | cut -d= -f2- || true
  fi
}

cmd_grant() {
  local site="" slug="" email="" item=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --site) site="${2:-}"; shift 2 ;;
      --slug) slug="${2:-}"; shift 2 ;;
      --email) email="${2:-}"; shift 2 ;;
      --item) item="${2:-}"; shift 2 ;;
      *) die "unknown option for grant: $1" ;;
    esac
  done
  require_broker
  if [[ -z "$site" ]]; then
    local rows n pick row r_account r_site r_reason
    rows="$(broker_cli requests --tsv)" || die "the broker did not list its requests; make doctor"
    if [[ -z "$rows" ]]; then
      log "No pending access requests. make vault-grant SITE=example.com grants a site directly."
      return 0
    fi
    n=0
    printf 'Pending access requests (the reason is the agent'"'"'s text):\n'
    while IFS=$'\t' read -r _ r_account r_site r_reason; do
      n=$((n + 1))
      printf '  %d. %s  (account %s)  %s\n' "$n" "$r_site" "$r_account" "$r_reason"
    done <<< "$rows"
    [[ -t 0 ]] || die "pass --site (make vault-grant SITE=...) when there is no terminal to pick in"
    printf 'Grant which? '
    IFS= read -r pick
    [[ "$pick" =~ $NUMBER_RE && "$pick" -ge 1 && "$pick" -le "$n" ]] || die "enter a number from 1 to ${n}"
    row="$(printf '%s\n' "$rows" | sed -n "${pick}p")"
    site="$(printf '%s' "$row" | cut -f3)"
    [[ -n "$slug" ]] || slug="$(printf '%s' "$row" | cut -f2)"
    [[ "$slug" != "-" ]] || slug=""
  fi
  if [[ -z "$slug" ]]; then
    local f count=0
    for f in "$ACCOUNTS_DIR"/*.env; do
      [[ -f "$f" ]] || continue
      count=$((count + 1))
      slug="$(basename "$f" .env)"
    done
    [[ "$count" -eq 1 ]] || die "several accounts or none; pass --slug (make vault-grant SLUG=...)"
  fi
  need_slug "$slug"
  need_account "$slug"
  [[ -n "$email" ]] || email="${VAULT_OWNER_EMAIL:-}"
  [[ -n "$email" || "$slug" != local ]] || email="$(state_get OWNER_EMAIL)"
  if [[ -z "$email" ]]; then
    [[ -t 0 ]] || die "pass --email with your own Bitwarden email"
    printf 'Your Bitwarden email for %s: ' "$slug"
    IFS= read -r email
  fi
  valid_email "$email" || die "not an email address: ${email}"
  local args=(grant --slug "$slug" --email "$email" --site "$site")
  [[ -z "$item" ]] || args+=(--item "$item")
  setup_run "${args[@]}" || die "bitwarden-setup grant did not move anything; the request stays pending"
  broker_cli sync >/dev/null || log "The broker did not sync; make vault-sync picks the item up"
  log "The broker synced; a pending request for ${site} is done once the item is in scope"
}

# --- remove, open -----------------------------------------------------------

cmd_remove() {
  local slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --slug) slug="${2:-}"; shift 2 ;;
      *) die "unknown option for remove: $1" ;;
    esac
  done
  need_slug "$slug"
  [[ "$slug" != local ]] \
    || die "the local account is part of make vault-init; BITWARDEN_LOCAL_ENABLED=0 and make up turn local mode off"
  need_account "$slug"
  local data
  data="$(data_dir)"
  rm -f "$(account_file "$slug")"
  rm -rf "${data:?}/secrets/${slug:?}"
  rm -rf "${data:?}/bw/${slug:?}" 2>/dev/null \
    || log "Could not remove ${data}/bw/${slug} (owned by the broker's user); remove it as root"
  log "Removed ${slug}: its account file and secrets"
  if broker_running; then
    restart_broker
    log "Restarted the broker, which no longer holds ${slug}"
  fi
  log "The API key still exists in Bitwarden; revoke it there (Settings > Security > Keys) if you are done with it"
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

# --- secrets-sync and the make up hook --------------------------------------

cmd_secrets_sync() {
  local rc=0
  docker compose --profile bitwarden-tools run --rm -T bitwarden-secrets-sync || rc=$?
  case "$rc" in
    0) ;;
    3)
      log "Files Hermes reads changed; running make hermes-reload"
      make -C "$ROOT" hermes-reload
      ;;
    *) die "secrets-sync wrote nothing or not everything (see above); is the broker running? make up, then make doctor" ;;
  esac
}

# Run by make up after docker compose up. Silent while BITWARDEN_ENABLED is off.
cmd_up_hook() {
  is_true "$(env_get BITWARDEN_ENABLED 0)" || return 0
  local f slugs="" slug
  for f in "$ACCOUNTS_DIR"/*.env; do
    [[ -f "$f" ]] || continue
    [[ -n "$(file_get "$f" BW_SECRETS_ITEM_SCOPE)" ]] || continue
    slugs="${slugs} $(basename "$f" .env)"
  done
  [[ -n "$slugs" ]] || return 0
  if ! broker_running; then
    log "Bitwarden secrets sync skipped: the broker is not running"
    return 0
  fi
  local run="" got state
  for slug in $slugs; do
    got="$(wait_for_account "$slug" "$(file_get "$(account_file "$slug")" BW_UNLOCK)")"
    state="${got%% *}"
    case "$state" in
      unlocked) run=1 ;;
      locked) log "Bitwarden secrets sync skipped: ${slug} is locked" ;;
      offline) log "Bitwarden secrets sync skipped: ${slug} is offline" ;;
      error) log "Bitwarden secrets sync skipped: ${slug} is in error (make doctor)" ;;
      *) log "Bitwarden secrets sync skipped: the broker has not loaded ${slug} (make doctor)" ;;
    esac
  done
  if [[ -n "$run" ]]; then
    (cmd_secrets_sync) || log "Bitwarden secrets sync failed; make secrets-sync to retry"
  fi
  return 0
}

# --- init -------------------------------------------------------------------

cmd_init() {
  local owner="" wait="$INIT_WAIT_SECONDS"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --owner) owner="${2:-}"; shift 2 ;;
      *) die "unknown option for init: $1" ;;
    esac
  done
  local data new
  data="$(data_dir)"
  # Step 1 of the order: every bind-mount source exists before any up.
  new="$(ensure_dirs "$data" bw broker secrets secrets/local secrets/local/tls \
    local local/tls local/tls/server local/vaultwarden)"
  ensure_dirs "$data" vaultwarden >/dev/null
  chmod 700 "${data}/secrets" "${data}/local"
  # shellcheck disable=SC2086  # one folder per line, no spaces in the names we make
  [[ -z "$new" ]] || linux_owner $new

  local step recorded
  step="$(state_get STEP)"
  recorded="$(lower "$(state_get OWNER_EMAIL)")"
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

  log "Last step: you sign in once so the agent account becomes a plain User with access to Agent and Stack secrets only"
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
  log "The local Vaultwarden holds the only copy of what you store in it. Back it up with scripts/vault.sh backup DIR."
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
  requests) shift; cmd_requests "$@" ;;
  grant) shift; cmd_grant "$@" ;;
  remove) shift; cmd_remove "$@" ;;
  open) shift; cmd_open "$@" ;;
  secrets-sync) shift; cmd_secrets_sync "$@" ;;
  up-hook) cmd_up_hook ;;
  backup) shift; cmd_backup "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  -h | --help | help) usage ;;
  *) usage >&2; exit 1 ;;
esac
