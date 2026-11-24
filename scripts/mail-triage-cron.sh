#!/usr/bin/env bash
# Set up the mail-triage Hermes cron job. See docs/email.md.
#
#   scripts/mail-triage-cron.sh create [--schedule "every 15m"] [--deliver signal] [--prompt-file FILE]
#   scripts/mail-triage-cron.sh show
#   scripts/mail-triage-cron.sh remove
#
# The job runs on the default profile with the mail tools only: no web,
# browser, terminal, drafts or other MCP servers. Hermes checks the mail
# index's watermark first and skips the model unless newer mail was indexed.
# Its report goes only to --deliver (default: signal when SIGNAL_HOME_CHANNEL
# is set, else local). `create` replaces an existing mail-triage job.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/triage-cron.sh
source "${ROOT}/scripts/lib/triage-cron.sh"

triage_cron_gate() {
  env_true MAIL_MCP_ENABLED || die "MAIL_MCP_ENABLED is off; run ./scripts/setup.sh --mail first"
  grep -qs 'mail-mcp:8000' data/hermes/config.yaml \
    || die "the default Hermes profile has no mail MCP; re-run hermes-api-bootstrap and hermes-browser-bootstrap"
}
# The job's monitor polls this route; an old mail-mcp image doesn't have it,
# and every tick would then fail and send a failure notice.
triage_cron_ready() {
  docker compose exec -T hermes python3 -c 'import urllib.request as u; u.urlopen("http://mail-mcp:8000/watermark", timeout=10)' >/dev/null 2>&1 \
    || die "hermes can't reach http://mail-mcp:8000/watermark; rebuild mail-mcp with: docker compose up -d --build mail-mcp"
}

triage_cron mail-triage mail mail-mcp "every 15m" compose/hermes/mail-triage-prompt.md "$@"
