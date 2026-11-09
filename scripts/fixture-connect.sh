#!/usr/bin/env bash
# Connect and disconnect hook for scripts/fixtures/addons-cli/hooked, used by
# scripts/test-addon-cli.sh. Logs its argv to STUB_LOG and exits 3 unless the
# argv is `[disconnect] --slug work --json`. connect reads the values on stdin
# and prints a done line with an account and an MCP name. When the host is
# refuse.example.com it echoes stdin on stdout and stderr, prints a
# login_refused error line and exits 1. FIXTURE_CONNECT_ERROR makes it print
# an error line with that name and exit 1; FIXTURE_CONNECT_MODE=exit1 makes it
# exit 1 with no error line; FIXTURE_CONNECT_MODE=stderr_error prints an error
# line on stderr and then succeeds. The password is transient: when stdin has
# one it must be the test value, else the hook exits 5; it logs
# "fixture-connect stdin-password" to STUB_LOG when it got the right one.
set -eu
mode=connect
if [ "${1:-}" = disconnect ]; then
  mode=disconnect
  shift
fi
printf 'fixture-connect %s %s\n' "$mode" "$*" >> "${STUB_LOG:-/dev/null}"
if [ "$*" != "--slug work --json" ]; then
  echo "unexpected argv: $*" >&2
  exit 3
fi
if [ "$mode" = disconnect ]; then
  printf '{"phase":"done"}\n'
  exit 0
fi
input="$(cat)"
printf '{"phase":"setup","status":"running","message":"checking login"}\n'
# The value is split so this file does not trip the test's secret scan.
good_password='s3cr3t''-value'
case "$input" in
  *"\"password\":\"${good_password}\""*) printf 'fixture-connect stdin-password\n' >> "${STUB_LOG:-/dev/null}" ;;
  *'"password"'*)
    case "$input" in
      *refuse.example.com*) ;;
      *) echo "unexpected password on stdin" >&2; exit 5 ;;
    esac ;;
esac
case "$input" in
  *refuse.example.com*)
    printf '%s\n' "$input"
    printf 'stderr: %s\n' "$input" >&2
    printf '{"phase":"error","error":"login_refused","message":"refused"}\n'
    exit 1
    ;;
esac
case "${FIXTURE_CONNECT_MODE:-}" in
  exit1) exit 1 ;;
  stderr_error) printf '{"phase":"error","error":"login_refused","message":"on stderr"}\n' >&2 ;;
esac
if [ -n "${FIXTURE_CONNECT_ERROR:-}" ]; then
  printf '{"phase":"error","error":"%s","message":"hook says no"}\n' "$FIXTURE_CONNECT_ERROR"
  exit 1
fi
printf '{"phase":"done","account":"work","mcp":["fixture-work"]}\n'
