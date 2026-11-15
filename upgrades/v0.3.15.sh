#!/usr/bin/env bash
# v0.3.15: cron fence on agent profiles, exact MCP tool lists, TODOS_KEY,
# CalDAV accounts in the add-on store. CHANGELOG.md "v0.3.15" has the detail.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Fences cron on agent profiles, writes the tool allowlists and TODOS_KEY,
# and applies the halt-stream overlay.
reload_hermes

# The bootstrap writes TODOS_KEY; the old key is read by nothing.
while IFS= read -r f; do
  remove_env "$f" VIKUNJA_MCP_TOKEN
done < <(hermes_env_files)

for f in "${UPGRADE_ROOT}"/compose/caldav-mcp/accounts/*.env; do
  [[ -f "$f" ]] || continue
  slug="$(basename "$f" .env)"
  needs_user "caldav-reconnect-${slug}" \
    "CalDAV account ${slug} moved to the caldav add-on's store and is not read any more. Connect it again with scripts/addon.sh connect caldav --instance ${slug} (url, username and password as JSON on stdin), then delete ${f}."
done

notice "Hermes cron jobs without their own enabled_toolsets no longer get the browser, mail, messages or add-ons without permissions.cron, on agent profiles too. Give such a job its own enabled_toolsets."
notice "The bootstraps now write tools.include exactly for every stack-registered MCP entry; hand edits to those lists are replaced."
