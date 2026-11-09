#!/usr/bin/env bash
# Set COMPOSE_PROFILES from the per-service *_ENABLED flags in .env, so each
# optional service has one switch. Called by make ensure-local (and so make up)
# and scripts/setup.sh.
#
#   scripts/sync-profiles.sh          sync, printing what changed
#   scripts/sync-profiles.sh --list   print each service's flag and state
#   scripts/sync-profiles.sh --table  print the flag, profile and seed table
#
# A flag set to 1 adds its profile. A flag set to 0 removes the profile only if
# this script added or adopted it, which it records in COMPOSE_PROFILES_MANAGED.
# A profile written into COMPOSE_PROFILES by hand while its flag is 0 keeps
# running, with a warning, as it did before this script existed. Profiles with
# no flag (core, n8n-postgres-legacy, anything from an override file) are never
# touched. The signal profile is synced by sync-signal-profile.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# flag profile seed
# seed=1: the flag is newer than its profile. When the flag is missing from
# .env, it is written as 1 if the profile is already on and 0 otherwise, so
# existing installs keep running what they run. seed=0: the flag predates this
# script; a missing flag leaves the profile alone.
#
# Built-in add-ons (addons/<id>/addon.json) add their own rows from the
# manifest's builtin block; this list keeps the services that have no
# manifest yet. docs/private/addon-manifest-spec.md
SERVICES_CORE="LIGHTRAG_MCP_ENABLED rag 0
NEO4J_ENABLED neo4j 0
CALDAV_MCP_ENABLED calendar 0
SQL_MCP_ENABLED db 0
VIKUNJA_MCP_ENABLED todo 0
MESSAGES_MCP_ENABLED messages 0
OPENCODE_MCP_ENABLED coding 0
BITWARDEN_LOCAL_ENABLED vault 0
ACTUAL_LOCAL_ENABLED budget-local 0
AUTOMATION_ENABLED automation 1
OLLAMA_ENABLED ollama 1
SEARXNG_PROD_ENABLED searxng-prod 1
SEARCH_GATE_ENABLED search-gate 1"

