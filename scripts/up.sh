#!/usr/bin/env bash
# Prepare local config and start the stack: what `make up` runs, for hosts
# without make (scripts/install.sh, scripts/upgrade.sh).
#
#   scripts/up.sh [--prepare] [--build SERVICE]... [--reload-hermes]
#
# --prepare     only create the gitignored local configs and sync profiles
#               (`make ensure-local`), and start nothing
# --build S     rebuild service S's image and recreate it after the start
# --reload-hermes
#               after the start, run both Hermes bootstraps with
#               HERMES_BOOTSTRAP_FORCE=1 and recreate hermes (`make
#               hermes-reload`)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

PREPARE_ONLY=0
RELOAD=0
builds=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare) PREPARE_ONLY=1; shift ;;
    --build) builds+=("$2"); shift 2 ;;
    --reload-hermes) RELOAD=1; shift ;;
    *) printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done

copy_example() {
  [[ -f "$1" ]] && return 0
  cp "$2" "$1"
  [[ -z "${3:-}" ]] || printf '%s\n' "$3"
}

copy_example searxng/settings.local.yml searxng/settings.yml
copy_example opencode/opencode.local.json opencode/opencode.json
copy_example compose/hermes/api-server.env compose/hermes/api-server.env.example \
  "Created compose/hermes/api-server.env — set API_SERVER_KEY"
copy_example compose/hermes/browser.env compose/hermes/browser.env.example \
  "Created compose/hermes/browser.env — set API_SERVER_KEY"
mkdir -p compose/postgres/services
./scripts/addon-dirs.sh
./scripts/web-browser-secrets.sh
./scripts/vault.sh api-token
./scripts/todo.sh ensure
./scripts/crm.sh ensure
./scripts/budget.sh ensure
./scripts/sync-signal-profile.sh
./scripts/sync-buzz-profile.sh
./scripts/sync-terminal-backend.sh
./scripts/sync-profiles.sh
./scripts/sync-n8n-credentials.sh

[[ "$PREPARE_ONLY" == 1 ]] && exit 0

echo "Profiles: $(env_get_file .env COMPOSE_PROFILES)"
./scripts/vault.sh pre-up
# A one-shot service that exits non-zero (a bootstrap, say) fails the whole
# `up` with only its name; print the end of its log, which says why.
if ! docker compose up -d; then
  docker compose ps -a --status exited --format '{{.Service}} {{.ExitCode}}' 2> /dev/null \
    | while read -r svc code; do
      [[ -n "$svc" && "$code" != 0 ]] || continue
      echo "--- ${svc} exited ${code}; the end of its log:" >&2
      docker compose logs --no-color --no-log-prefix --tail 15 "$svc" >&2 || true
    done
  exit 1
fi
./scripts/vault.sh up-hook

if [[ ${#builds[@]} -gt 0 ]]; then
  docker compose up -d --build "${builds[@]}"
fi

if [[ "$RELOAD" == 1 ]]; then
  docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap
  docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap
  docker compose up -d --no-deps --force-recreate hermes
fi
