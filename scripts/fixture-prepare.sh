#!/usr/bin/env bash
# Prepare hook for scripts/fixtures/addons-cli/hooked, used by
# scripts/test-addon-cli.sh. Logs its argv to STUB_LOG and the flag it sees,
# so the test can tell it ran before step flags. FIXTURE_PREPARE_FAIL=1 prints
# an error line and exits 1.
set -eu
printf 'prepare %s flag=%s\n' "$*" "$(grep -E '^HOOKED_ADDON_ENABLED=' .env | cut -d= -f2-)" >> "${STUB_LOG:-/dev/null}"
if [ "${FIXTURE_PREPARE_FAIL:-0}" = 1 ]; then
  printf '{"phase":"error","error":"command_failed","message":"prepare says no"}\n'
  exit 1
fi
