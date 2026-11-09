#!/usr/bin/env bash
# onToggle.script of scripts/fixtures/addons-cli/toggled, used by scripts/test-addon-cli.sh.
# Logs its argv to the file STUB_LOG names; FIXTURE_FOLLOW_FAIL=1 makes it exit 3.
set -eu
printf 'fixture-follow %s\n' "$*" >> "${STUB_LOG:-/dev/null}"
[ "${FIXTURE_FOLLOW_FAIL:-0}" = 1 ] && exit 3
exit 0
