#!/usr/bin/env bash
# Creates the browser service's client tokens (docs/contracts/browser.md) in
# <data root>/web-browser/secrets: agent, credentials and viewer, one file per
# client, mode 0600 in a 0700 folder. hermes and bitwarden-broker bind-mount
# two of these files whatever WEB_BROWSER_ENABLED says, and Docker creates a
# missing bind source as a directory (owned by root on Linux), which then
# blocks the file from being written. setup.sh and make ensure-local run this.
# A file that exists and is not empty is kept, so a token survives a re-run.
# A path it can't write is warned about, not fatal.
#
#   ./scripts/web-browser-secrets.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/secret.sh
source "${ROOT}/scripts/lib/secret.sh"

# ASSISTANT_DATA_ROOT from the environment wins over .env, as it does for
# compose.
root="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-}")"
if ! err="$(validate_data_root_path "$root" 2>&1)"; then
  printf '[web-browser-secrets] warning: %s; no browser-service tokens created\n' "$err" >&2
  exit 0
fi

warn() { printf '[web-browser-secrets] warning: %s\n' "$1" >&2; }
# shellcheck disable=SC2034 # read by lib/secret.sh
SECRET_FILE_FIX="run sudo chown -R \"\$(id -u):\$(id -g)\" '${root}/web-browser'"

dir="${root}/web-browser/secrets"
ensure_secret_dir "$dir" warn || exit 0

# A directory at a token's path (a container started before this ran left one
# at the bind source) is warned about and skipped.
for file in agent credentials viewer; do
  path="${dir}/${file}"
  ensure_secret_file "$path" warn || continue
  if [[ "$SECRET_FILE_CREATED" == 1 ]]; then
    printf 'Created browser-service token %s\n' "$path"
  fi
done
