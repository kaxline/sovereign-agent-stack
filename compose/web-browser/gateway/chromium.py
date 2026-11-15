"""Chromium, started and owned by the gateway process.

It listens for DevTools on the container's own loopback, so the gateway is the
only way in: nothing outside the container can reach that port. The profile is a
named volume, so the sites a person signs in to through the viewer survive a
restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request

LOG = logging.getLogger("web-browser.chromium")

# --remote-debugging-address is not read with a pipe, and 127.0.0.1 is its
# default; the flag is here so a change of Chromium's default cannot open it.
FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-breakpad",
    "--disable-features=Translate,MediaRouter",
    "--password-store=basic",
    "--use-mock-keychain",
    "--remote-allow-origins=*",  # the gateway is the only client and refuses Origin itself
)


def launch(binary: str, profile_dir: str, port: int, display: str, window: str) -> subprocess.Popen:
    env = dict(os.environ, DISPLAY=display)
    args = [binary, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", f"--window-size={window}", *FLAGS, "about:blank"]
    LOG.info("starting chromium on display %s, devtools on 127.0.0.1:%d", display, port)
    return subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def browser_url(port: int, timeout: float = 30.0) -> str:
    """Chromium's browser WebSocket URL, waited for while it starts."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = ""
    while loop.time() < deadline:
        try:
            raw = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/json/version")
            url = json.loads(raw).get("webSocketDebuggerUrl")
            if isinstance(url, str) and url.startswith("ws://"):
                return url
            last = "no webSocketDebuggerUrl"
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last = type(exc).__name__
        await asyncio.sleep(0.25)
    raise RuntimeError(f"chromium devtools did not come up on 127.0.0.1:{port} ({last})")


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed loopback URL
        return response.read(1 << 20)
