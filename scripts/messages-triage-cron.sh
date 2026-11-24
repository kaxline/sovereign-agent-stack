#!/usr/bin/env bash
# Set up the messages-triage Hermes cron job. See docs/messages.md.
#
#   scripts/messages-triage-cron.sh create [--schedule "every 30m"] [--deliver signal] [--prompt-file FILE]
#   scripts/messages-triage-cron.sh show
#   scripts/messages-triage-cron.sh remove
#
# The job runs on the default profile with the messages tools only: no web,
# browser, terminal or other MCP servers. New-message events fire it at once
# through the default profile's `messages` webhook route; the schedule is a
# safety net, and Hermes skips the model on those ticks unless the messages
# MCP's watermark shows unreported messages. Its report goes only to --deliver
# (default: signal when SIGNAL_HOME_CHANNEL is set, else local). `create`
# replaces an existing messages-triage job.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/triage-cron.sh
source "${ROOT}/scripts/lib/triage-cron.sh"

triage_cron_gate() {
  env_true MESSAGES_MCP_ENABLED || die "MESSAGES_MCP_ENABLED is off; run ./scripts/setup.sh --messages first"
  grep -qs 'messages-mcp:8000' data/hermes/config.yaml \
    || die "the default Hermes profile has no messages MCP; run make hermes-reload"
  grep -qs 'messages_triage_batch' data/hermes/config.yaml \
    || die "the default profile doesn't list messages_triage_batch; run make hermes-reload"
}
triage_cron_notes() {
  # shellcheck disable=SC2154 # triage_cron sets $schedule
  [[ -n "$(env_get MESSAGES_WEBHOOK_SECRET)" ]] \
    || printf 'messages-triage-cron: no MESSAGES_WEBHOOK_SECRET, so new messages only show up on the schedule (%s)\n' "$schedule" >&2
}

triage_cron messages-triage messages messages-mcp "every 30m" compose/hermes/messages-triage-prompt.md "$@"
