#!/bin/sh
# Which MAIL_MCP_ENABLED values mail-triage-cron.sh create accepts: the same
# set as sync-profiles.sh and addon_manifest.truthy (1, true, yes, on, any
# case). Runs a copy of the script in a scratch root whose .env is made up and
# which has no Hermes config, so an accepted flag stops at the next check.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "${tmp}/scripts"
cp "${ROOT}/scripts/mail-triage-cron.sh" "${tmp}/scripts/"

create() {
  printf 'MAIL_MCP_ENABLED=%s\n' "$1" > "${tmp}/.env"
  bash "${tmp}/scripts/mail-triage-cron.sh" create 2>&1 || true
}

for on in 1 true TRUE yes Yes on ON; do
  out="$(create "$on")"
  case "$out" in
    *"the default Hermes profile has no mail MCP"*) ;;
    *) fail "MAIL_MCP_ENABLED=${on} should pass the flag check ($out)" ;;
  esac
done
for off in 0 false off no ""; do
  out="$(create "$off")"
  case "$out" in
    *"MAIL_MCP_ENABLED is off"*) ;;
    *) fail "MAIL_MCP_ENABLED=${off} should be refused ($out)" ;;
  esac
done

printf 'OK mail-triage-cron flag values\n'
