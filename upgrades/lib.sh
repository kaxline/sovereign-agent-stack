# shellcheck shell=bash
# Helpers for upgrade hooks (upgrades/vX.Y.Z.sh). A hook sources this file,
# runs from the stack root after the new version's files and setup.sh, and
# before the stack starts. It asks for work after the start, and reports what
# the user must do, by printing directives on stdout; logs go to stderr.
#
#   rebuild SERVICE        rebuild SERVICE's image and recreate it
#   reload_hermes          run both bootstraps forced and recreate hermes
#   needs_user ID MESSAGE  something the upgrade can't do for the user
#   notice MESSAGE         a behaviour change worth reading, no action needed
#
# scripts/upgrade.sh runs the hooks and acts on the directives. Hooks must be
# idempotent: an upgrade that fails halfway runs them again.

UPGRADE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib/env.sh
source "${UPGRADE_ROOT}/scripts/lib/env.sh"

rebuild() { printf 'rebuild\t%s\n' "$1"; }
reload_hermes() { printf 'reload_hermes\n'; }
needs_user() { printf 'needs_user\t%s\t%s\n' "$1" "$2"; }
notice() { printf 'notice\t%s\n' "$1"; }

# enabled FLAG: whether .env turns FLAG on (env_true: 1, true, yes, on).
enabled() { env_true "$1"; }

# hermes_env_files: the default profile's .env and each named profile's.
hermes_env_files() {
  local home="${UPGRADE_ROOT}/data/hermes" f
  [[ -f "${home}/.env" ]] && printf '%s\n' "${home}/.env"
  for f in "${home}"/profiles/*/.env; do
    [[ -f "$f" ]] && printf '%s\n' "$f"
  done
  return 0
}
