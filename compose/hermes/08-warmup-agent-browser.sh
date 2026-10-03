#!/bin/sh
# Install agent-browser onto the container filesystem.
#
# The native binary under data/hermes (a Docker Desktop virtiofs bind mount)
# is mode 644. access() on that mount reports it executable, so the CLI's
# own chmod never runs and exec returns EACCES. /opt/hermes is not that mount.
# Hermes resolves agent-browser on PATH before npx; /opt/hermes/bin is first.
set -u

SPEC="${AGENT_BROWSER_NPX_SPEC:-agent-browser@0.26.0}"
PREFIX="/opt/hermes/agent-browser-cli"
BIN_LINK="/opt/hermes/bin/agent-browser"
PKG_BIN="${PREFIX}/node_modules/agent-browser/bin"
JS="${PKG_BIN}/agent-browser.js"
LOCK_DIR="/opt/data/.agent-browser-warmup"
LOCK_FILE="${LOCK_DIR}/warmup.lock"

log() { echo "[08-warmup-agent-browser] $*"; }

probe() {
  su -s /bin/sh hermes -c "$BIN_LINK --version" >/tmp/agent-browser-probe.out 2>&1
}

link_cli() {
  mkdir -p /opt/hermes/bin
  if [ ! -f "$JS" ]; then
    log "WARN: missing $JS"
    return 1
  fi
  # The JS entrypoint execs the native binary beside it. chmod here sticks;
  # the copies under data/hermes are left alone.
  chmod a+x "$JS" || true
  find "$PKG_BIN" -name 'agent-browser-linux-*' -type f -exec chmod a+x {} +
  ln -sfn "$JS" "$BIN_LINK"
}

install_cli() {
  mkdir -p "$PREFIX" /opt/data/.npm
  if npm install \
    --prefix "$PREFIX" \
    --ignore-scripts \
    --cache /opt/data/.npm \
    --prefer-offline \
    -- "$SPEC" >/tmp/agent-browser-install.out 2>&1; then
    return 0
  fi
  log "WARN: npm install of $SPEC failed (first 20 lines):"
  head -n 20 /tmp/agent-browser-install.out 2>/dev/null || true
  return 1
}

mkdir -p "$LOCK_DIR"

(
  flock -w 180 9 || { log "Could not acquire install lock"; exit 0; }
  if probe; then
    log "OK: $BIN_LINK"
    exit 0
  fi
  log "Installing $SPEC under $PREFIX"
  if ! install_cli; then
    log "WARN: agent-browser stays on the npx fallback until install succeeds"
    exit 0
  fi
  if ! link_cli; then
    exit 0
  fi
  if probe; then
    log "OK: $BIN_LINK"
  else
    log "WARN: agent-browser --version failed as hermes (first 20 lines):"
    head -n 20 /tmp/agent-browser-probe.out 2>/dev/null || true
  fi
) 9>"$LOCK_FILE"

exit 0
