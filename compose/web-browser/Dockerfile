# The stack's browser provider: Debian's Chromium, headful on an Xvfb display,
# behind the CDP gateway in gateway/ (docs/contracts/browser.md). The viewer is
# x11vnc on the same display behind noVNC, so a person can sign in to a site
# once or finish a CAPTCHA the agent stopped at.
#
# Same base digest as compose/bitwarden-broker (a manifest list: arm64 and
# amd64). Debian's chromium is a security-supported package, so the Chromium
# version follows the base image rather than a pin of ours.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates chromium chromium-sandbox fonts-liberation novnc python3 python3-websockets \
       websockify x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
COPY gateway /app/gateway

# The root filesystem is read-only at run time. /tmp is a tmpfs; the Chromium
# profile is the web_browser_profile volume at /profile. Compose runs the
# service as HERMES_UID:HERMES_GID, which needs no passwd entry.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    WEB_BROWSER_CHROMIUM=/usr/bin/chromium \
    WEB_BROWSER_PROFILE_DIR=/profile \
    WEB_BROWSER_SECRETS_DIR=/run/secrets \
    WEB_BROWSER_DISPLAY=:99
WORKDIR /app
USER 1000:1000

ENTRYPOINT ["/app/entrypoint.sh"]
