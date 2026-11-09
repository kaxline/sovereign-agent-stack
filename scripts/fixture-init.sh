#!/usr/bin/env bash
# Init hook for scripts/fixtures/addons-cli/hooked, used by scripts/test-addon-cli.sh.
# Prints a progress line and a done line that reports an account. FIXTURE_INIT_MODE
# makes it fail instead: `exit5` exits 5, `error` prints an error line and exits 1
# with the name in FIXTURE_INIT_ERROR; `error0` does the same and exits 0.
set -eu
printf '{"phase":"setup","status":"running","message":"creating %s"}\n' "${1:-nothing}"
printf 'plain log line\n'
case "${FIXTURE_INIT_MODE:-}" in
  exit5) exit 5 ;;
  error | error0)
    printf '{"phase":"error","error":"%s","message":"hook says no"}\n' "${FIXTURE_INIT_ERROR:-bad_slug}"
    [ "$FIXTURE_INIT_MODE" = error0 ] && exit 0
    exit 1
    ;;
esac
printf '{"phase":"done","account":"main"}\n'
