#!/bin/sh
# Slug -> CALDAV_<SLUG>_PASSWORD naming used by hermes-api-bootstrap.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
bootstrap="${ROOT}/compose/hermes/bootstrap-api-profile.sh"

load_fns "$bootstrap" caldav_password_env_key

got="$(caldav_password_env_key personal)"
[ "$got" = "CALDAV_PERSONAL_PASSWORD" ] || fail "personal -> ${got}"

got="$(caldav_password_env_key work-cal)"
[ "$got" = "CALDAV_WORK_CAL_PASSWORD" ] || fail "work-cal -> ${got}"

printf 'OK caldav password env key\n'
