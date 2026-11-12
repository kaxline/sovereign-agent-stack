"""A small ACP client over WebSocket, for the client-gateway tests and the oracle.

Uses the ``websockets`` package (present in the Hermes image). Every frame it
receives is checked against the vendored schema when a ``Validator`` is given,
and the problems collect in ``frame_errors`` for the test to assert on.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Awaitable, Callable

import websockets

RequestHandler = Callable[[dict], Awaitable[dict]]


class RpcError(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.error = error
        self.code = error.get("code")

    @property
    def reason(self) -> str | None:
        data = self.error.get("data") or {}
        return (data.get("io.github.kaxline.stack") or {}).get("reason")


class AcpClient:
    def __init__(self, ws, validator=None) -> None:
        self.ws = ws
        self.validator = validator
        self.connection_id = None
        self.notifications: asyncio.Queue = asyncio.Queue()
        self.updates: list[dict] = []
        self.frame_errors: list[str] = []
        self.handlers: dict[str, RequestHandler] = {}
        self.server_requests: list[dict] = []
        self.cancelled_requests: list[object] = []
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._sent_methods: dict[int, str] = {}
        self._reader: asyncio.Task | None = None

    @classmethod
    async def connect(cls, url: str, token: str | None, origin: str | None = None, validator=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        ws = await websockets.connect(url, additional_headers=headers, origin=None, max_size=None)
        client = cls(ws, validator)
        client.connection_id = ws.response.headers.get("Acp-Connection-Id")
        client._reader = asyncio.create_task(client._read())
        return client

    async def close(self) -> None:
        await self.ws.close()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def _read(self) -> None:
        try:
            async for text in self.ws:
                frame = json.loads(text)
                if self.validator is not None:
                    self.frame_errors += self.validator.frame_errors(frame)
                if "method" in frame and "id" in frame:
                    asyncio.create_task(self._answer(frame))
                elif "method" in frame:
                    if frame["method"] == "session/update":
                        self.updates.append(frame["params"])
                    if frame["method"] == "$/cancel_request":
                        self.cancelled_requests.append(frame["params"].get("requestId"))
                    await self.notifications.put(frame)
                else:
                    fut = self._pending.pop(frame.get("id"), None)
                    if fut is not None and not fut.done():
                        if "error" in frame:
                            fut.set_exception(RpcError(frame["error"]))
                        else:
                            if self.validator is not None:
                                method = self._sent_methods.get(frame["id"])
                                if method:
                                    self.frame_errors += self.validator.result_errors(method, frame["result"])
                            fut.set_result(frame["result"])
        except websockets.ConnectionClosed:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("socket closed"))

    async def _answer(self, frame: dict) -> None:
        self.server_requests.append(frame)
        handler = self.handlers.get(frame["method"])
        if handler is None:
            reply = {"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32601, "message": "not handled"}}
        else:
            try:
                reply = {"jsonrpc": "2.0", "id": frame["id"], "result": await handler(frame["params"])}
            except asyncio.CancelledError:
                reply = {"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32800, "message": "cancelled"}}
        try:
            await self.ws.send(json.dumps(reply))
        except websockets.ConnectionClosed:
            pass

    async def request(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._sent_methods[rid] = method
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    def start_request(self, method: str, params: dict | None = None) -> tuple[int, asyncio.Task]:
        """Send without waiting; returns the request id and a task for the result."""
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._sent_methods[rid] = method
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

        async def go():
            await self.ws.send(payload)
            return await fut

        return rid, asyncio.create_task(go())

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}))

    async def send_raw(self, text: str) -> None:
        await self.ws.send(text)

    async def next_notification(self, method: str, timeout: float = 10) -> dict:
        async def find():
            while True:
                frame = await self.notifications.get()
                if frame["method"] == method:
                    return frame["params"]

        return await asyncio.wait_for(find(), timeout)

    async def initialize(self, **caps) -> dict:
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": caps or {"fs": {"readTextFile": False, "writeTextFile": False}},
                "clientInfo": {"name": "acp_client.py", "version": "0"},
            },
        )
