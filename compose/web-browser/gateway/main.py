"""Start Chromium, then serve the CDP gateway in front of it.

Tokens are files in WEB_BROWSER_SECRETS_DIR, one per client: ``agent`` for the
agent runtime (path ``/devtools/browser/stack``) and ``credentials`` for the
credential backend (``/devtools/browser/credentials``). A missing or short file
leaves that client off, so the broker's token is not needed to run the browser.
With neither file the gateway exits: nothing could connect.

The process exits when Chromium exits, so compose restarts the container as a
whole rather than leaving a gateway in front of nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from . import chromium, rules
from .server import Gateway

LOG = logging.getLogger("web-browser")

# A token is 32 bytes of hex in setup.sh; anything under this is a placeholder.
MIN_TOKEN = 32

TOKEN_FILES = {"stack": "agent", "credentials": "credentials"}


def _listen(raw: str) -> tuple[str, int]:
    host, _, port = raw.strip().rpartition(":")
    try:
        number = int(port)
    except ValueError:
        number = -1
    if not host or not 0 <= number <= 65535:
        raise SystemExit("WEB_BROWSER_LISTEN must be an address and port, such as 0.0.0.0:9222")
    return host.strip("[]"), number


def tokens(secrets_dir: Path) -> dict:
    out = {}
    for name, filename in TOKEN_FILES.items():
        path = secrets_dir / filename
        try:
            value = path.read_text().strip()
        except OSError:
            LOG.info("%s: no token file at %s, that client is off", name, path)
            continue
        if len(value) < MIN_TOKEN:
            LOG.warning("%s: the token in %s is shorter than %d characters, that client is off",
                        name, path, MIN_TOKEN)
            continue
        out[name] = value
    return out


async def run() -> int:
    env = os.environ
    host, port = _listen(env.get("WEB_BROWSER_LISTEN") or "0.0.0.0:9222")
    secrets_dir = Path(env.get("WEB_BROWSER_SECRETS_DIR") or "/run/secrets")
    client_tokens = tokens(secrets_dir)
    if not client_tokens:
        print(f"web-browser: no client token in {secrets_dir} "
              f"({', '.join(sorted(TOKEN_FILES.values()))}); run scripts/setup.sh", file=sys.stderr)
        return 2

    devtools_port = int(env.get("WEB_BROWSER_DEVTOOLS_PORT") or 9221)
    browser = chromium.launch(
        binary=env.get("WEB_BROWSER_CHROMIUM") or "/usr/bin/chromium",
        profile_dir=env.get("WEB_BROWSER_PROFILE_DIR") or "/profile",
        port=devtools_port,
        display=env.get("WEB_BROWSER_DISPLAY") or ":99",
        window=env.get("WEB_BROWSER_WINDOW") or "1280,900",
    )
    try:
        upstream = await chromium.browser_url(devtools_port)
    except RuntimeError as exc:
        browser.terminate()
        print(f"web-browser: {exc}", file=sys.stderr)
        return 1

    gateway = Gateway(upstream_url=upstream, tokens=client_tokens, host=host, port=port)
    listening = await gateway.start()
    LOG.info("gateway on %s:%d for %s", host, listening,
             ", ".join(f"{n} ({rules.CLIENTS[n]})" for n in sorted(client_tokens)))
    try:
        while browser.poll() is None:
            await asyncio.sleep(1)
    finally:
        await gateway.close()
    LOG.warning("chromium exited (%s); stopping so compose restarts the service", browser.returncode)
    return 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
