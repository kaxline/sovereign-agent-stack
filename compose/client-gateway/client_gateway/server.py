"""HTTP upgrade, routing, auth and startup.

``GET /acp`` serves the built-in assistant and ``GET /acp/agents/<slug>`` an
agent, both as WebSocket upgrades. The bearer token, the Origin and the
identity are checked before the upgrade is accepted, so a refused client gets
an HTTP status (401, 403, 404) and no socket. ``GET /health`` answers without
auth and reveals nothing but liveness.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .auth import Settings, SettingsError, bearer_ok, load_settings, origin_ok
from .backend import Backend
from .connection import Connection
from .sessions import SessionHub
from .validate import Validator

log = logging.getLogger("client_gateway")

VERSION = os.environ.get("CLIENT_GATEWAY_VERSION", "dev")


class _WsTransport:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def send_text(self, text: str) -> None:
        await self.ws.send_text(text)

    async def receive_text(self) -> str:
        return await self.ws.receive_text()


def create_app(settings: Settings, backend: Backend) -> Starlette:
    hub = SessionHub()
    backend.bind(hub)
    validator = Validator() if settings.validate_frames else None

    async def acp(ws: WebSocket) -> None:
        if not origin_ok(ws.headers.get("origin"), settings.allowed_origins):
            await ws.send_denial_response(PlainTextResponse("origin not allowed\n", status_code=403))
            return
        if not bearer_ok(ws.headers.get("authorization"), settings.token):
            await ws.send_denial_response(
                PlainTextResponse("unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
            )
            return
        slug = ws.path_params.get("slug")
        identity = await backend.describe_identity(slug)
        if identity is None:
            await ws.send_denial_response(PlainTextResponse("no such agent\n", status_code=404))
            return
        conn = Connection(_WsTransport(ws), identity, backend, hub, validator, VERSION)
        await ws.accept(headers=[(b"acp-connection-id", conn.id.encode())])
        await conn.run()

    async def health(_request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(_app):
        await backend.start()
        try:
            yield
        finally:
            await backend.stop()

    return Starlette(
        routes=[
            Route("/health", health),
            WebSocketRoute("/acp", acp),
            WebSocketRoute("/acp/agents/{slug}", acp),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"client-gateway: {exc}", file=sys.stderr)
        raise SystemExit(2)
    import warnings

    import uvicorn

    from .hermes.backend import HermesBackend

    # uvicorn 0.54 deprecates its "websockets" implementation for
    # "websockets-sansio", but the sansio one logs "ASGI callable returned
    # without completing handshake" after every refused upgrade (401/403/404).
    warnings.filterwarnings("ignore", message="The `websockets` implementation is deprecated")

    app = create_app(settings, HermesBackend(settings))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", ws="websockets")


if __name__ == "__main__":
    main()
