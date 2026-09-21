#!/bin/sh
# Pre-warm agent-browser in every Hermes profile npm cache so runtime tool
# calls never hit a cold `npx -y` reify (ECOMPROMISED on Docker bind mounts).
set -u

SPEC="${AGENT_BROWSER_NPX_SPEC:-agent-browser@0.26.0}"
LOCK_DIR="/opt/data/.agent-browser-warmup"
LOCK_FILE="${LOCK_DIR}/warmup.lock"
MARKER="${LOCK_DIR}/warmed-${SPEC}"

log() { echo "[08-warmup-agent-browser] $*"; }

mkdir -p "$LOCK_DIR"

# Stale rename leftovers from interrupted npx reify.
cleanup_npx_trees() {
  find /opt/data -path '*/.npm/_npx/*/node_modules/.agent-browser-*' -type d 2>/dev/null \
    | while read -r d; do
        log "Removing stale npx leftover: $d"
        rm -rf "$d" || true
      done
}

warmup_one() {
  home="$1"
  mkdir -p "$home/.npm"
  log "Warming $SPEC under HOME=$home"
  # Prefer-offline after first fetch; ignore-scripts matches Hermes browser_tool.
  if HOME="$home" npm exec --ignore-scripts --yes -- "$SPEC" --help >/tmp/agent-browser-warmup.out 2>&1; then
    log "OK: $home"
    return 0
  fi
  log "WARN: warmup failed for $home (first 20 lines):"
  head -n 20 /tmp/agent-browser-warmup.out 2>/dev/null || true
  return 1
}

(
  flock -w 180 9 || { log "Could not acquire warmup lock"; exit 0; }
  cleanup_npx_trees
  if [ -f "$MARKER" ]; then
    log "Already warmed ($MARKER); refreshing help once"
  fi
  ok=0
  # Default + named profiles that run gateways with browser tools.
  for home in \
    /opt/data \
    /opt/data/profiles/browser/home \
    /opt/data/profiles/api-server/home \
    /opt/data/profiles/software-engineer/home
  do
    [ -d "$(dirname "$home")" ] || [ "$home" = "/opt/data" ] || continue
    if warmup_one "$home"; then ok=$((ok + 1)); fi
  done
  if [ "$ok" -gt 0 ]; then
    echo "$SPEC $(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$MARKER"
    log "Warmed $ok home(s)"
  else
    log "WARN: no successful warmups — browser_exec may still fail until npm lock settles"
  fi
) 9>"$LOCK_FILE"

exit 0
