#!/usr/bin/env bash
# Write n8n's demo credentials (n8n/demo-data/credentials/*.json) when the
# automation profile is on. Does nothing when it is off, so installs without
# n8n need neither the n8n image nor the N8N_* keys.
#
#   scripts/sync-n8n-credentials.sh           write them if none exist yet
#   scripts/sync-n8n-credentials.sh --force   rewrite them (setup.sh, after
#                                             rotating a key)
#
# Called by make ensure-local (and so make up) and scripts/setup.sh, after
# sync-profiles.sh has set COMPOSE_PROFILES. n8n-import seeds these files into
# a new n8n store, so they must exist before the first make up with automation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

force=0
case "${1:-}" in
  "") ;;
  --force) force=1 ;;
  *) die "usage: $0 [--force]" ;;
esac

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

has_compose_profile automation || exit 0

missing=""
for key in N8N_ENCRYPTION_KEY N8N_USER_MANAGEMENT_JWT_SECRET LIGHTRAG_API_KEY; do
  [[ -n "$(env_get "$key")" ]] || missing="${missing} ${key}"
done
if [[ -n "$missing" ]]; then
  die "automation is on but${missing} not set in .env. Run ./scripts/setup.sh to generate them. If n8n already has a store, set N8N_ENCRYPTION_KEY to the key it was created with."
fi

CRED_DIR="n8n/demo-data/credentials"
if [[ "$force" -eq 0 ]] && compgen -G "${CRED_DIR}/*.json" >/dev/null; then
  exit 0
fi

# Node from the n8n image, which automation pulls anyway, so the host needs
# none. N8N_NODE=host uses the host's node instead (tests, development).
run_node() {
  if [[ "${N8N_NODE:-}" == host ]]; then
    command -v node >/dev/null 2>&1 \
      || die "N8N_NODE=host needs Node on PATH to write ${CRED_DIR}/*.json"
    node "$@"
    return
  fi
  local image
  image="$(env_get N8N_IMAGE)"
  stack_container --env N8N_ENCRYPTION_KEY --env LIGHTRAG_API_KEY \
    --env NEO4J_PASSWORD --env NEO4J_USERNAME \
    "${image:-$(compose_image_default N8N_IMAGE)}" node "$@"
}

N8N_ENCRYPTION_KEY="$(env_get N8N_ENCRYPTION_KEY)" \
LIGHTRAG_API_KEY="$(env_get LIGHTRAG_API_KEY)" \
NEO4J_PASSWORD="$(env_get NEO4J_PASSWORD)" \
NEO4J_USERNAME="$(env_get NEO4J_USERNAME)" \
  run_node scripts/generate-n8n-credentials.js
log "Wrote n8n demo credentials to ${CRED_DIR}"
