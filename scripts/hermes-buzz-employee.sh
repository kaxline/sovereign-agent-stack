#!/usr/bin/env bash
# Compatibility wrapper: create an independent agent and attach Buzz.
# Prefer: make agent-create NAME=<slug> WITH=buzz
#
# Usage:
#   make hermes-buzz-employee PROFILE=software-engineer DISPLAY_NAME="Software Engineer"
#   ./scripts/hermes-buzz-employee.sh software-engineer "Software Engineer"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAME="${1:-${PROFILE:-${NAME:-}}}"
DISPLAY_NAME="${2:-${DISPLAY_NAME:-}}"

export NAME
export DISPLAY_NAME
export ROLE="${ROLE:-${ROLE_BLURB:-}}"
export WITH=buzz

if [[ -n "$ROLE" ]]; then
  export ROLE_BLURB="$ROLE"
fi

exec ./scripts/agent-create.sh "$NAME" "$DISPLAY_NAME"
