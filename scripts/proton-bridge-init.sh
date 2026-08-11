#!/usr/bin/env bash
# Idempotent Proton Mail Bridge account setup (interactive login on first run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[proton-bridge] %s\n' "$1"; }
die() { printf '[proton-bridge] ERROR: %s\n' "$1" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_cmd docker
docker compose version >/dev/null 2>&1 || die "docker compose is not available"

if ! docker info >/dev/null 2>&1; then
  die "Docker daemon is not running"
fi

[[ -f .env ]] || die "Missing .env — run ./scripts/setup.sh --mail first"

# shellcheck disable=SC1091
source .env

profiles="${COMPOSE_PROFILES:-}"
if [[ ",${profiles}," != *",mail,"* ]]; then
  die "COMPOSE_PROFILES does not include 'mail' — run ./scripts/setup.sh --mail"
fi

[[ -n "${PROTON_BRIDGE_IMAGE:-}" ]] || die "PROTON_BRIDGE_IMAGE is not set — run ./scripts/setup.sh --mail"

if pgrep -if 'proton.?mail.?bridge' >/dev/null 2>&1; then
  log "WARNING: Proton Mail Bridge may be running on the host (Mac app)."
  log "         Quit the Mac Bridge app before continuing — only one instance can run."
fi

compose_mail() {
  docker compose --profile mail "$@"
}

mkdir -p compose/protonmail-bridge
if [[ ! -f compose/protonmail-bridge/credentials.env ]]; then
  cp compose/protonmail-bridge/credentials.env.example compose/protonmail-bridge/credentials.env
fi

if compose_mail run --rm --entrypoint test protonmail-bridge -d /root/.password-store 2>/dev/null; then
  log "Bridge volume already initialized — skipping interactive login"
else
  log "Starting interactive Bridge setup..."
  log "In the Bridge CLI: run 'login', complete 2FA, then 'info' to verify, then 'exit'."
  log "Optional: run 'updates check off' (or similar) if offered, to discourage auto-updates."
  compose_mail run --rm -it protonmail-bridge init
fi

"$ROOT/scripts/sync-proton-bridge-credentials.sh"

cat <<'EOF'

Proton Mail Bridge init complete.

Next steps:
  1. docker compose up -d          (if COMPOSE_PROFILES=mail is in .env)
     — or —
     docker compose --profile mail up -d

  2. In n8n, use the pre-imported "Proton Mail (Bridge)" IMAP credential
     on Email Trigger / IMAP nodes.

Verify connectivity from n8n:
  docker exec -it n8n sh -c 'nc -zv protonmail-bridge 143'

EOF
