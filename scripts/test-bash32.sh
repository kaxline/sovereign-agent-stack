#!/bin/sh
# Host scripts under macOS's /bin/bash 3.2, which is what a packaged app or a
# machine without Homebrew's bash gets from `#!/usr/bin/env bash`. Runs a copy of
# the tracked files with a stub docker and a scratch HOME. Exits 0 without
# testing when there is no bash 3.x, unless BASH32_REQUIRED=1 (CI sets it).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASH32="${BASH32:-/bin/bash}"

# shellcheck disable=SC2016  # expanded by the bash under test
version="$("$BASH32" -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null || true)"
if [ "$version" != 3 ]; then
  if [ "${BASH32_REQUIRED:-0}" = 1 ]; then
    echo "error: ${BASH32} is not bash 3.x" >&2
    exit 1
  fi
  echo "skipped: ${BASH32} is not bash 3.x"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "${tmp}/bin" "${tmp}/home" "${tmp}/stack"

ln -s "$BASH32" "${tmp}/bin/bash"
printf '#!/bin/sh\nexit 0\n' > "${tmp}/bin/docker"
chmod +x "${tmp}/bin/docker"

cd "$ROOT"
git ls-files -z | xargs -0 tar -cf - | tar -xf - -C "${tmp}/stack"

cd "${tmp}/stack"
export HOME="${tmp}/home"
export PATH="${tmp}/bin:${PATH}"

fail=0
check() {
  label="$1"
  shift
  if "$@" > "${tmp}/out" 2>&1; then
    echo "ok: ${label}"
  else
    echo "FAIL: ${label}"
    tail -5 "${tmp}/out"
    fail=1
  fi
}

for script in scripts/*.sh scripts/lib/*.sh; do
  head -1 "$script" | grep -q '^#!/bin/sh' && continue
  check "parse ${script}" bash -n "$script"
done

for enabled in 0 1; do
  printf 'HERMES_BUZZ_ENABLED=%s\n' "$enabled" > .env
  check "sync-buzz-profile with HERMES_BUZZ_ENABLED=${enabled} and no profiles" \
    ./scripts/sync-buzz-profile.sh
done
printf 'HERMES_BUZZ_ENABLED=0\nBUZZ_RELAY_URL=ws://localhost:3000\n' > .env
check "sync-buzz-profile with a relay URL and no profiles" \
  ./scripts/sync-buzz-profile.sh
rm -f .env

check "setup.sh, default profiles" \
  ./scripts/setup.sh --non-interactive --workspace demo
rm -f .env
check "setup.sh, optional profiles" \
  ./scripts/setup.sh --non-interactive --workspace demo \
  --rag --automation --research --coding --calendar --db --todo --crm
check "setup.sh again over the same .env" \
  ./scripts/setup.sh --non-interactive --workspace demo \
  --rag --automation --research --coding --calendar --db --todo --crm
check "setup.sh --research turns on the flag and profile" \
  sh -c 'grep -qx "GPTR_MCP_ENABLED=1" .env && grep -E "^COMPOSE_PROFILES=" .env | grep -q ",research"'
check "setup.sh --crm turns on both Twenty flags and generates its secrets" \
  sh -c 'grep -qx "TWENTY_LOCAL_ENABLED=1" .env && grep -qx "TWENTY_MCP_ENABLED=1" .env \
    && grep -qx "TWENTY_MULTIWORKSPACE_BOOL=false" .env && grep -qE "^TWENTY_ENCRYPTION_KEY=.{40,}" .env \
    && grep -qE "^TWENTY_DB_PASSWORD=[0-9a-f]{64}$" .env'
printf 'x\n' > "${tmp}/key"
sed -i.bak -e 's|^TWENTY_ENCRYPTION_KEY=.*|TWENTY_ENCRYPTION_KEY=|' -e 's|^TWENTY_DB_PASSWORD=.*|TWENTY_DB_PASSWORD=|' .env
printf 'TWENTY_ENCRYPTION_KEY_FILE=%s\nTWENTY_DB_PASSWORD_FILE=%s\n' "${tmp}/key" "${tmp}/key" >> .env
check "setup.sh leaves Twenty secrets empty when their *_FILE is set" \
  sh -c './scripts/setup.sh --non-interactive --workspace demo >/dev/null 2>&1 && grep -qx "TWENTY_ENCRYPTION_KEY=" .env && grep -qx "TWENTY_DB_PASSWORD=" .env'
check "make ensure-local" make ensure-local
check "make ensure-local adds the crm profile and Twenty's data dirs" \
  sh -c 'grep -E "^COMPOSE_PROFILES=" .env | grep -q "crm" && test -d data/twenty/db && test -d data/twenty/storage'
check "crm.sh tests" ./scripts/test-crm-sh.sh
check "sync-profiles tests" ./scripts/test-sync-profiles.sh

exit "$fail"
