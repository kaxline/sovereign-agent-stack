#!/usr/bin/env bash
# Stop `make up` early when automation is on and a key n8n needs is empty.
# docker-compose.yml can't require them with `:?`, since compose interpolates
# services whose profile is off. n8n-import encrypts its demo credentials with
# N8N_ENCRYPTION_KEY and LIGHTRAG_API_KEY when it seeds a new n8n store, and
# fails without them. Does nothing when automation is off.
#
# Called by make ensure-local (and so make up), after sync-profiles.sh has set
# COMPOSE_PROFILES.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

has_compose_profile automation || exit 0

missing=""
for key in N8N_ENCRYPTION_KEY N8N_USER_MANAGEMENT_JWT_SECRET LIGHTRAG_API_KEY; do
  [[ -n "$(env_get "$key")" ]] || missing="${missing} ${key}"
done
if [[ -n "$missing" ]]; then
  die "automation is on but${missing} not set in .env. Run ./scripts/setup.sh to generate them. If n8n already has a store, set N8N_ENCRYPTION_KEY to the key it was created with."
fi
