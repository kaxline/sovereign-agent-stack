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
