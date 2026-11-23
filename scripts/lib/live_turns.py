"""Prints the dashboard gateway's live turns as one compact JSON array.

Run inside the hermes container, read from stdin, by scripts/coding-root.sh
--apply: `docker compose exec -T hermes /opt/hermes/.venv/bin/python -`. It
logs in to the dashboard on 127.0.0.1:9119 with the container's own basic-auth
settings, opens /api/ws with a ticket and calls session.live_turns
(compose/hermes/gateway_liveness.py), which answers from memory with ids,
status and times only: no title, preview or message text.

Exits 1 without printing when the gateway can't be reached, refuses the login
or doesn't know the method (an image without the overlay), so the caller
treats the answer as unknown.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

BASE = "http://127.0.0.1:9119"
TIMEOUT_S = 15.0


async def live_turns() -> list:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=timeout) as http:
        login = {
            "provider": "basic",
            "username": os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "admin"),
            "password": os.environ["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"],
            "next": "",
        }
        async with http.post(f"{BASE}/auth/password-login", json=login) as r:
            if r.status != 200:
                raise RuntimeError(f"dashboard login answered {r.status}")
        async with http.post(f"{BASE}/api/auth/ws-ticket") as r:
            if r.status != 200:
                raise RuntimeError(f"ws-ticket answered {r.status}")
            ticket = (await r.json())["ticket"]
        async with http.ws_connect(f"{BASE.replace('http', 'ws', 1)}/api/ws", params={"ticket": ticket}) as ws:
            await ws.send_json({"jsonrpc": "2.0", "id": "live-turns", "method": "session.live_turns", "params": {}})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                if not isinstance(frame, dict) or frame.get("id") != "live-turns":
                    continue  # events such as gateway.ready
                if "error" in frame:
                    raise RuntimeError(f"session.live_turns: {frame['error'].get('code')}")
                return list(frame["result"]["sessions"])
    raise RuntimeError("the gateway closed the socket before answering")


def main() -> int:
    try:
        rows = asyncio.run(asyncio.wait_for(live_turns(), TIMEOUT_S))
    except Exception as exc:  # any failure means "unknown"; the type alone, no URLs
        print(f"live_turns: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rows, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
