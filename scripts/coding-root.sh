#!/usr/bin/env bash
# Manage shared coding roots for Hermes + OpenCode.
#
# Primary root: OPENCODE_WORKSPACE_HOST (mounted on the terminal backend and opencode).
# Extra roots: colon-prefixed entries in CODING_EXTRA_ROOTS (.env) plus matching
# bind mounts under the terminal service (hermes-worker, or hermes when
# HERMES_TERMINAL_BACKEND=local) and services.opencode in
# docker-compose.override.yml (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
ENV_FILE="${ROOT}/.env"
OVERRIDE="${ROOT}/docker-compose.override.yml"

die() { echo "error: $*" >&2; exit 1; }

# shellcheck source=lib/roots.sh
source "${ROOT}/scripts/lib/roots.sh"

path_covered() {
  local candidate="$1"
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  [[ -n "$primary" ]] || return 1
  if [[ "$candidate" == "$primary" || "$candidate" == "$primary"/* ]]; then
    return 0
  fi
  local IFS=':'
  local root
  # shellcheck disable=SC2086
  for root in $extras; do
    [[ -z "$root" ]] && continue
    if [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]; then
      return 0
    fi
  done
  return 1
}

# recreate_services: what to recreate after a coding root change. The binds sit
# on the terminal service and opencode; CODING_EXTRA_ROOTS feeds opencode-mcp's
# root check and hermes' HERMES_WRITE_SAFE_ROOT, which write_file and patch
# check even when the shell runs on hermes-worker.
recreate_services() {
  local service
  service="$(terminal_service)"
  if [[ "$service" == hermes ]]; then
    printf '%s\n' "hermes opencode opencode-mcp"
  else
    printf '%s\n' "${service} hermes opencode opencode-mcp"
  fi
}

cmd_list() {
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST '(unset)')"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  echo "Primary coding root (OPENCODE_WORKSPACE_HOST):"
  echo "  $primary"
  echo "Extra coding roots (CODING_EXTRA_ROOTS):"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    echo "  (none)"
  else
    local IFS=':'
    local root
    # shellcheck disable=SC2086
    for root in $extras; do
      [[ -z "$root" ]] && continue
      echo "  $root"
    done
  fi
  if [[ -f "$OVERRIDE" ]]; then
    echo "Override file: $OVERRIDE"
  else
    echo "Override file: (missing — will be created on add)"
  fi
}

cmd_add() {
  local path="${1:-}"
  validate_path "$path"
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — run ./scripts/setup.sh first"

  local primary service
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  [[ -n "$primary" ]] || die "OPENCODE_WORKSPACE_HOST is unset"
  service="$(terminal_service)"

  if path_covered "$path"; then
    echo "Already covered by an existing coding root: $path"
    cmd_list
    return 0
  fi

  local extras
  extras="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    extras=":$path"
  else
    case ":${extras#:}:" in
      *:"$path":*) echo "Already in CODING_EXTRA_ROOTS: $path"; cmd_list; return 0 ;;
    esac
    extras="${extras}:$path"
  fi
  [[ "$extras" == :* ]] || extras=":$extras"

  upsert_env "$ENV_FILE" CODING_EXTRA_ROOTS "$extras"
  ensure_override
  add_mount "$service" "$path"
  add_mount opencode "$path"

  echo
  echo "Added coding root: $path"
  echo "CODING_EXTRA_ROOTS=$extras"
  echo
  echo "Recreate containers so the new binds and HERMES_WRITE_SAFE_ROOT apply:"
  echo "  docker compose up -d --force-recreate $(recreate_services)"
  echo "  # or: make down && make up"
  echo
  echo "If this tree has .env / credential files, shadow them (see docs/opencode.md):"
  echo "  find \"$path\" -name '.env' -not -path '*/node_modules/*'"
}

usage() {
  cat <<EOF
Usage: $0 <list|add DIR>

  list          Show primary and extra coding roots
  add DIR       Grant Hermes + OpenCode access to an absolute directory

EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    list) cmd_list ;;
    add)
      shift
      cmd_add "${1:-}"
      ;;
    -h|--help|help) usage ;;
    "") usage; exit 1 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
