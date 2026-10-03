#!/bin/sh
# Slug -> CALDAV_<SLUG>_PASSWORD naming used by hermes-api-bootstrap.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

# shellcheck disable=SC1090
eval "$(sed -n '/^caldav_password_env_key() {/,/^}/p' "$bootstrap")"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

got="$(caldav_password_env_key personal)"
[ "$got" = "CALDAV_PERSONAL_PASSWORD" ] || fail "personal -> ${got}"

got="$(caldav_password_env_key work-cal)"
[ "$got" = "CALDAV_WORK_CAL_PASSWORD" ] || fail "work-cal -> ${got}"

printf 'OK caldav password env key\n'
