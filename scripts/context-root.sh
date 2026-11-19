#!/usr/bin/env bash
# Manage read-only context roots for Hermes file context (not OpenCode).
#
# Extra roots: colon-prefixed entries in CONTEXT_EXTRA_ROOTS (.env) plus matching
# read-only bind mounts under the terminal service (hermes-worker, or hermes when
# HERMES_TERMINAL_BACKEND=local) in docker-compose.override.yml
# (gitignored). Paths already covered by OPENCODE_WORKSPACE_HOST or
# CODING_EXTRA_ROOTS are treated as visible (writable — coding-root wins).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
ENV_FILE="${ROOT}/.env"
OVERRIDE="${ROOT}/docker-compose.override.yml"

die() { echo "error: $*" >&2; exit 1; }

# shellcheck source=lib/roots.sh
source "${ROOT}/scripts/lib/roots.sh"

# True if candidate is under any already-mounted coding or context root.
path_visible() {
  local candidate="$1"
  local primary extras context
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  context="$(env_get CONTEXT_EXTRA_ROOTS)"
  if [[ -n "$primary" && ( "$candidate" == "$primary" || "$candidate" == "$primary"/* ) ]]; then
    return 0
  fi
  local IFS=':'
  local root
  # shellcheck disable=SC2086
  for root in $extras $context; do
    [[ -z "$root" ]] && continue
    if [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]; then
      return 0
    fi
  done
  return 1
}

path_covered_by_coding() {
  local candidate="$1"
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -n "$primary" && ( "$candidate" == "$primary" || "$candidate" == "$primary"/* ) ]]; then
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

cmd_list() {
  local extras
  extras="$(env_get CONTEXT_EXTRA_ROOTS)"
  echo "Context roots (CONTEXT_EXTRA_ROOTS, Hermes read-only):"
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
  echo "Also visible via coding roots (writable — coding-root wins):"
  echo "  primary: $(env_get OPENCODE_WORKSPACE_HOST '(unset)')"
  local coding
  coding="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -z "$coding" || "$coding" == ":" ]]; then
    echo "  extras: (none)"
  else
    echo "  extras: $coding"
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
  local service
  service="$(terminal_service)"

  if path_covered_by_coding "$path"; then
    echo "Already visible via a coding root (writable): $path"
    echo "Prefer a dedicated context root for notes vaults you do not want the coding agent to edit."
    cmd_list
    return 0
  fi

  if path_visible "$path"; then
    echo "Already a context root: $path"
    cmd_list
    return 0
  fi

  local extras
  extras="$(env_get CONTEXT_EXTRA_ROOTS)"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    extras=":$path"
  else
    case ":${extras#:}:" in
      *:"$path":*) echo "Already in CONTEXT_EXTRA_ROOTS: $path"; cmd_list; return 0 ;;
    esac
    extras="${extras}:$path"
  fi
  [[ "$extras" == :* ]] || extras=":$extras"

  upsert_env "$ENV_FILE" CONTEXT_EXTRA_ROOTS "$extras"
  ensure_override
  add_mount "$service" "$path" --ro

  echo
  echo "Added context root: $path"
  echo "CONTEXT_EXTRA_ROOTS=$extras"
  echo
  echo "Recreate the terminal backend so the new read-only bind applies:"
  echo "  docker compose up -d --force-recreate $(terminal_service)"
  echo "  # or: make down && make up"
  echo
  echo "Then declare the path in the project's sources.yaml and run:"
  echo "  make project-index PROJECT=<slug>"
}

usage() {
  cat <<EOF
Usage: $0 <list|add DIR>

  list          Show Hermes context roots (read-only notes mounts)
  add DIR       Mount an absolute directory into the terminal backend read-only

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
