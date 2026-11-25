# Helpers shared by scripts/doctor.sh and its sections (scripts/lib/doctor-*.sh).
# doctor.sh sources this from the repo root with ROOT set; the section tests
# source it too, so a section runs against the same ok/bad/warn and DATA_ROOT
# it gets in doctor.
# shellcheck shell=bash

# shellcheck source=lib/env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# shellcheck source=lib/data-root.sh
source "$(dirname "${BASH_SOURCE[0]}")/data-root.sh"

# The user data root as written, and as an absolute path. ASSISTANT_DATA_ROOT
# from the environment wins over .env, as in compose, addon.sh and vault.sh.
DATA_ROOT_RAW="${ASSISTANT_DATA_ROOT:-$(env_get ASSISTANT_DATA_ROOT ./data)}"
# shellcheck disable=SC2034  # read by doctor.sh and the sections
DATA_ROOT="$(expand_user_path "$DATA_ROOT_RAW")"

PASS="${PASS:-0}"
FAIL="${FAIL:-0}"
WARN="${WARN:-0}"

ok() { printf '  OK  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf ' FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf ' WARN %s\n' "$1"; WARN=$((WARN + 1)); }

# has_profile NAME: has_compose_profile under the name doctor.sh and the
# sections use.
has_profile() { has_compose_profile "$1"; }

# svc_running SERVICE: whether compose has SERVICE running.
svc_running() {
  docker compose ps --status running "$1" 2>/dev/null | grep -q "$1"
}

# service_health SERVICE: its container's healthcheck status, else its state
# (running, exited...), or nothing when compose has no container for it.
service_health() {
  local cid
  cid="$(docker compose ps -q "$1" 2>/dev/null || true)"
  [[ -n "$cid" ]] || return 0
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true
}

# check_service_health SERVICE [NOTE]: one doctor line for SERVICE's health.
# NOTE, when given, explains a "starting" state in parentheses.
check_service_health() {
  local state
  state="$(service_health "$1")"
  case "$state" in
    healthy) ok "${1} is healthy" ;;
    starting) warn "${1} is starting${2:+ ($2)}" ;;
    "") warn "${1} not running — make up" ;;
    *) bad "${1} is ${state} — docker compose logs ${1}" ;;
  esac
}

# check_flag_profile FLAG PROFILE [OKTEXT]: whether COMPOSE_PROFILES matches
# an add-on's .env flag. One OK line when PROFILE is on (OKTEXT replaces the
# default text), one FAIL line when FLAG is on and PROFILE is not, and nothing
# when both are off. FLAG and PROFILE are a row of sync-profiles.sh --table;
# test-doctor-kit.sh checks every call against it.
check_flag_profile() {
  if has_profile "$2"; then
    ok "${3:-COMPOSE_PROFILES includes $2}"
  elif env_true "$1"; then
    bad "${1}=1 but '${2}' not in COMPOSE_PROFILES — run make ensure-local"
  fi
}

# check_flag_set FLAG PROFILE NOTE: the other half of check_flag_profile. One
# OK line when FLAG is on, else a WARN that PROFILE is on without it, ending
# in NOTE (what Hermes misses).
check_flag_set() {
  if env_true "$1"; then
    ok "${1}=1"
  else
    warn "${2} profile on but ${1} is not 1 — ${3}"
  fi
}

# hermes_profile_config LABEL: the config.yaml of Hermes profile LABEL
# (default, api-server, browser), relative to the repo root.
hermes_profile_config() {
  if [[ "$1" == default ]]; then
    printf '%s\n' data/hermes/config.yaml
  else
    printf '%s\n' "data/hermes/profiles/${1}/config.yaml"
  fi
}

# check_profile_registers LABEL PATTERN WHAT [MISSING]: whether Hermes profile
# LABEL's config.yaml matches the extended regex PATTERN. An OK line naming
# WHAT when it does; when it doesn't and MISSING is given, a WARN that the
# profile has no MISSING entry. Nothing when the profile has no config.yaml.
check_profile_registers() {
  local cfg
  cfg="$(hermes_profile_config "$1")"
  [[ -f "$cfg" ]] || return 0
  if grep -qE -- "$2" "$cfg"; then
    ok "Hermes ${1} profile registers ${3}"
  elif [[ -n "${4:-}" ]]; then
    warn "Hermes ${1} profile has no ${4} entry — run make hermes-reload"
  fi
}

# check_api_server_clean PATTERN MSG: a FAIL line with MSG when the api-server
# profile's config.yaml matches the extended regex PATTERN, for MCP servers
# unattended sessions must not have.
check_api_server_clean() {
  if grep -qsE -- "$1" "$(hermes_profile_config api-server)"; then
    bad "$2"
  fi
}
