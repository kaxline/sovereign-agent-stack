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

# ASSISTANT_DATA_ROOT from the environment wins over .env, as it does for
# compose.
root="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-}")"
if ! err="$(validate_data_root_path "$root" 2>&1)"; then
  printf '[web-browser-secrets] warning: %s; no browser-service tokens created\n' "$err" >&2
  exit 0
fi

dir="${root}/web-browser/secrets"
if ! mkdir -p "$dir" 2> /dev/null || ! chmod 700 "$dir" 2> /dev/null; then
  printf "[web-browser-secrets] warning: could not create %s at 0700; run sudo chown -R \"\$(id -u):\$(id -g)\" '%s'\n" \
    "$dir" "${root}/web-browser" >&2
  exit 0
fi

for file in agent credentials viewer; do
  path="${dir}/${file}"
  if [[ -d "$path" ]]; then
    # A container started before this ran left a directory at the bind source.
    printf "[web-browser-secrets] warning: %s is a directory, so no token was written; stop the stack, remove it (sudo rmdir '%s') and run this again\n" \
      "$path" "$path" >&2
    continue
  fi
  if [[ ! -s "$path" ]]; then
    if ! (umask 077; openssl rand -hex 32 > "$path") 2> /dev/null; then
      printf '[web-browser-secrets] warning: could not write %s\n' "$path" >&2
      continue
    fi
    printf 'Created browser-service token %s\n' "$path"
  fi
  chmod 600 "$path" 2> /dev/null || true
done
