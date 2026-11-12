"""One ACP connection: the JSON-RPC loop over a WebSocket.

Each incoming request runs in its own task, so a long ``session/prompt`` doesn't
hold up ``session/cancel`` or a permission answer on the same socket.
``$/cancel_request`` cancels that task and the request answers ``-32800``.
Logs carry method names, ids and durations only, never params.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from .backend import Backend, Identity
from .errors import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    NS,
    PARSE_ERROR,
    REQUEST_CANCELLED,
    RESOURCE_NOT_FOUND,
    AcpError,
    error_obj,
)
from .sessions import SessionHub
from .validate import Validator

log = logging.getLogger("client_gateway.connection")

PROTOCOL_VERSION = 1
# Stack extensions this gateway serves, with a version integer each.
EXTENSIONS: dict[str, int] = {}


class Transport:
    """What a connection needs from a socket; Starlette's WebSocket in production."""

    async def send_text(self, text: str) -> None: ...
    async def receive_text(self) -> str: ...


Handler = Callable[["Connection", dict], Awaitable[object]]


class Connection:
    def __init__(
        self,
        transport: Transport,
        identity: Identity,
        backend: Backend,
        hub: SessionHub,
        validator: Validator | None = None,
        version: str = "dev",
    ) -> None:
        self.id = str(uuid.uuid4())
        self.transport = transport
        self.identity = identity
        self.backend = backend
        self.hub = hub
        self.validator = validator
        self.version = version
        self.client_capabilities: dict = {}
        self._send_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._outgoing: dict[int, asyncio.Future] = {}
        self._incoming: dict[object, asyncio.Task] = {}
        self._closed = False

    # -- sending -----------------------------------------------------------

    async def _send(self, frame: dict) -> None:
        if self._closed:
            return
        text = json.dumps(frame, separators=(",", ":"))
        async with self._send_lock:
            try:
                await self.transport.send_text(text)
            except Exception:
                self._closed = True

    async def send_notification(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def send_request(self, method: str, params: dict) -> dict:
        """Send a request to the client and wait for its result. Cancelling the
        awaiting task sends ``$/cancel_request``. An error answer raises AcpError."""
        rid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._outgoing[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await fut
        except asyncio.CancelledError:
            await self.send_notification("$/cancel_request", {"requestId": rid})
            raise
        finally:
            self._outgoing.pop(rid, None)

    # -- receiving ---------------------------------------------------------

    async def run(self) -> None:
        log.info("conn %s open identity=%s", self.id, self.identity.slug or "-")
        try:
            while True:
                try:
                    text = await self.transport.receive_text()
                except Exception:
                    break
                await self._on_text(text)
        finally:
            self._closed = True
            for task in list(self._incoming.values()):
                task.cancel()
            for fut in self._outgoing.values():
                if not fut.done():
                    fut.cancel()
            self.hub.detach_all(self)
            log.info("conn %s closed", self.id)

    async def _on_text(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            await self._send({"jsonrpc": "2.0", "id": None, "error": error_obj(PARSE_ERROR, "parse error")})
            return
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            await self._send(
                {"jsonrpc": "2.0", "id": None, "error": error_obj(INVALID_REQUEST, "not a JSON-RPC 2.0 object")}
            )
            return
        if "method" in frame:
            if "id" in frame:
                if frame["id"] in self._incoming:
                    await self._send({
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "error": error_obj(INVALID_REQUEST, "request id already in use", "duplicate_request_id"),
                    })
                    return
                task = asyncio.create_task(self._serve(frame))
                self._incoming[frame["id"]] = task
                task.add_done_callback(lambda _t, rid=frame["id"]: self._incoming.pop(rid, None))
            else:
                await self._on_notification(frame["method"], frame.get("params") or {})
            return
        rid = frame.get("id")
        fut = self._outgoing.get(rid) if isinstance(rid, int) else None
        if fut is None or fut.done():
            return
        if "error" in frame:
            err = frame["error"] or {}
            fut.set_exception(AcpError(err.get("code", INTERNAL_ERROR), err.get("message", "client error")))
        else:
            fut.set_result(frame.get("result") or {})

    async def _on_notification(self, method: str, params: dict) -> None:
        if method == "$/cancel_request":
            task = self._incoming.get(params.get("requestId"))
            if task is not None:
                task.cancel()
        elif method == "session/cancel":
            sid = params.get("sessionId")
            if isinstance(sid, str):
                try:
                    await self.backend.cancel(self.identity, sid)
                except AcpError as exc:
                    log.info("conn %s session/cancel: %s", self.id, exc.reason)
        # Unknown notifications are ignored, as JSON-RPC allows.

    async def _serve(self, frame: dict) -> None:
        rid, method, params = frame["id"], frame["method"], frame.get("params") or {}
        started = time.monotonic()
        try:
            if self.validator is not None:
                problems = self.validator.params_errors(method, params)
                if problems and not problems[0].startswith("no schema"):
                    raise AcpError(INVALID_PARAMS, "invalid params", "invalid_params", details=problems[:5])
            handler = HANDLERS.get(method)
            if handler is None:
                raise AcpError(METHOD_NOT_FOUND, f"method not found: {method}", "method_not_found")
            result = await handler(self, params)
            reply: dict = {"jsonrpc": "2.0", "id": rid, "result": result if result is not None else {}}
            outcome = "ok"
        except asyncio.CancelledError:
            reply = {"jsonrpc": "2.0", "id": rid, "error": error_obj(REQUEST_CANCELLED, "request cancelled")}
            outcome = "cancelled"
        except AcpError as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": exc.to_error()}
            outcome = exc.reason or str(exc.code)
        except Exception as exc:  # a bug; the client still gets an answer
            log.exception("conn %s %s failed", self.id, method)
            reply = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": error_obj(INTERNAL_ERROR, "internal error", "internal_error", type=type(exc).__name__),
            }
            outcome = "internal_error"
        log.info("conn %s %s %s %.0fms", self.id, method, outcome, (time.monotonic() - started) * 1000)
        await self._send(reply)

    def watch(self, session_id: str) -> None:
        self.hub.attach(session_id, self)


# -- ACP methods ---------------------------------------------------------------


async def _initialize(conn: Connection, params: dict) -> dict:
    conn.client_capabilities = params.get("clientCapabilities") or {}
    ident = conn.identity
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentInfo": {"name": ident.name, "title": ident.title, "version": conn.version},
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            "mcpCapabilities": {"http": True, "sse": False},
            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}, "delete": {}, "systemPrompt": {}},
            "_meta": {NS: {"extensions": dict(EXTENSIONS)}},
        },
        "authMethods": [],
    }


