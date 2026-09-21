#!/usr/bin/env bash
# Create an independent Hermes agent profile (channel-agnostic).
# Seeds SOUL with three-tier memory guidance, registers the agent, and optionally
# attaches Buzz when HERMES_BUZZ_ENABLED=1 or WITH=buzz.
#
# Usage:
#   make agent-create NAME=software-engineer DISPLAY_NAME="Software Engineer"
#   ./scripts/agent-create.sh software-engineer "Software Engineer"
#   WITH=buzz ROLE="Implements features." ./scripts/agent-create.sh coder
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }

env_get() {
  local key="$1"
  local default="${2:-}"
  local val=""
  if [[ -f .env ]]; then
    val="$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
  fi
  if [[ -z "$val" ]]; then
    echo "$default"
  else
    echo "$val"
  fi
}

# shellcheck source=lib/agent-registry.sh
source "${ROOT}/scripts/lib/agent-registry.sh"

NAME="${1:-${NAME:-}}"
DISPLAY_NAME="${2:-${DISPLAY_NAME:-}}"
ROLE_BLURB="${ROLE:-${ROLE_BLURB:-Focus on your specialty; collaborate with humans and other agents in shared rooms and project files.}}"
WITH="${WITH:-}"

[[ -n "$NAME" ]] || die "Usage: $0 <agent-slug> [display-name]
  Example: $0 software-engineer \"Software Engineer\"
  Env: ROLE=… WITH=buzz"

case "$NAME" in
  *[!a-z0-9_-]*|"")
    die "NAME must be a lowercase slug (a-z, 0-9, _, -): got '${NAME}'"
    ;;
  browser|api-server|default)
    die "NAME '${NAME}' is reserved by this stack"
    ;;
esac

if [[ -z "$DISPLAY_NAME" ]]; then
  DISPLAY_NAME="$NAME"
fi

[[ -f .env ]] || die ".env missing — run ./scripts/setup.sh first"

if ! docker compose ps --status running hermes 2>/dev/null | grep -q hermes; then
  die "hermes container is not running — make up first"
fi

profile_dir="data/hermes/profiles/${NAME}"
if [[ -d "$profile_dir" ]]; then
  log "Profile directory already exists: ${profile_dir}"
else
  log "Creating Hermes profile '${NAME}'"
  docker compose exec -T hermes hermes profile create "$NAME"
fi

[[ -d "$profile_dir" ]] || die "expected ${profile_dir} after profile create"

# Independent agents keep their own memories/ — never the browser/api-server overlay.
mkdir -p "${profile_dir}/memories"

template="${ROOT}/compose/hermes/agent-soul.template.md"
soul="${profile_dir}/SOUL.md"
if [[ -f "$template" ]]; then
  if [[ ! -f "$soul" ]] || grep -q 'Hermes Agent' "$soul" 2>/dev/null; then
    # Escape sed replacement delimiters for display/role text.
    _esc() { printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'; }
    sed -e "s/{{DISPLAY_NAME}}/$(_esc "$DISPLAY_NAME")/g" \
        -e "s/{{NAME}}/$(_esc "$NAME")/g" \
        -e "s/{{ROLE_BLURB}}/$(_esc "$ROLE_BLURB")/g" \
        "$template" > "$soul"
    log "Wrote ${soul}"
  else
    log "Keeping existing ${soul}"
  fi
else
  die "missing template ${template}"
fi

printf '{"gateway_state":"running"}\n' > "${profile_dir}/gateway_state.json"
log "Marked gateway_state=running for '${NAME}'"

agent_registry_upsert "$NAME" "$DISPLAY_NAME" "$ROLE_BLURB"
agent_registry_sync_siblings

attach_buzz=0
case "$(echo "${WITH}" | tr '[:upper:]' '[:lower:]')" in
  buzz|1|true|yes) attach_buzz=1 ;;
esac

buzz_enabled="$(env_get HERMES_BUZZ_ENABLED 0)"
case "$(echo "$buzz_enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) attach_buzz=1 ;;
esac

if [[ "$attach_buzz" -eq 1 ]]; then
  log "Attaching Buzz transport for '${NAME}'…"
  DISPLAY_NAME="$DISPLAY_NAME" ./scripts/agent-attach-buzz.sh "$NAME"
else
  cat <<EOF

Agent ready (local only — under data/hermes/, not git):

  Name:         ${NAME}
  Display name: ${DISPLAY_NAME}
  Home:         ${profile_dir}
  Registry:     data/hermes/agents/registry.json

Memory tiers (see SOUL.md and docs/agents.md):
  Private:  ${profile_dir}/memories/ + this profile's sessions
  Project:  /opt/projects/
  Global:   /opt/memory/

Next steps:
  1. Edit ${profile_dir}/SOUL.md to refine the role
  2. Recreate Hermes so s6 registers the gateway:
       docker compose up -d --force-recreate hermes
  3. Optional rooms/DMs: make agent-create NAME=${NAME} WITH=buzz
     (or: ./scripts/agent-attach-buzz.sh ${NAME})

See docs/agents.md.
EOF
fi
