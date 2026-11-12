"""Client for Hermes' legacy JSON-RPC protocol on the dashboard's ``/api/ws``.

Connect: ``POST /auth/password-login`` (provider ``basic``) for the session
cookie, ``POST /api/auth/ws-ticket`` for a single-use 30 s ticket, then
``/api/ws?ticket=``. The first frame is the ``gateway.ready`` event; the client
then sends ``client.capabilities {server_requests: true}`` (without it Hermes
withdraws every approval at once). A fresh ticket is minted for every
connection. On a drop the owner is told (``on_down``) and the client
reconnects with backoff up to 30 s, telling the owner again when it is back
(``on_up``).

Frames: events arrive as ``{"method": "event", "params": {type, session_id,
seq?, payload?}}``; server requests as ``{"id": "srq-…", "method": …}``,
answered with a bare result frame. Nothing here logs params or payloads.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("client_gateway.hermes.upstream")

PING_INTERVAL_S = 15.0
READY_TIMEOUT_S = 10.0
BACKOFF_MAX_S = 30.0


class HermesError(Exception):
    def __init__(self, method: str, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{method}: {code} {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


class UpstreamDown(Exception):
    """The upstream socket is not connected, or dropped while a call was in flight."""


EventHandler = Callable[[dict], Awaitable[None]]
RequestHandler = Callable[[str, str, dict], Awaitable[dict]]  # (srq id, method, params)
StateHandler = Callable[[], Awaitable[None]]


class Upstream:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        on_event: EventHandler,
        on_request: RequestHandler,
        on_down: StateHandler,
        on_up: StateHandler,
        provider: str = "basic",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.provider = provider
        self.on_event = on_event
        self.on_request = on_request
        self.on_down = on_down
        self.on_up = on_up
        self.replay_epoch: str | None = None
        self.connected = asyncio.Event()
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._runner: asyncio.Task | None = None
        self._stopping = False
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()

    async def drop(self) -> None:
        """Close the socket as if the network dropped (tests, and a stuck socket)."""
        if self._ws is not None:
            await self._ws.close()

    # -- connection --------------------------------------------------------

    async def _login(self) -> str:
        assert self._http is not None
        async with self._http.post(
            f"{self.base_url}/auth/password-login",
            json={"provider": self.provider, "username": self.user, "password": self.password, "next": ""},
        ) as r:
            if r.status != 200:
                raise UpstreamDown(f"dashboard login answered {r.status}")
        async with self._http.post(f"{self.base_url}/api/auth/ws-ticket") as r:
            if r.status != 200:
                raise UpstreamDown(f"ws-ticket answered {r.status}")
            return (await r.json())["ticket"]

    async def _connect_once(self) -> None:
        assert self._http is not None
        ticket = await self._login()
        ws_url = self.base_url.replace("http", "ws", 1) + "/api/ws"
        self._ws = await self._http.ws_connect(ws_url, params={"ticket": ticket}, heartbeat=None, max_msg_size=0)
        first = await asyncio.wait_for(self._ws.receive(), READY_TIMEOUT_S)
        frame = json.loads(first.data) if first.type == aiohttp.WSMsgType.TEXT else {}
        params = frame.get("params") or {}
        if frame.get("method") != "event" or params.get("type") != "gateway.ready":
            raise UpstreamDown("first frame was not gateway.ready")
        payload = params.get("payload") or {}
        if payload.get("session_authority"):
            # The canonical wire; this client speaks legacy (spike 0.6).
            raise UpstreamDown("upstream serves hermes-gateway-v1 (session_authority); legacy client can't use it")
        self.replay_epoch = payload.get("replay_epoch")

    async def _run(self) -> None:
        delay = 0.5
        while not self._stopping:
            try:
                await self._connect_once()
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError, UpstreamDown, ValueError) as exc:
                log.warning("upstream connect failed: %s; retry in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
                continue
            delay = 0.5
            reader = asyncio.create_task(self._read())
            try:
                await self._call_raw("client.capabilities", {"server_requests": True}, timeout=10)
                self.connected.set()
                log.info("upstream connected")
                await self.on_up()
                pinger = asyncio.create_task(self._ping())
                try:
                    await reader
                finally:
                    pinger.cancel()
            except (UpstreamDown, HermesError, asyncio.TimeoutError) as exc:
                log.warning("upstream handshake failed: %s", exc)
                reader.cancel()
            self.connected.clear()
            self._fail_pending()
            if self._ws is not None and not self._ws.closed:
                await self._ws.close()
            if self._stopping:
                return
            log.warning("upstream dropped")
            await self.on_down()
            await asyncio.sleep(delay)

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            try:
                await self._call_raw("gateway.ping", {}, timeout=PING_INTERVAL_S)
            except (UpstreamDown, HermesError, asyncio.TimeoutError):
                log.warning("upstream ping failed; closing the socket")
                await self.drop()
                return

    def _fail_pending(self) -> None:
        for method, fut in self._pending.values():
            if not fut.done():
                fut.set_exception(UpstreamDown(f"upstream dropped during {method}"))
        self._pending.clear()

    async def _read(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            method = frame.get("method")
            if method == "event":
                self._spawn(self.on_event(frame.get("params") or {}))
            elif method is not None and "id" in frame:
                self._spawn(self._serve_request(frame))
            elif "id" in frame:
                entry = self._pending.pop(frame["id"], None)
                if entry is None:
                    continue
                name, fut = entry
                if fut.done():
                    continue
                if "error" in frame:
                    err = frame["error"] or {}
                    fut.set_exception(HermesError(name, err.get("code", 0), err.get("message", ""), err.get("data")))
                else:
                    fut.set_result(frame.get("result") or {})

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_request(self, frame: dict) -> None:
        rid, method = frame["id"], frame["method"]
        try:
            result = await self.on_request(str(rid), method, frame.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as exc:  # Hermes resolves an error answer as "no answer"
            log.info("server request %s not answered: %s", method, type(exc).__name__)
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not handled"}}
        await self._send(reply)

    async def _send(self, frame: dict) -> None:
        if self._ws is None or self._ws.closed:
            raise UpstreamDown("upstream not connected")
        await self._ws.send_str(json.dumps(frame, separators=(",", ":")))

    # -- calls -------------------------------------------------------------

    async def _call_raw(self, method: str, params: dict, timeout: float) -> dict:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = (method, fut)
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def call(self, method: str, params: dict, timeout: float = 180.0) -> dict:
        """One request. Raises HermesError for an error answer and UpstreamDown when
        the socket is down or drops before the answer."""
        if not self.connected.is_set():
            raise UpstreamDown("upstream not connected")
        return await self._call_raw(method, params, timeout)
