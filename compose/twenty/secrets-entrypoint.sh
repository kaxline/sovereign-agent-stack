#!/bin/sh
# Entrypoint for twenty-server and twenty-worker. When TWENTY_ENCRYPTION_KEY_FILE
# or TWENTY_DB_PASSWORD_FILE is set (compose points them at /run/secrets/*), read
# the secret from the file here, so it never sits in the container's environment
# as docker inspect shows it. Then hand over to Twenty's own entrypoint, which
# runs migrations (server) and execs the command.
#
# A secret set both as a value and as a file is refused. Errors name the
# variable, never its value.
set -eu

die() {
  printf '[twenty-secrets] ERROR: %s\n' "$1" >&2
  exit 1
}

# File contents without the trailing newline; empty or unreadable is an error.
read_secret() {
  [ -r "$2" ] || die "$1 points at a file that can't be read"
  _value="$(cat "$2")"
  [ -n "$_value" ] || die "$1 points at an empty file"
  printf '%s' "$_value"
}

if [ -n "${TWENTY_ENCRYPTION_KEY_FILE:-}" ]; then
  [ -z "${ENCRYPTION_KEY:-}" ] \
    || die "set TWENTY_ENCRYPTION_KEY or TWENTY_ENCRYPTION_KEY_FILE in .env, not both"
  ENCRYPTION_KEY="$(read_secret TWENTY_ENCRYPTION_KEY_FILE "$TWENTY_ENCRYPTION_KEY_FILE")"
  export ENCRYPTION_KEY
fi

if [ -n "${TWENTY_DB_PASSWORD_FILE:-}" ]; then
  # Compose builds the URL from TWENTY_DB_PASSWORD, which is empty in file mode.
  case "${PG_DATABASE_URL:-}" in
    postgres://twenty:@*) ;;
    *) die "set TWENTY_DB_PASSWORD or TWENTY_DB_PASSWORD_FILE in .env, not both" ;;
  esac
  _password="$(read_secret TWENTY_DB_PASSWORD_FILE "$TWENTY_DB_PASSWORD_FILE")"
  # A file may hold any characters; the URL needs them percent-encoded.
  _encoded="$(printf '%s' "$_password" | node -e \
    'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(encodeURIComponent(s)))')" \
    || die "could not encode TWENTY_DB_PASSWORD_FILE"
  PG_DATABASE_URL="postgres://twenty:${_encoded}@${PG_DATABASE_URL#postgres://twenty:@}"
  export PG_DATABASE_URL
  unset _password _encoded
fi

exec "${TWENTY_ENTRYPOINT:-/app/entrypoint.sh}" "$@"
