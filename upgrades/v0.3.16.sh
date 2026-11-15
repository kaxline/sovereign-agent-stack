#!/usr/bin/env bash
# v0.3.16: Bitwarden sign-in through bitwarden-signin. CHANGELOG.md
# "v0.3.16" has the detail.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

enabled BITWARDEN_ENABLED || exit 0

# The broker's image carries the sign-in API; an old image answers none of it.
rebuild bitwarden-broker

needs_user bitwarden-in-page-fill \
  "Bitwarden logins are no longer filled into the agent's own page. To keep that for an item, add a boolean custom field agent-in-page-fill set to true; other items now sign in through the bitwarden-signin browser."
