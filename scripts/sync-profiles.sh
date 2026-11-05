#!/usr/bin/env bash
# Set COMPOSE_PROFILES from the per-service *_ENABLED flags in .env, so each
# optional service has one switch. Called by make ensure-local (and so make up)
# and scripts/setup.sh.
#
#   scripts/sync-profiles.sh          sync, printing what changed
#   scripts/sync-profiles.sh --list   print each service's flag and state
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
SERVICES="LIGHTRAG_MCP_ENABLED rag 0
CALDAV_MCP_ENABLED calendar 0
SQL_MCP_ENABLED db 0
VIKUNJA_MCP_ENABLED todo 0
MAIL_MCP_ENABLED mail 0
MAIL_DRAFTS_ENABLED mail-drafts 0
MESSAGES_MCP_ENABLED messages 0
OPENCODE_MCP_ENABLED coding 0
TWENTY_LOCAL_ENABLED crm 0
AUTOMATION_ENABLED automation 1
GPTR_MCP_ENABLED research 1
OLLAMA_ENABLED ollama 1
SEARXNG_PROD_ENABLED searxng-prod 1
SEARCH_GATE_ENABLED search-gate 1"

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

if [[ "$profiles" != "$before_profiles" ]]; then
  upsert_env .env COMPOSE_PROFILES "$profiles"
fi
if [[ "$managed" != "$before_managed" ]]; then
  upsert_env .env COMPOSE_PROFILES_MANAGED "$managed"
fi
