"""A scripted stand-in for Hermes' dashboard ``/api/ws`` (legacy protocol, v0.23.1).

Enough of the wire for the client gateway's Hermes backend: password login
and single-use ws tickets, ``gateway.ready``, the session and prompt methods,
turn events, ``approval`` and ``clarify`` server requests, ``request.cancel``,
and 4001 for a reaped runtime id. Params are checked against allow-lists the
way Hermes' ``extra="forbid"`` contracts do (error 4000), with the stack
overlays' fields (``context``, ``history_budget``, ``profile``) added.

What a turn does depends on the prompt text: ``tool``, ``approve``,
``clarify``, ``hang`` (until interrupted), ``nostream``, ``fail``; anything
else streams "Hello" and " world". All data is synthetic.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

USER, PASSWORD = "admin", "fake-dashboard-password"

ALLOWED = {
    "client.capabilities": {"server_requests", "desktop_local"},
    "gateway.ping": None,  # anything
    "profiles.list": {"include_sessions"},
    "profiles.describe": {"name"},
    "session.create": {"source", "cwd", "title", "profile", "context", "history_budget", "messages", "hidden", "parent_session_id"},
    "session.resume": {"session_id", "source", "profile", "omit_messages", "context", "history_budget", "lazy"},
    "session.list": {"limit", "profile", "title", "include_hidden"},
    "session.close": {"session_id", "profile"},
    "session.delete": {"session_id", "profile"},
    "session.interrupt": {"session_id", "profile"},
    "session.cwd.set": {"session_id", "cwd", "profile"},
    "prompt.submit": {"session_id", "text", "context", "queued", "display_kind", "profile"},
}


@dataclass
class FakeSession:
    stored_id: str
    profile: str
    source: str | None
    title: str | None = None
    cwd: str = "/opt/data"
    context: str | None = None
    messages: list = field(default_factory=list)


class FakeTuiGateway:
    def __init__(self, profiles=("default", "alpha")) -> None:
        self.profiles = list(profiles)
        self.mcp = {"default": [{"name": "boundary", "enabled": True}], "alpha": []}
        self.sessions: dict[str, FakeSession] = {}
        self.runtime: dict[str, str] = {}  # runtime id -> stored id
        self.calls: list[tuple[str, dict]] = []
        self.answers: list[tuple[str, dict]] = []
        self.epoch = secrets.token_hex(16)
        self._tickets: set[str] = set()
        self._cookies: set[str] = set()
        self._sockets: set[web.WebSocketResponse] = set()
        self._ids = itertools.count(1)
        self._rows = itertools.count(100)
        self._srq: dict[str, asyncio.Future] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._queue: dict[str, list[str]] = {}
        self._seq: dict[str, int] = {}
        self._owner: dict[str, web.WebSocketResponse] = {}
        self.logins = 0
        self.runner: web.AppRunner | None = None
        self.url = ""

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/auth/password-login", self._login)
        app.router.add_post("/api/auth/ws-ticket", self._ticket)
        app.router.add_get("/api/ws", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        await self.drop_all()
        if self.runner:
            await self.runner.cleanup()

    async def drop_all(self) -> None:
        """Close every socket, as a Hermes restart would, then stop the turns."""
        for ws in list(self._sockets):
            await ws.close()
        self._owner.clear()
        self.runtime.clear()
        for task in list(self._running.values()):
            task.cancel()
        self.epoch = secrets.token_hex(16)

    def reap(self, stored_id: str) -> None:
        """Forget a session's runtime id, so calls with it answer 4001."""
        for rid, sid in list(self.runtime.items()):
            if sid == stored_id:
                del self.runtime[rid]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    # -- HTTP ---------------------------------------------------------------

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("provider") != "basic" or body.get("username") != USER or body.get("password") != PASSWORD:
            return web.json_response({"detail": "bad credentials"}, status=401)
        self.logins += 1
        token = secrets.token_hex(8)
        self._cookies.add(token)
        resp = web.json_response({"ok": True, "next": "/"})
        resp.set_cookie("hermes_session_at", token, httponly=True, samesite="Lax")
        return resp

    async def _ticket(self, request: web.Request) -> web.Response:
        if request.cookies.get("hermes_session_at") not in self._cookies:
            return web.json_response({"detail": "unauthorized"}, status=401)
        ticket = secrets.token_urlsafe(32)
        self._tickets.add(ticket)
        return web.json_response({"ticket": ticket, "ttl_seconds": 30})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ticket = request.query.get("ticket", "")
        if ticket not in self._tickets:
            raise web.HTTPForbidden()
        self._tickets.discard(ticket)  # single-use
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        await self._send(ws, self._event("gateway.ready", "", {"heartbeat": True, "replay_epoch": self.epoch}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                if "method" in frame:
                    asyncio.create_task(self._dispatch(ws, frame))
                elif frame.get("id") in self._srq:
                    fut = self._srq.pop(frame["id"])
                    if not fut.done():
                        fut.set_result(frame.get("result") if "result" in frame else None)
        finally:
            self._sockets.discard(ws)
        return ws

    # -- wire helpers -------------------------------------------------------

    async def _send(self, ws, frame: dict) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps(frame))

    def _event(self, etype: str, runtime_id: str, payload: dict | None) -> dict:
        params: dict = {"type": etype, "session_id": runtime_id}
        if payload is not None:
            params["payload"] = payload
        if runtime_id:
            self._seq[runtime_id] = self._seq.get(runtime_id, 0) + 1
            params["seq"] = self._seq[runtime_id]
        return {"jsonrpc": "2.0", "method": "event", "params": params}

    async def emit(self, runtime_id: str, etype: str, payload: dict | None = None) -> None:
        ws = self._owner.get(runtime_id)
        if ws is not None:
            await self._send(ws, self._event(etype, runtime_id, payload))

    async def ask(self, runtime_id: str, method: str, params: dict) -> dict | None:
        ws = self._owner.get(runtime_id)
        rid = f"srq-{secrets.token_hex(6)}"
        fut = asyncio.get_running_loop().create_future()
        self._srq[rid] = fut
        await self._send(ws, {"jsonrpc": "2.0", "id": rid, "method": method, "params": {"session_id": runtime_id, **params}})
        try:
            return await fut
        except asyncio.CancelledError:
            self._srq.pop(rid, None)
            await self.emit(runtime_id, "request.cancel", {"id": rid, "method": method, "reason": "interrupted"})
            raise

    @staticmethod
    def _err(rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    async def _dispatch(self, ws, frame: dict) -> None:
        rid, method, params = frame.get("id"), frame["method"], frame.get("params") or {}
        self.calls.append((method, params))
        allowed = ALLOWED.get(method, set())
        if method not in ALLOWED:
            await self._send(ws, self._err(rid, -32601, f"unknown method {method}"))
            return
        if allowed is not None:
            extra = set(params) - allowed
            if extra:
                await self._send(ws, self._err(rid, 4000, f"unknown param {sorted(extra)[0]}"))
                return
        try:
            result = await getattr(self, "_m_" + method.replace(".", "_"))(ws, params)
            await self._send(ws, {"jsonrpc": "2.0", "id": rid, "result": result})
        except _RpcError as exc:
            await self._send(ws, self._err(rid, exc.code, exc.message))

    def _live(self, params: dict) -> FakeSession:
        stored = self.runtime.get(params.get("session_id", ""))
        if stored is None:
            raise _RpcError(4001, "session not found; resume it")
        return self.sessions[stored]

    def _new_runtime(self, ws, stored_id: str) -> str:
        rid = secrets.token_hex(4)
        self.runtime[rid] = stored_id
        self._owner[rid] = ws
        return rid

    # -- methods ------------------------------------------------------------

    async def _m_client_capabilities(self, ws, p):
        return {"server_requests": ["approval", "clarify", "secret"], "declines_not_shown": True}

    async def _m_gateway_ping(self, ws, p):
        return {"ok": True}

    async def _m_profiles_list(self, ws, p):
        return {"profiles": [{"name": n, "is_default": n == "default", "display_name": n.title()} for n in self.profiles]}

    async def _m_profiles_describe(self, ws, p):
        if p.get("name") not in self.profiles:
            raise _RpcError(5063, "no such profile")
        return {"name": p["name"], "mcp_servers": self.mcp.get(p["name"], [])}

    async def _m_session_create(self, ws, p):
        profile = p.get("profile") or "default"
        if profile not in self.profiles:
            raise _RpcError(4064, "profile unavailable")
        if p.get("context") is not None and len(p["context"]) > 16000:
            raise _RpcError(4080, "context too long")
        stored = f"20261112_120000_{secrets.token_hex(3)}"
        self.sessions[stored] = FakeSession(stored, profile, p.get("source"), p.get("title"), context=p.get("context"))
        return {"session_id": self._new_runtime(ws, stored), "stored_session_id": stored, "message_count": 0, "messages": []}

    async def _m_session_resume(self, ws, p):
        s = self.sessions.get(p.get("session_id", ""))
        if s is None or s.profile != (p.get("profile") or "default"):
            raise _RpcError(4007, "session not found")
        live = [r for r, sid in self.runtime.items() if sid == s.stored_id]
        rid = live[0] if live else self._new_runtime(ws, s.stored_id)
        self._owner[rid] = ws
        out = {"session_id": rid, "resumed": s.stored_id, "message_count": len(s.messages)}
        if not p.get("omit_messages"):
            out["messages"] = list(s.messages)
        return out

    async def _m_session_list(self, ws, p):
        profile = p.get("profile") or "default"
        return {
            "sessions": [
                {"id": s.stored_id, "title": s.title, "preview": "", "started_at": 1762948800.0,
                 "message_count": len(s.messages), "source": s.source}
                for s in self.sessions.values() if s.profile == profile
            ]
        }

    async def _m_session_close(self, ws, p):
        s = self._live(p)
        self.reap(s.stored_id)
        return {"closed": True, "messages": []}

    async def _m_session_delete(self, ws, p):
        s = self.sessions.get(p.get("session_id", ""))
        if s is None:
            raise _RpcError(4007, "session not found")
        if any(sid == s.stored_id for sid in self.runtime.values()):
            raise _RpcError(4023, "session is active")
        del self.sessions[s.stored_id]
        return {"deleted": s.stored_id}

    async def _m_session_cwd_set(self, ws, p):
        s = self._live(p)
        if not str(p.get("cwd", "")).startswith("/"):
            raise _RpcError(4017, "cwd invalid")
        s.cwd = p["cwd"]
        return {"cwd": s.cwd}

    async def _m_session_interrupt(self, ws, p):
        s = self._live(p)
        rid = p["session_id"]
        self._queue.pop(rid, None)
        task = self._running.get(rid)
        if task is not None:
            task.cancel()
        return {"status": "interrupted"}

    async def _m_prompt_submit(self, ws, p):
        s = self._live(p)
        rid = p["session_id"]
        if p.get("context") is not None and len(p["context"]) > 16000:
            raise _RpcError(4080, "context too long")
        if rid in self._running:
            if not p.get("queued"):
                raise _RpcError(4009, "busy (the fake only queues)")
            self._queue.setdefault(rid, []).append(p.get("text", ""))
            return {"status": "queued"}
        self._start_turn(rid, s, p.get("text", ""))
        return {"status": "streaming"}

    # -- turns --------------------------------------------------------------

    def _start_turn(self, rid: str, s: FakeSession, text: str) -> None:
        task = asyncio.create_task(self._turn(rid, s, text))
        self._running[rid] = task

        def done(_t):
            if self._running.get(rid) is task:
                del self._running[rid]
            queue = self._queue.get(rid)
            if queue and not _t.cancelled():
                self._start_turn(rid, s, queue.pop(0))

        task.add_done_callback(done)

    async def _turn(self, rid: str, s: FakeSession, text: str) -> None:
        s.messages.append({"role": "user", "text": text, "row_id": next(self._rows)})
        usage = {"input": 10, "output": 5, "context_used": 15, "context_max": 32000, "avg_tps": 40.0}
        turn_id = f"t{next(self._ids)}"
        if text == "fail":
            await self.emit(rid, "message.start", {"turn_id": turn_id})
            await self.emit(rid, "message.complete", {"text": "", "status": "error", "failure_reason": "provider", "usage": usage})
            return
        try:
            await self.emit(rid, "message.start", {"turn_id": turn_id})
            reply = "Hello world"
            if text.startswith("tool"):
                await self.emit(rid, "tool.generating", {"name": "terminal"})
                await self.emit(rid, "tool.start", {"tool_id": "call_1", "name": "terminal", "context": "ls /tmp", "args": {"command": "ls /tmp"}})
                await self.emit(rid, "tool.complete", {"tool_id": "call_1", "name": "terminal", "duration_s": 0.2, "summary": "listed", "result_text": "a.txt"})
                s.messages.append({"role": "tool", "name": "terminal", "context": "ls /tmp", "tool_call_id": "call_1", "args": {"command": "ls /tmp"}})
            if text.startswith("approve"):
                answer = await self.ask(rid, "approval", {
                    "request_id": "appr-1", "command": "rm -rf /tmp/x", "description": "delete a folder",
                    "choices": ["once", "session", "always", "deny"], "tool_name": "terminal", "pattern_key": "rm",
                })
                self.answers.append(("approval", answer or {}))
                reply = f"choice={(answer or {}).get('choice')}"
            if text.startswith("clarify"):
                answer = await self.ask(rid, "clarify", {"questions": [
                    {"qid": "q1", "question": "Which color?", "choices": ["red", "blue"], "multi_select": False},
                ]})
                self.answers.append(("clarify", answer or {}))
                reply = f"answers={(answer or {}).get('answers')}"
            if text.startswith("hang"):
                await asyncio.sleep(3600)
            if text != "nostream":
                await self.emit(rid, "reasoning.delta", {"text": "thinking"})
                half = len(reply) // 2
                await self.emit(rid, "message.delta", {"text": reply[:half], "turn_id": turn_id, "segment": 0, "offset": 0})
                await self.emit(rid, "message.delta", {"text": reply[half:], "turn_id": turn_id, "segment": 0, "offset": half})
            await self.emit(rid, "session.usage", {"usage": usage})
            s.messages.append({"role": "assistant", "text": reply, "row_id": next(self._rows)})
            await self.emit(rid, "message.complete", {"text": reply, "status": "complete", "usage": usage})
        except asyncio.CancelledError:
            await self.emit(rid, "message.complete", {"text": "", "status": "interrupted", "usage": usage})
            raise


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
