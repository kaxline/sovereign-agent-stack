#!/command/with-contenv sh
# Shared uv cache on a named volume, owned by the hermes user.
#
# Each profile has its own HOME under data/hermes, so by default each kept its
# own uv cache on the virtiofs bind mount, where uv is slow. UV_CACHE_DIR points
# every profile, and Hermes' package manager, at one cache on a native volume.
# This script makes it writable by the hermes user.
#
# Until Hermes v0.22.7 it also warmed `uvx browser-use` here for browser_exec.
# v0.22.7 runs browser_harness from its own venv and ships no uvx, so there is
# nothing to warm.
set -u
CACHE="${UV_CACHE_DIR:-/opt/uv-cache}"
# Whatever uid cont-init 01 gave the hermes user (HERMES_UID).
U="$(id -u hermes)"
G="$(id -g hermes)"

log() { echo "[11-warmup-uv-cache] $*"; }

mkdir -p "$CACHE"
# A fresh named volume is root-owned. Recurse only when the owner is wrong,
# so a large existing cache is not walked on every start.
if [ "$(stat -c %u "$CACHE")" != "$U" ]; then
  chown -R "$U:$G" "$CACHE" || log "WARN: cannot chown $CACHE"
fi