# One "flag profile seed" row per addons/*/addon.json. No python3 here (first
# run, scripts/test-no-host-python.sh), so the builtin block is read with sed.
# That relies on the canonical layout scripts/test-addon-manifests.py
# enforces: the block opens with `  "builtin": {` and has one key per line.
# A built-in manifest may also list `gates`: the list opens with `  "gates": [`,
# each gate has "flag", "profile", "seed" lines in that order, and one row per
# gate follows the builtin row.
addon_rows() {
  local manifest block flag profile seed gates gflag gprofile gseed line
  for manifest in "${ROOT}"/addons/*/addon.json; do
    [[ -f "$manifest" ]] || continue
    block="$(sed -n '/^  "builtin": {$/,/^  }/p' "$manifest")"
    flag="$(printf '%s\n' "$block" | sed -n 's/^    "flag": "\([A-Z0-9_]*\)",*$/\1/p')"
    profile="$(printf '%s\n' "$block" | sed -n 's/^    "profile": "\([a-z0-9-]*\)",*$/\1/p')"
    seed="$(printf '%s\n' "$block" | sed -n 's/^    "seed": \([a-z]*\),*$/\1/p')"
    case "$seed" in
      true) seed=1 ;;
      false) seed=0 ;;
      *) seed="" ;;
    esac
    if [[ -z "$flag" || -z "$profile" || -z "$seed" ]]; then
      die "${manifest#"${ROOT}/"} has no readable builtin block; run python3 scripts/test-addon-manifests.py"
    fi
    printf '%s %s %s\n' "$flag" "$profile" "$seed"
    gates="$(sed -n '/^  "gates": \[$/,/^  \]/p' "$manifest")"
    [[ -n "$gates" ]] || continue
    gflag="" gprofile=""
    while IFS= read -r line; do
      case "$line" in
        *'"flag": "'*) gflag="$(printf '%s\n' "$line" | sed -n 's/^ *"flag": "\([A-Z0-9_]*\)",*$/\1/p')" ;;
        *'"profile": "'*) gprofile="$(printf '%s\n' "$line" | sed -n 's/^ *"profile": "\([a-z0-9-]*\)",*$/\1/p')" ;;
        *'"seed": '*)
          gseed="$(printf '%s\n' "$line" | sed -n 's/^ *"seed": \([a-z]*\),*$/\1/p')"
          case "$gseed" in true) gseed=1 ;; false) gseed=0 ;; *) gseed="" ;; esac
          if [[ -z "$gflag" || -z "$gprofile" || -z "$gseed" ]]; then
            die "${manifest#"${ROOT}/"} has an unreadable gates block; run python3 scripts/test-addon-manifests.py"
          fi
          printf '%s %s %s\n' "$gflag" "$gprofile" "$gseed"
          gflag="" gprofile=""
          ;;
      esac
    done <<EOF_GATES
$gates
EOF_GATES
  done
}

# A plain assignment, so a die inside addon_rows stops the script (set -e).
ADDON_ROWS="$(addon_rows)"
SERVICES="$(printf '%s\n%s\n' "$SERVICES_CORE" "$ADDON_ROWS" | sed '/^$/d')"
dup="$(printf '%s\n' "$SERVICES" | cut -d' ' -f1 | sort | uniq -d | paste -sd, - || true)"
[[ -z "$dup" ]] || die "flag listed twice (SERVICES_CORE and an add-on manifest?): ${dup}"

if [[ "${1:-}" == "--table" ]]; then
  printf '%s\n' "$SERVICES"
  exit 0
fi

env_has() {
  grep -qE "^${1}=" .env 2>/dev/null
}

truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# csv helpers (bash 3.2: no associative arrays)
csv_has() {
  [[ ",$1," == *",$2,"* ]]
}
csv_add() {
  if [[ -z "$1" ]]; then
    printf '%s' "$2"
  elif csv_has "$1" "$2"; then
    printf '%s' "$1"
  else
    printf '%s,%s' "$1" "$2"
  fi
}
csv_remove() {
  printf '%s' "$1" | tr ',' '\n' | grep -vx -- "$2" | sed '/^$/d' | paste -sd, - || true
}
csv_clean() {
  # Strip spaces and empty items, and the pre-core hermes-webui name.
  printf '%s' "$1" | tr -d ' ' | tr ',' '\n' | grep -vx 'hermes-webui' | sed '/^$/d' | paste -sd, - || true
}

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

if [[ "${1:-}" == "--list" ]]; then
  profiles="$(csv_clean "$(env_get COMPOSE_PROFILES core)")"
  printf '%-22s %-14s %s\n' FLAG PROFILE STATE
  while read -r flag profile _seed; do
    value="$(env_get "$flag")"
    if csv_has "$profiles" "$profile"; then
      if truthy "$value"; then state=on; else state="on (flag is ${value:-unset})"; fi
    else
      if truthy "$value"; then state="off (run make ensure-local)"; else state=off; fi
    fi
    printf '%-22s %-14s %s\n' "$flag" "$profile" "$state"
  done <<< "$SERVICES"
  value="$(env_get HERMES_SIGNAL_ENABLED)"
  if csv_has "$profiles" signal; then state=on; else state=off; fi
  printf '%-22s %-14s %s\n' HERMES_SIGNAL_ENABLED signal "$state"
  echo
  echo "COMPOSE_PROFILES=${profiles}"
  exit 0
fi

before_profiles="$(env_get COMPOSE_PROFILES)"
before_managed="$(env_get COMPOSE_PROFILES_MANAGED)"
profiles="$(csv_clean "$(env_get COMPOSE_PROFILES core)")"
managed="$(csv_clean "$before_managed")"

while read -r flag profile seed; do
  if ! env_has "$flag"; then
    [[ "$seed" == 1 ]] || continue
    if csv_has "$profiles" "$profile"; then
      upsert_env .env "$flag" 1
      log "Set ${flag}=1 (the ${profile} profile was already on)"
    else
      upsert_env .env "$flag" 0
    fi
  fi

  if truthy "$(env_get "$flag")"; then
    if ! csv_has "$profiles" "$profile"; then
      profiles="$(csv_add "$profiles" "$profile")"
      log "Added '${profile}' to COMPOSE_PROFILES (${flag}=1)"
    fi
    managed="$(csv_add "$managed" "$profile")"
  elif csv_has "$managed" "$profile"; then
    if csv_has "$profiles" "$profile"; then
      profiles="$(csv_remove "$profiles" "$profile")"
      log "Removed '${profile}' from COMPOSE_PROFILES (${flag}=0)"
    fi
    managed="$(csv_remove "$managed" "$profile")"
  elif csv_has "$profiles" "$profile"; then
    warn "'${profile}' is in COMPOSE_PROFILES but ${flag} is not 1. Set ${flag}=1 to switch it from the flag, or remove '${profile}' from COMPOSE_PROFILES."
  fi
done <<< "$SERVICES"

[[ -n "$profiles" ]] || profiles=core

# Keys docker-compose.yml leaves optional so installs without these profiles
# need not set them. Checked against the final profile list, so a profile
# added by hand counts too. COMPOSE_PROFILES is left as it was on failure.
missing=""
if csv_has "$profiles" rag || csv_has "$profiles" neo4j; then
  [[ -n "$(env_get NEO4J_PASSWORD)" ]] || missing="${missing} NEO4J_PASSWORD"
fi
if csv_has "$profiles" rag; then
  [[ -n "$(env_get LIGHTRAG_API_KEY)" ]] || missing="${missing} LIGHTRAG_API_KEY"
fi
if [[ -n "$missing" ]]; then
  die "rag or neo4j is on but${missing} not set in .env. Run ./scripts/setup.sh to generate them."
fi

if [[ "$profiles" != "$before_profiles" ]]; then
  upsert_env .env COMPOSE_PROFILES "$profiles"
fi
if [[ "$managed" != "$before_managed" ]]; then
  upsert_env .env COMPOSE_PROFILES_MANAGED "$managed"
fi
