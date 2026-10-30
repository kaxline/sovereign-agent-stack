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
  --rag --automation --coding --calendar --db --todo
check "setup.sh again over the same .env" \
  ./scripts/setup.sh --non-interactive --workspace demo \
  --rag --automation --coding --calendar --db --todo
check "make ensure-local" make ensure-local

exit "$fail"