def _sid(params: dict) -> str:
    sid = params.get("sessionId")
    if not isinstance(sid, str) or not sid:
        raise AcpError(INVALID_PARAMS, "sessionId is required", "invalid_params")
    return sid


async def _session_new(conn: Connection, params: dict) -> dict:
    result = await conn.backend.new_session(conn.identity, params)
    conn.watch(result["sessionId"])
    return result


async def _session_load(conn: Connection, params: dict) -> dict:
    sid = _sid(params)
    conn.watch(sid)  # before replay, so the history reaches this connection
    try:
        return await conn.backend.load_session(conn.identity, params, replay=True)
    except BaseException:
        conn.hub.detach(sid, conn)
        raise


async def _session_resume(conn: Connection, params: dict) -> dict:
    sid = _sid(params)
    result = await conn.backend.load_session(conn.identity, params, replay=False)
    conn.watch(sid)
    return result


async def _session_list(conn: Connection, params: dict) -> dict:
    return await conn.backend.list_sessions(conn.identity, params)


async def _session_close(conn: Connection, params: dict) -> dict:
    sid = _sid(params)
    conn.hub.detach(sid, conn)
    if not conn.hub.watchers(sid):
        await conn.backend.close_session(conn.identity, sid)
    return {}


async def _session_delete(conn: Connection, params: dict) -> dict:
    sid = _sid(params)
    await conn.backend.delete_session(conn.identity, sid)
    for other in conn.hub.watchers(sid):
        conn.hub.detach(sid, other)
    return {}


async def _session_prompt(conn: Connection, params: dict) -> dict:
    sid = _sid(params)
    if conn not in conn.hub.watchers(sid):
        raise AcpError(RESOURCE_NOT_FOUND, "session is not open on this connection", "session_not_open")
    blocks = params.get("prompt") or []
    meta = (params.get("_meta") or {}).get(NS) or {}
    return await conn.backend.prompt(conn.identity, sid, blocks, meta)


HANDLERS: dict[str, Handler] = {
    "initialize": _initialize,
    "session/new": _session_new,
    "session/load": _session_load,
    "session/resume": _session_resume,
    "session/list": _session_list,
    "session/close": _session_close,
    "session/delete": _session_delete,
    "session/prompt": _session_prompt,
}
