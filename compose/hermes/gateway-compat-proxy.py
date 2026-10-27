#!/usr/bin/env python3
"""Keep the per-profile API ports answering under the multiplexed gateway.

With HERMES_GATEWAY_TOPOLOGY=multiplex, one Hermes gateway serves every
profile on the default listener (8642) at /p/<profile>/. Boundary, n8n, the
WebUI and tool-eval call api-server on 8643 and browser on 8644. This proxy
listens on those ports and forwards each request to the matching prefix:

    :8643/v1/runs  ->  127.0.0.1:8642/p/api-server/v1/runs

Requests and responses stream in both directions (run events are SSE),
Authorization passes through unchanged, so each profile's own API_SERVER_KEY
still applies, and WebSocket upgrades (/v1/browser-control/ws) are bridged.

HERMES_COMPAT_PROXY_ROUTES lists port=profile pairs (default
"8643=api-server,8644=browser"); HERMES_COMPAT_PROXY_UPSTREAM is the default
listener (default http://127.0.0.1:8642). Run by the s6 service
gateway-compat-proxy; see docs/hermes.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, WSMsgType, web

log = logging.getLogger("gateway-compat-proxy")

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
RETRY_BIND_SECONDS = 10


def parse_routes(raw: str) -> list[tuple[int, str]]:
    routes = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        port, _, profile = item.partition("=")
        if not port.strip().isdigit() or not profile.strip():
            raise ValueError(f"bad route {item!r}: want port=profile")
        routes.append((int(port), profile.strip()))
    return routes


def _forward_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def make_app(upstream: str, profile: str, session_box: dict) -> web.Application:
    prefix = f"{upstream.rstrip('/')}/p/{profile}"

    async def ws_bridge(request: web.Request, url: str) -> web.WebSocketResponse:
        headers = {k: v for k, v in _forward_headers(request.headers).items()
                   if not k.lower().startswith("sec-websocket-")}
        async with session_box["session"].ws_connect(url, headers=headers) as upstream_ws:
            client_ws = web.WebSocketResponse()
            await client_ws.prepare(request)

            async def pump(src, dst) -> None:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break
                await dst.close()

            await asyncio.gather(pump(client_ws, upstream_ws), pump(upstream_ws, client_ws),
                                 return_exceptions=True)
            return client_ws

    async def handle(request: web.Request) -> web.StreamResponse:
        url = prefix + str(request.rel_url)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await ws_bridge(request, url)
        body = request.content if request.body_exists else None
        try:
            upstream_cm = await session_box["session"].request(
                request.method, url, headers=_forward_headers(request.headers), data=body,
                allow_redirects=False,
            )
        except ClientConnectionError as exc:
            return web.json_response({"error": {"message": f"Hermes gateway unreachable: {exc}",
                                                "type": "server_error", "code": "gateway_unreachable"}},
                                     status=502)
        async with upstream_cm as up:
            resp = web.StreamResponse(status=up.status, reason=up.reason,
                                      headers=_forward_headers(up.headers))
            if up.headers.get("Content-Length") and "chunked" not in up.headers.get("Transfer-Encoding", ""):
                resp.content_length = int(up.headers["Content-Length"])
            try:
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
            except ConnectionResetError:
                # The client gave up first (a probe timeout, a closed tab); closing the
                # upstream response below is all there is to do.
                log.debug("client closed %s %s before the response finished", request.method, request.path)
            return resp

    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


async def serve(routes: list[tuple[int, str]], upstream: str, host: str) -> None:
    # Reads stay open for as long as a run streams; auto_decompress=False passes bodies through as sent.
    session = ClientSession(timeout=ClientTimeout(total=None, connect=10, sock_read=None),
                            auto_decompress=False)
    box = {"session": session}
    runners = []
    for port, profile in routes:
        runner = web.AppRunner(make_app(upstream, profile, box), access_log=None)
        await runner.setup()
        runners.append(runner)
        warned = False
        while True:
            try:
                await web.TCPSite(runner, host, port).start()
                break
            except OSError as exc:
                # A profile still running its own gateway (gateway.standalone: true) holds the port.
                if not warned:
                    log.warning("cannot bind %s:%d (%s); retrying every %ds", host, port, exc, RETRY_BIND_SECONDS)
                    warned = True
                await asyncio.sleep(RETRY_BIND_SECONDS)
        log.info("%s:%d -> %s/p/%s/", host, port, upstream, profile)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await session.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="[gateway-compat-proxy] %(message)s")
    routes = parse_routes(os.environ.get("HERMES_COMPAT_PROXY_ROUTES", "8643=api-server,8644=browser"))
    upstream = os.environ.get("HERMES_COMPAT_PROXY_UPSTREAM", "http://127.0.0.1:8642")
    host = os.environ.get("HERMES_COMPAT_PROXY_HOST", "0.0.0.0")
    asyncio.run(serve(routes, upstream, host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
