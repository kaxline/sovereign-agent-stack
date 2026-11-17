#!/usr/bin/env bash
# scripts/web-browser-secrets.sh: creates the three browser-service tokens under
# ASSISTANT_DATA_ROOT (where compose mounts them), at 0600 in a 0700 folder,
# keeps existing ones, warns about a directory at a token's path, and runs from
# both setup.sh and make ensure-local before anything starts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

data="${tmp}/data"
dir="${data}/web-browser/secrets"
ASSISTANT_DATA_ROOT="$data" "${ROOT}/scripts/web-browser-secrets.sh" > /dev/null 2> "${tmp}/err" \
  || fail "web-browser-secrets.sh exits 0"
[ ! -s "${tmp}/err" ] || fail "no warnings on a fresh root: $(cat "${tmp}/err")"
[ "$(mode_of "$dir")" = 700 ] || fail "${dir} is 0700"
for f in agent credentials viewer; do
  [ -f "${dir}/${f}" ] || fail "creates ${f}"
  [ "$(mode_of "${dir}/${f}")" = 600 ] || fail "${f} is 0600"
  grep -qE '^[0-9a-f]{64}$' "${dir}/${f}" || fail "${f} holds 32 random bytes as hex"
done
[ "$(cat "${dir}/agent")" != "$(cat "${dir}/credentials")" ] || fail "each client gets its own token"

# Existing tokens stay; an empty file is filled.
first="$(cat "${dir}/agent")"
: > "${dir}/viewer"
ASSISTANT_DATA_ROOT="$data" "${ROOT}/scripts/web-browser-secrets.sh" > /dev/null || fail "a second run exits 0"
[ "$(cat "${dir}/agent")" = "$first" ] || fail "keeps an existing token"
[ -s "${dir}/viewer" ] || fail "fills an empty token file"

# A directory at a token's path (Docker's missing bind source) is warned about, exit 0.
other="${tmp}/other"
mkdir -p "${other}/web-browser/secrets/credentials"
ASSISTANT_DATA_ROOT="$other" "${ROOT}/scripts/web-browser-secrets.sh" > /dev/null 2> "${tmp}/err" \
  || fail "a directory at a token's path still exits 0"
grep -q 'credentials is a directory' "${tmp}/err" || fail "a directory at a token's path is warned about"
[ -s "${other}/web-browser/secrets/agent" ] || fail "the other tokens are still written"

# A data root inside ./data/hermes is refused with a warning, exit 0.
ASSISTANT_DATA_ROOT="${ROOT}/data/hermes/nope" "${ROOT}/scripts/web-browser-secrets.sh" 2> "${tmp}/err" \
  || fail "a refused data root still exits 0"
grep -q 'no browser-service tokens created' "${tmp}/err" || fail "a refused data root is warned about"
[ ! -e "${ROOT}/data/hermes/nope" ] || fail "nothing is created under a refused root"

# Both entry points run it, and make ensure-local before --prepare stops.
awk '/web-browser-secrets.sh/ { a = NR } /PREPARE_ONLY" == 1/ { p = NR } END { exit !(a && p && a < p) }' \
  "${ROOT}/scripts/up.sh" || fail "scripts/up.sh runs web-browser-secrets.sh before --prepare stops"
grep -q '^\./scripts/web-browser-secrets\.sh$' "${ROOT}/scripts/setup.sh" \
  || fail "scripts/setup.sh runs web-browser-secrets.sh"
! grep -q 'WEB_BROWSER_DATA_DIR' "${ROOT}/scripts/setup.sh" \
  || fail "setup.sh must not write the tokens outside ASSISTANT_DATA_ROOT"

echo "OK web-browser-secrets.sh"
