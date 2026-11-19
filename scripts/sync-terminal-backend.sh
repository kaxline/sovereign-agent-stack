#!/usr/bin/env bash
# Project coding and context roots onto the active terminal service.
# Called by make ensure-local and scripts/setup.sh.
#
# HERMES_TERMINAL_BACKEND=ssh (default) uses hermes-worker.
# HERMES_TERMINAL_BACKEND=local uses hermes and drops an empty hermes-worker block.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests may point SYNC_TERMINAL_ROOT at a temp directory with its own .env.
ROOT="${SYNC_TERMINAL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
# shellcheck source=lib/python.sh
source "$SCRIPT_DIR/lib/python.sh"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ ! -f .env ]]; then
  die "missing .env — run ./scripts/setup.sh"
fi

backend="$(env_get HERMES_TERMINAL_BACKEND)"
if [[ -z "$backend" ]]; then
  upsert_env .env HERMES_TERMINAL_BACKEND ssh
  backend=ssh
  log "Set HERMES_TERMINAL_BACKEND=ssh"
fi

case "$backend" in
  ssh|local) ;;
  *) die "HERMES_TERMINAL_BACKEND must be ssh or local (got ${backend})" ;;
esac

# The terminal service mounts an empty directory over this checkout's data/ at
# its host path, so a coding root that contains the repo can't reach Hermes
# state. Compose has no variable for the project directory, so record it.
# A value that still points at a stack checkout is kept, and a symlinked .env
# (a git worktree sharing main's) is left alone, so a worktree never repoints
# the mask away from main.
stack_dir="$(env_get ASSISTANT_STACK_DIR)"
if [[ ! -L .env ]] && [[ -z "$stack_dir" || ! -f "$stack_dir/docker-compose.yml" ]]; then
  upsert_env .env ASSISTANT_STACK_DIR "$ROOT"
  log "Set ASSISTANT_STACK_DIR=$ROOT"
fi

coding="$(env_get CODING_EXTRA_ROOTS)"
context="$(env_get CONTEXT_EXTRA_ROOTS)"
override="$ROOT/docker-compose.override.yml"

# The editor is piped in: inside the Hermes image only ROOT is mounted.
stack_python - sync "$override" "$backend" "$coding" "$context" \
  < "$SCRIPT_DIR/lib/override_mounts.py"
