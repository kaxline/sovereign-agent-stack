#!/bin/sh
# Xvfb, the VNC server and noVNC in the background, then the gateway, which
# owns Chromium (gateway/main.py) and exits when Chromium does, so compose
# restarts the whole service rather than leaving a gateway with no browser.
set -eu

DISPLAY_NUM="${WEB_BROWSER_DISPLAY:-:99}"
GEOMETRY="${WEB_BROWSER_SCREEN:-1280x900x24}"
VIEWER_PORT="${WEB_BROWSER_VIEWER_LISTEN:-6080}"
VIEWER_PASSWORD_FILE="${WEB_BROWSER_SECRETS_DIR:-/run/secrets}/viewer"

Xvfb "$DISPLAY_NUM" -screen 0 "$GEOMETRY" -nolisten tcp &

# The viewer runs only with a password: whoever holds it drives the browser
# with no method filter. Without the file, nothing listens and the agent side
# still works.
if [ -s "$VIEWER_PASSWORD_FILE" ]; then
  mkdir -p /tmp/viewer
  x11vnc -display "$DISPLAY_NUM" -localhost -rfbport 5900 -shared -forever -nopw \
    -passwdfile "rm:$VIEWER_PASSWORD_FILE" -quiet -bg -o /tmp/viewer/x11vnc.log
  websockify --web /usr/share/novnc "$VIEWER_PORT" 127.0.0.1:5900 &
else
  echo "web-browser: no viewer password in $VIEWER_PASSWORD_FILE; the viewer is off" >&2
fi

exec python3 -m gateway.main "$@"
