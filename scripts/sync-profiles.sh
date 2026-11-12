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
SERVICES_CORE="NEO4J_ENABLED neo4j 0
OLLAMA_ENABLED ollama 1
SEARXNG_PROD_ENABLED searxng-prod 1
SEARCH_GATE_ENABLED search-gate 1"

# One "flag profile seed" row per addons/*/addon.json. No python3 here (first
# run, scripts/test-no-host-python.sh), so the builtin block is read with awk.
# That relies on the canonical layout scripts/test-addon-manifests.py
# enforces: the block opens with `  "builtin": {` and has one key per line.
# A built-in manifest may also list `gates`: the list opens with `  "gates": [`,
# each gate has "flag", "profile", "seed" lines in that order, and one row per
# gate follows the builtin row.
addon_rows() {
  local manifest out line file
  local -a manifests=()
  for manifest in "${ROOT}"/addons/*/addon.json; do
    [[ -f "$manifest" ]] && manifests+=("$manifest")
  done
  [[ ${#manifests[@]} -gt 0 ]] || return 0
  # One awk run for every manifest; a sed per key cost a process each. It
  # prints the rows, and "! builtin FILE" or "! gates FILE" for a block it
  # cannot read. A builtin key given twice counts as unreadable.
  out="$(awk '
    function val(s) { sub(/^ *"[a-z]*": "?/, "", s); sub(/"?,*$/, "", s); return s }
    function bool(s) { return s == "true" ? 1 : s == "false" ? 0 : "" }
    function flush() {
      if (file == "") return
      if (flag == "" || profile == "" || seed == "" || twice) { print "! builtin " file; return }
      print flag " " profile " " seed
      printf "%s", rows
      if (gbad) print "! gates " file
    }
    function builtin_line() {
      if ($0 ~ /^    "flag": "[A-Z0-9_]*",*$/) { twice += flag != ""; flag = val($0) }
      else if ($0 ~ /^    "profile": "[a-z0-9-]*",*$/) { twice += profile != ""; profile = val($0) }
      else if ($0 ~ /^    "seed": [a-z]*,*$/) { twice += seed != ""; seed = bool(val($0)) }
    }
    function gate_line() {
      if (index($0, "\"flag\": \"")) gflag = $0 ~ /^ *"flag": "[A-Z0-9_]*",*$/ ? val($0) : ""
      else if (index($0, "\"profile\": \"")) gprofile = $0 ~ /^ *"profile": "[a-z0-9-]*",*$/ ? val($0) : ""
      else if (index($0, "\"seed\": ")) {
        gseed = $0 ~ /^ *"seed": [a-z]*,*$/ ? bool(val($0)) : ""
        if (gflag == "" || gprofile == "" || gseed == "") gbad = 1
        else if (!gbad) rows = rows gflag " " gprofile " " gseed "\n"
        gflag = gprofile = ""
      }
    }
    FNR == 1 {
      flush(); file = FILENAME
      inb = ing = twice = gbad = 0; flag = profile = seed = rows = gflag = gprofile = ""
    }
    inb { if (/^  [}]/) inb = 0; else builtin_line(); next }
    ing { if (/^  []]/) ing = 0; else gate_line(); next }
    /^  "builtin": [{]$/ { inb = 1; next }
    /^  "gates": [[]$/ { ing = 1; next }
    END { flush() }
  ' "${manifests[@]}")" || die "could not read the add-on manifests in ${ROOT}/addons"
  while IFS= read -r line; do
    case "$line" in
      "! builtin "*)
        file="${line#"! builtin "}"
        die "${file#"${ROOT}/"} has no readable builtin block; run python3 scripts/test-addon-manifests.py"
        ;;
      "! gates "*)
        file="${line#"! gates "}"
        die "${file#"${ROOT}/"} has an unreadable gates block; run python3 scripts/test-addon-manifests.py"
        ;;
      ?*) printf '%s\n' "$line" ;;
    esac
  done <<< "$out"
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

truthy() {
  case "$1" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn]) return 0 ;;
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

# .env, read once into envv_<KEY> variables, first line winning as in env_get.
# env_get forks grep, head and cut, and the loop below asks about every flag.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" == *=* ]] || continue
  key="${line%%=*}"
  case "$key" in "" | [0-9]* | *[!A-Za-z0-9_]*) continue ;; esac
  name="envv_${key}"
  [[ -n "${!name+x}" ]] || printf -v "$name" '%s' "${line#*=}"
done < .env
env_present() {
  local name="envv_$1"
  [[ -n "${!name+x}" ]]
}
# env_value KEY [DEFAULT]: sets $value to what env_get KEY DEFAULT prints.
env_value() {
  local name="envv_$1"
  value="${!name-}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  [[ -n "$value" ]] || value="${2:-}"
}
# env_set KEY VALUE: after upsert_env appends a key .env did not have.
env_set() {
  printf -v "envv_$1" '%s' "$2"
}

if [[ "${1:-}" == "--list" ]]; then
  env_value COMPOSE_PROFILES core
  profiles="$(csv_clean "$value")"
  printf '%-22s %-14s %s\n' FLAG PROFILE STATE
  while read -r flag profile _seed; do
    env_value "$flag"
    if csv_has "$profiles" "$profile"; then
      if truthy "$value"; then state=on; else state="on (flag is ${value:-unset})"; fi
    else
      if truthy "$value"; then state="off (run make ensure-local)"; else state=off; fi
    fi
    printf '%-22s %-14s %s\n' "$flag" "$profile" "$state"
  done <<< "$SERVICES"
  if csv_has "$profiles" signal; then state=on; else state=off; fi
  printf '%-22s %-14s %s\n' HERMES_SIGNAL_ENABLED signal "$state"
  echo
  echo "COMPOSE_PROFILES=${profiles}"
  exit 0
fi

env_value COMPOSE_PROFILES
before_profiles="$value"
env_value COMPOSE_PROFILES_MANAGED
before_managed="$value"
env_value COMPOSE_PROFILES core
profiles="$(csv_clean "$value")"
managed="$(csv_clean "$before_managed")"

while read -r flag profile seed; do
  if ! env_present "$flag"; then
    [[ "$seed" == 1 ]] || continue
    if csv_has "$profiles" "$profile"; then
      upsert_env .env "$flag" 1
      env_set "$flag" 1
      log "Set ${flag}=1 (the ${profile} profile was already on)"
    else
      upsert_env .env "$flag" 0
      env_set "$flag" 0
    fi
  fi

  env_value "$flag"
  if truthy "$value"; then
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
  env_value NEO4J_PASSWORD
  [[ -n "$value" ]] || missing="${missing} NEO4J_PASSWORD"
fi
if csv_has "$profiles" rag; then
  env_value LIGHTRAG_API_KEY
  [[ -n "$value" ]] || missing="${missing} LIGHTRAG_API_KEY"
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
