# Helpers shared by scripts/doctor.sh and its sections (scripts/lib/doctor-*.sh).
# doctor.sh sources this from the repo root; the section tests source it too,
# so a section runs against the same ok/bad/warn it gets in doctor.
# shellcheck shell=bash

# shellcheck source=env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

PASS="${PASS:-0}"
FAIL="${FAIL:-0}"
WARN="${WARN:-0}"

ok() { printf '  OK  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf ' FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf ' WARN %s\n' "$1"; WARN=$((WARN + 1)); }

# has_profile NAME: whether .env's COMPOSE_PROFILES (default core) lists NAME.
has_profile() {
  local p IFS=','
  for p in $(env_get COMPOSE_PROFILES core); do
    p="${p//[[:space:]]/}"
    [[ "$p" == "$1" ]] && return 0
  done
  return 1
}

# svc_running SERVICE: whether compose has SERVICE running.
svc_running() {
  docker compose ps --status running "$1" 2>/dev/null | grep -q "$1"
}
