#!/bin/sh
# Shared uv cache for `uvx browser-use` (browser_exec), on a named volume.
#
# Each profile has its own HOME under data/hermes, so by default each kept its
# own uv cache on the virtiofs bind mount: one uvx run took ~57 s there even
# warm, against ~1 s on a native volume. UV_CACHE_DIR points every profile at
# one cache. This script makes it writable by the hermes user, then warms it in
# the background so the first browser call does not pay the ~40 s install.
set -u
CACHE="${UV_CACHE_DIR:-/opt/uv-cache}"
# Whatever uid cont-init 01 gave the hermes user (HERMES_UID).
U="$(id -u hermes)"
G="$(id -g hermes)"
OUT=/proc/1/fd/1

log() { echo "[11-warmup-uv-cache] $*"; }

mkdir -p "$CACHE"
# A fresh named volume is root-owned. Recurse only when the owner is wrong,
# so a large existing cache is not walked on every start.
if [ "$(stat -c %u "$CACHE")" != "$U" ]; then
  chown -R "$U:$G" "$CACHE" || log "WARN: cannot chown $CACHE"
fi

# Detached: cont-init must not wait on a download. Same command Hermes runs
# (tools/browser_use_cli.py), so the cached environment is the one it reuses.
[ -w "$OUT" ] || OUT=/dev/null
# shellcheck disable=SC2016  # expanded by the inner sh
setsid sh -c '
  cd /tmp || exit 0
  start=$(date +%s)
  if UV_CACHE_DIR="$1" HOME=/tmp /command/s6-setuidgid hermes /usr/local/bin/uvx browser-use --help \
      >/tmp/uv-cache-warmup.out 2>&1; then
    echo "[11-warmup-uv-cache] OK: browser-use cached in $1 ($(( $(date +%s) - start ))s)"
  else
    echo "[11-warmup-uv-cache] WARN: warmup failed; see /tmp/uv-cache-warmup.out"
  fi
' warmup "$CACHE" </dev/null >"$OUT" 2>&1 &
log "warming $CACHE in the background"
