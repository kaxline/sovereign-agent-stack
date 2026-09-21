#!/command/with-contenv bash
# Start a localhost→host.docker.internal TCP proxy for the Buzz relay so
# agent profiles can keep BUZZ_RELAY_URL=ws://localhost:3000 (community host
# must match Desktop) while still reaching the host-side `just relay`.
set -euo pipefail

log() { echo "[buzz-localhost-proxy] $*"; }

# Only relevant when Buzz is enabled and the configured relay is loopback.
enabled="${HERMES_BUZZ_ENABLED:-0}"
case "$(echo "$enabled" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) ;;
  *)
    log "HERMES_BUZZ_ENABLED off — skip"
    exit 0
    ;;
esac

relay="${BUZZ_RELAY_URL:-}"
case "$relay" in
  *localhost*|*127.0.0.1*) ;;
  *)
    log "BUZZ_RELAY_URL is not loopback (${relay:-empty}) — skip"
    exit 0
    ;;
esac

# Extract port from ws://host:port (default 3000).
port=3000
if [[ "$relay" =~ :([0-9]+)(/|$) ]]; then
  port="${BASH_REMATCH[1]}"
fi

export BUZZ_LOCAL_PROXY_PORT="$port"
export BUZZ_LOCAL_PROXY_UPSTREAM_PORT="$port"

# Already listening (recreate / re-run)?
if (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1; then
  log "127.0.0.1:${port} already accepting — skip"
  exit 0
fi

proxy_py="/bootstrap/buzz-localhost-proxy.py"
if [[ ! -f "$proxy_py" ]]; then
  log "missing ${proxy_py} — skip"
  exit 0
fi

# Probe upstream before backgrounding.
if ! python3 - <<PY
import socket, sys
port = int("${port}")
try:
    s = socket.create_connection(("host.docker.internal", port), timeout=2)
    s.close()
except OSError as e:
    print(e)
    sys.exit(1)
PY
then
  log "host.docker.internal:${port} unreachable — skip (is make buzz-relay-start running?)"
  exit 0
fi

log "starting proxy 127.0.0.1:${port} → host.docker.internal:${port}"
# Background under s6 so cont-init can finish; logs go to container stdout via
# the main process namespace (good enough for doctor / docker logs).
python3 "$proxy_py" >>/var/log/buzz-localhost-proxy.log 2>&1 &
echo $! >/var/run/buzz-localhost-proxy.pid
sleep 0.3
if (echo >/dev/tcp/127.0.0.1/"$port") >/dev/null 2>&1; then
  log "proxy up (pid $(cat /var/run/buzz-localhost-proxy.pid))"
else
  log "proxy failed to bind — see /var/log/buzz-localhost-proxy.log"
fi
