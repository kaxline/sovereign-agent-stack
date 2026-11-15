"""The CDP gateway: one WebSocket per client, Chromium behind it.

A client connects to ``ws://<host>:9222/devtools/browser/<name>?token=<token>``.
The name picks the client (``gateway.rules.CLIENTS``) and the token is compared
against that client's own token in constant time. A request carrying an
``Origin`` header is refused, so a page in any browser cannot drive this one.

Each accepted connection opens its own WebSocket to Chromium's browser endpoint,
so Chromium keeps the sessions of one client private to it, and the gateway needs
no multiplexer. Messages are forwarded both ways, with ``gateway.rules`` applied
to each call the client makes: a refused call is answered with a CDP error and
never reaches Chromium, and ``Browser.close`` is answered here, which closes that
client and leaves the browser running.

Log lines name the client, the method and the verdict. Never a parameter, a
result or a token: a CDP frame holds page content and values a fill wrote.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

import websockets

from . import rules

LOG = logging.getLogger("web-browser.gateway")

# websockets logs frames at DEBUG, and a frame can hold a page's content.
for _name in ("websockets", "websockets.server", "websockets.client", "websockets.protocol"):
    logging.getLogger(_name).setLevel(logging.WARNING)

PATH_PREFIX = "/devtools/browser/"
# A screenshot or a DOM snapshot comes back in one frame, so the cap is well
# above Chromium's own; the gateway is not the limit the agent runs into.
MAX_MESSAGE = 100 << 20
# -32000 is CDP's server error, the code Chromium itself uses for a refusal.
REFUSED_CODE = -32000


def _client_for(path: str, token: str) -> str | None:
    """The client name for ``path`` when ``token`` is its token, else None."""
    name = path[len(PATH_PREFIX):] if path.startswith(PATH_PREFIX) else ""
    return name if name in rules.CLIENTS else None


class Gateway:
    def __init__(self, upstream_url: str, tokens: dict, host: str = "0.0.0.0", port: int = 9222):
        unknown = set(tokens) - set(rules.CLIENTS)
        if unknown:
            raise ValueError(f"no such client: {', '.join(sorted(unknown))}")
        if not tokens:
            raise ValueError("at least one client token is required")
        self.upstream_url = upstream_url
        self.tokens = dict(tokens)
        self.host = host
        self.port = port
        self._server = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> int:
        self._server = await websockets.serve(
            self._client, self.host, self.port, max_size=MAX_MESSAGE, compression=None,
            process_request=self._process_request, ping_interval=20, ping_timeout=20,
        )
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # --- the handshake -----------------------------------------------------

    async def _process_request(self, *args):
        """Refuse before the upgrade: unknown path 404, bad token 401, Origin 403.

        Takes both the ``(path, headers)`` of websockets 10 and the
        ``(connection, request)`` of websockets 14 and later.
        """
        connection = None
        if len(args) == 2 and isinstance(args[0], str):
            path, headers = args
        else:
            connection, request = args
            path, headers = request.path, request.headers
        split = urlsplit(path)
        name = split.path[len(PATH_PREFIX):] if split.path.startswith(PATH_PREFIX) else ""
        if name not in rules.CLIENTS or name not in self.tokens:
            return self._refuse(connection, HTTPStatus.NOT_FOUND, "not found")
        if headers.get("Origin") is not None:
            LOG.info("%s - origin_refused", name)
            return self._refuse(connection, HTTPStatus.FORBIDDEN, "origin refused")
        given = (parse_qs(split.query).get("token") or [""])[0]
        if not hmac.compare_digest(given, self.tokens[name]):
            LOG.info("%s - token_refused", name)
            return self._refuse(connection, HTTPStatus.UNAUTHORIZED, "unauthorized")
        return None

    @staticmethod
    def _refuse(connection, status: HTTPStatus, text: str):
        if connection is not None:  # websockets 14+
            return connection.respond(status, f"{text}\n")
        return status, [("Content-Type", "text/plain; charset=utf-8")], f"{text}\n".encode()

    # --- one client --------------------------------------------------------

    async def _client(self, ws, path: str = "") -> None:
        path = urlsplit(getattr(ws, "path", None) or path).path
        name = path[len(PATH_PREFIX):] if path.startswith(PATH_PREFIX) else ""
        role = rules.CLIENTS.get(name)
        if role is None:  # _process_request has already refused; belt and braces
            await ws.close(code=1008, reason="unknown client")
            return
        LOG.info("%s connected", name)
        try:
            async with websockets.connect(self.upstream_url, max_size=MAX_MESSAGE,
                                          compression=None, ping_interval=None) as upstream:
                await self._pump(ws, upstream, name, role)
        except websockets.ConnectionClosed:
            pass
        except OSError as exc:
            LOG.warning("%s: browser unreachable (%s)", name, type(exc).__name__)
            await ws.close(code=1011, reason="browser unreachable")
        finally:
            LOG.info("%s disconnected", name)

    async def _pump(self, ws, upstream, name: str, role: str) -> None:
        to_browser = asyncio.create_task(self._from_client(ws, upstream, name, role))
        to_client = asyncio.create_task(self._from_browser(ws, upstream))
        try:
            done, pending = await asyncio.wait({to_browser, to_client},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
        except asyncio.CancelledError:
            to_browser.cancel()
            to_client.cancel()
            raise

    async def _from_client(self, ws, upstream, name: str, role: str) -> None:
        async for raw in ws:
            message = self._parse(raw)
            if message is None:
                LOG.info("%s - unparseable", name)
                await self._error(ws, None, None, "unparseable message")
                continue
            method = message.get("method")
            call, reason = rules.verdict(role, method if isinstance(method, str) else "", message.get("params"))
            LOG.info("%s %s %s", name, method if isinstance(method, str) else "-", reason)
            if call == rules.ALLOW:
                await upstream.send(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                continue
            if call == rules.ANSWER:
                await self._send(ws, {"id": message.get("id"), "result": {}}, message.get("sessionId"))
                await ws.close(code=1000, reason="closed")
                return
            await self._error(ws, message.get("id"), message.get("sessionId"), reason)

    @staticmethod
    async def _from_browser(ws, upstream) -> None:
        async for raw in upstream:
            await ws.send(raw)

    # --- frames ------------------------------------------------------------

    @staticmethod
    def _parse(raw):
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            return None
        return message if isinstance(message, dict) else None

    @classmethod
    async def _error(cls, ws, ident, session_id, reason: str) -> None:
        await cls._send(ws, {"id": ident,
                             "error": {"code": REFUSED_CODE, "message": f"web-browser gateway: {reason}"}},
                        session_id)

    @staticmethod
    async def _send(ws, body: dict, session_id=None) -> None:
        if isinstance(session_id, str) and session_id:
            body["sessionId"] = session_id
        try:
            await ws.send(json.dumps(body))
        except websockets.ConnectionClosed:
            pass
