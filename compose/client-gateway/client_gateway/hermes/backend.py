"""The Hermes backend: ACP sessions on top of the legacy ``/api/ws`` protocol.

ACP's ``sessionId`` is Hermes' stored session id. The runtime id Hermes hands
out on create/resume stays in here; a session whose runtime id was reaped
(4001) or lost with an upstream drop is resumed before the next call. One
upstream socket serves every identity, with ``profile`` on each call for
agents (the built-in assistant sends none, as Boundary does today).

A turn is resolved by ``message.complete`` (or a bare ``error`` before the turn
started). Hermes queues a prompt sent during a turn when it carries
``queued: true``, so turns per session are a FIFO. Upstream events are handled
one at a time per session (a queue and a worker each), so the FIFO's head can't
change under a handler. A turn whose prompt the client cancelled stays in the
FIFO, marked abandoned, until Hermes completes it; its updates are dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..auth import Settings
from ..backend import Backend, Identity, RequestCancelled
from ..errors import INTERNAL_ERROR, INVALID_PARAMS, NS, AcpError
from . import projection
from .errors import to_acp
from .upstream import HermesError, Upstream, UpstreamDown

log = logging.getLogger("client_gateway.hermes")

SOURCE = "boundary"
LISTED_SOURCES = {None, "", "boundary"}
CONTEXT_URI = "stack:context"
MAX_CONTEXT_CHARS = 16_000
DEFAULT_IDENTITY = Identity(slug=None, name="hermes", title="Assistant", profile="default")


@dataclass
class Turn:
    future: asyncio.Future
    stream: projection.TurnStream = field(default_factory=projection.TurnStream)
    started: bool = False
    queued: bool = False
    abandoned: bool = False


@dataclass
class HSession:
    stored_id: str
    identity: Identity
    runtime_id: str | None = None
    last_seq: int = 0
    turns: deque = field(default_factory=deque)
    # A turn may still be running in Hermes after an upstream drop.
    dirty: bool = False
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None


class HermesBackend(Backend):
    def __init__(self, settings: Settings, upstream: Upstream | None = None) -> None:
        self.upstream = upstream or Upstream(
            settings.hermes_url,
            settings.dashboard_user,
            settings.dashboard_password,
            on_event=self._on_event,
            on_request=self._on_request,
            on_down=self._on_down,
            on_up=self._on_up,
        )
        self._sessions: dict[tuple[str, str], HSession] = {}  # (profile, stored id)
        self._by_runtime: dict[str, HSession] = {}
        self._requests: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        await self.upstream.start()

    async def stop(self) -> None:
        await self.upstream.stop()
        for s in self._sessions.values():
            if s.worker is not None:
                s.worker.cancel()

    # -- upstream plumbing ---------------------------------------------------

    async def _call(self, method: str, params: dict, timeout: float = 180.0) -> dict:
        try:
            return await self.upstream.call(method, params, timeout)
        except HermesError as exc:
            raise to_acp(method, exc.code, exc.message) from None
        except UpstreamDown:
            raise AcpError(INTERNAL_ERROR, "Hermes is not reachable", "runtime_unavailable") from None
        except asyncio.TimeoutError:
            raise AcpError(INTERNAL_ERROR, f"Hermes did not answer {method}", "runtime_timeout") from None

    @staticmethod
    def _profile(identity: Identity) -> dict:
        return {} if identity.slug is None else {"profile": identity.profile}

    def _session(self, identity: Identity, sid: str) -> HSession:
        key = (identity.profile, sid)
        s = self._sessions.get(key)
        if s is None:
            s = HSession(stored_id=sid, identity=identity)
            s.worker = asyncio.create_task(self._event_worker(s))
            self._sessions[key] = s
        return s

    def _forget(self, s: HSession) -> None:
        self._unbind_runtime(s)
        self._sessions.pop((s.identity.profile, s.stored_id), None)
        if s.worker is not None:
            s.worker.cancel()

    def _bind_runtime(self, s: HSession, runtime_id: str) -> None:
        if s.runtime_id and s.runtime_id != runtime_id:
            self._by_runtime.pop(s.runtime_id, None)
        s.runtime_id = runtime_id
        self._by_runtime[runtime_id] = s

    def _unbind_runtime(self, s: HSession) -> None:
        if s.runtime_id:
            self._by_runtime.pop(s.runtime_id, None)
        s.runtime_id = None

    async def _resume(self, s: HSession, extra: dict | None = None, omit_messages: bool = True) -> dict:
        params = {
            "session_id": s.stored_id,
            "source": SOURCE,
            "omit_messages": omit_messages,
            **self._profile(s.identity),
            **(extra or {}),
        }
        res = await self._call("session.resume", params)
        self._bind_runtime(s, res["session_id"])
        if s.dirty:
            s.dirty = False
            if res.get("running") and not s.turns:
                # The turn that was running when the upstream dropped is still going.
                # Its client already got runtime_restarted; stop it, and keep a
                # placeholder at the FIFO's head for its message.complete.
                done = asyncio.get_running_loop().create_future()
                done.cancel()
                s.turns.append(Turn(future=done, started=True, abandoned=True))
                await self._quiet_interrupt(s)
        return res

    async def _quiet_interrupt(self, s: HSession) -> None:
        if s.runtime_id is None:
            log.info("session.interrupt skipped: no runtime id")
            return
        try:
            res = await self.upstream.call("session.interrupt", {"session_id": s.runtime_id}, timeout=30)
            log.info("session.interrupt %s (turns held: %d)", res.get("status"), len(s.turns))
        except (UpstreamDown, HermesError, asyncio.TimeoutError) as exc:
            log.info("session.interrupt failed: %s", type(exc).__name__)

    async def _interrupt(self, s: HSession) -> None:
        """Interrupt the running turn. Hermes drops the queued prompts, so they end
        here (before the await, so a message.complete handled meanwhile can't move
        one to the head); the running turn ends with its own message.complete."""
        queued = list(s.turns)[1:]
        for turn in queued:
            s.turns.remove(turn)
            if not turn.future.done():
                turn.future.set_result(("cancelled", None))
        await self._quiet_interrupt(s)

    def _end_turns(self, s: HSession, exc: Exception | None = None) -> None:
        while s.turns:
            turn = s.turns.popleft()
            if not turn.future.done():
                if exc is None:
                    turn.future.set_result(("cancelled", None))
                else:
                    turn.future.set_exception(exc)

    async def _live_call(self, s: HSession, method: str, params: dict) -> dict:
        """A call on the session's runtime id, resuming first when there is none or
        when Hermes reaped it (4001)."""
        for attempt in (0, 1):
            if s.runtime_id is None:
                await self._resume(s)
            try:
                return await self.upstream.call(method, {"session_id": s.runtime_id, **params})
            except HermesError as exc:
                if exc.code == 4001 and attempt == 0:
                    self._unbind_runtime(s)
                    continue
                raise to_acp(method, exc.code, exc.message) from None
            except UpstreamDown:
                raise AcpError(INTERNAL_ERROR, "Hermes is not reachable", "runtime_unavailable") from None
        raise AssertionError("unreachable")

    async def _on_down(self) -> None:
        for s in list(self._sessions.values()):
            self._unbind_runtime(s)
            if s.turns:
                s.dirty = True
            self._end_turns(s, AcpError(INTERNAL_ERROR, "Hermes restarted during the turn", "runtime_restarted"))
            while not s.events.empty():
                s.events.get_nowait()
        for task in self._requests.values():
            task.cancel()

    async def _on_up(self) -> None:
        """Sessions are resumed on their next call; nothing to do eagerly yet (phase 2
        resumes watched sessions and replays with session.events.since)."""

    # -- identities and sessions ----------------------------------------------

    async def describe_identity(self, slug: str | None) -> Identity | None:
        if slug is None:
            return DEFAULT_IDENTITY
        try:
            res = await self.upstream.call("profiles.list", {"include_sessions": False}, timeout=10)
        except (UpstreamDown, HermesError, asyncio.TimeoutError):
            # Can't tell; accept the upgrade and let calls fail with a reason.
            return Identity(slug=slug, name=slug, title=slug, profile=slug)
        for p in res.get("profiles") or []:
            if p.get("name") == slug and not p.get("is_default"):
                return Identity(slug=slug, name=slug, title=p.get("display_name") or slug, profile=slug)
        return None

    async def list_sessions(self, identity: Identity, params: dict) -> dict:
        res = await self._call("session.list", {"limit": 200, **self._profile(identity)})
        out = []
        for row in res.get("sessions") or []:
            if row.get("source") not in LISTED_SOURCES:
                continue
            info = {"sessionId": row["id"], "cwd": row.get("cwd") or "/", "title": row.get("title") or None}
            updated = _iso(row.get("last_active") or row.get("started_at"))
            if updated:
                info["updatedAt"] = updated
            meta = {k: row[k] for k in ("preview", "message_count") if row.get(k) is not None}
            if meta:
                info["_meta"] = {NS: {_camel(k): v for k, v in meta.items()}}
            out.append(info)
        return {"sessions": out}

    def _setup_params(self, params: dict) -> dict:
        """Hermes create/resume fields from ACP ``systemPrompt`` and stack ``_meta``."""
        out: dict = {}
        sp = params.get("systemPrompt")
        if sp is not None:
            if sp.get("mode") != "append":
                raise AcpError(INVALID_PARAMS, "only systemPrompt mode append is supported", "system_prompt_mode_unsupported")
            blocks = sp.get("content") or []
            if any(b.get("type") != "text" for b in blocks):
                raise AcpError(INVALID_PARAMS, "systemPrompt takes text blocks only", "system_prompt_invalid")
            text = "\n\n".join(b.get("text", "") for b in blocks)
            if len(text) > MAX_CONTEXT_CHARS:
                raise AcpError(INVALID_PARAMS, f"systemPrompt is over {MAX_CONTEXT_CHARS} characters", "context_invalid")
            out["context"] = text
        meta = (params.get("_meta") or {}).get(NS) or {}
        if "historyBudget" in meta:
            budget = meta["historyBudget"]
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
                raise AcpError(INVALID_PARAMS, "historyBudget must be a non-negative integer", "history_budget_invalid")
            out["history_budget"] = budget
        return out

    async def _mcp_report(self, identity: Identity, servers: list) -> dict | None:
        """Legacy session.create takes no MCP servers, so ACP's are checked against the
        profile's registrations by name. Per-session servers come with the canonical
        protocol (editor.mcp_servers)."""
        if not servers:
            return None
        names = [str(s.get("name")) for s in servers]
        registered: set[str] = set()
        try:
            res = await self.upstream.call("profiles.describe", {"name": identity.profile}, timeout=30)
            registered = {m.get("name") for m in res.get("mcp_servers") or [] if m.get("enabled", True)}
        except (UpstreamDown, HermesError, asyncio.TimeoutError):
            pass
        return {
            "applied": [n for n in names if n in registered],
            "skipped": [{"name": n, "reason": "mcp_server_not_registered"} for n in names if n not in registered],
        }

    async def new_session(self, identity: Identity, params: dict) -> dict:
        setup = self._setup_params(params)
        meta = (params.get("_meta") or {}).get(NS) or {}
        create = {"source": meta.get("source") or SOURCE, **self._profile(identity), **setup}
        if meta.get("title"):
            create["title"] = meta["title"]
        res = await self._call("session.create", create)
        s = self._session(identity, res["stored_session_id"])
        self._bind_runtime(s, res["session_id"])
        stack: dict = {}
        cwd = params.get("cwd")
        if cwd and cwd != "/":
            try:
                await self._live_call(s, "session.cwd.set", {"cwd": cwd})
                stack["cwd"] = {"applied": True}
            except AcpError as exc:
                stack["cwd"] = {"applied": False, "reason": exc.reason}
        report = await self._mcp_report(identity, params.get("mcpServers") or [])
        if report is not None:
            stack["mcpServers"] = report
        out: dict = {"sessionId": s.stored_id}
        if stack:
            out["_meta"] = {NS: stack}
        return out

    async def load_session(self, identity: Identity, params: dict, replay: bool) -> dict:
        s = self._session(identity, params["sessionId"])
        meta = (params.get("_meta") or {}).get(NS) or {}
        extra = self._setup_params(params)
        omit = (not replay) or bool(meta.get("omitMessages"))
        res = await self._resume(s, extra, omit_messages=omit)
        if replay and not omit:
            for update in projection.history_updates(res.get("messages") or []):
                await self.sink.update(s.stored_id, update)
        stack: dict = {}
        report = await self._mcp_report(identity, params.get("mcpServers") or [])
        if report is not None:
            stack["mcpServers"] = report
        if res.get("messages_omitted"):
            stack["messagesOmitted"] = res["messages_omitted"]
        return {"_meta": {NS: stack}} if stack else {}

    async def close_session(self, identity: Identity, session_id: str) -> None:
        s = self._sessions.get((identity.profile, session_id))
        if s is None:
            return
        if s.turns:
            await self._interrupt(s)
            self._end_turns(s)
        if s.runtime_id is None:
            return
        try:
            await self.upstream.call("session.close", {"session_id": s.runtime_id}, timeout=30)
        except (UpstreamDown, HermesError, asyncio.TimeoutError) as exc:
            log.info("session.close failed: %s", type(exc).__name__)
        self._unbind_runtime(s)

    async def delete_session(self, identity: Identity, session_id: str) -> None:
        await self.close_session(identity, session_id)
        await self._call("session.delete", {"session_id": session_id, **self._profile(identity)})
        s = self._sessions.get((identity.profile, session_id))
        if s is not None:
            self._forget(s)

    # -- turns -----------------------------------------------------------------

    @staticmethod
    def _prompt_text(blocks: list) -> tuple[str, str | None]:
        parts: list[str] = []
        context: str | None = None
        for b in blocks:
            kind = b.get("type")
            if kind == "text":
                parts.append(b.get("text", ""))
            elif kind == "resource":
                res = b.get("resource") or {}
                if res.get("uri") == CONTEXT_URI:
                    context = res.get("text") or ""
                elif "text" in res:
                    parts.append(f"<resource uri=\"{res.get('uri', '')}\">\n{res['text']}\n</resource>")
                else:
                    raise AcpError(INVALID_PARAMS, "binary resources are not supported", "prompt_content_unsupported")
            elif kind == "resource_link":
                parts.append(b.get("uri", ""))
            else:
                raise AcpError(INVALID_PARAMS, f"{kind} content is not supported", "prompt_content_unsupported")
        if context is not None and len(context) > MAX_CONTEXT_CHARS:
            raise AcpError(INVALID_PARAMS, f"context is over {MAX_CONTEXT_CHARS} characters", "context_invalid")
        return "\n\n".join(p for p in parts if p), context

    async def prompt(self, identity: Identity, session_id: str, blocks: list, meta: dict) -> dict:
        s = self._session(identity, session_id)
        text, context = self._prompt_text(blocks)
        submit: dict = {"text": text}
        if context is not None:
            submit["context"] = context
        turn = Turn(future=asyncio.get_running_loop().create_future())
        if s.runtime_id is None:
            # Resume first: after an upstream drop it may find a leftover turn,
            # which makes this prompt a queued one.
            await self._resume(s)
        # Always queued: Hermes reads it only on its busy path, and it clears
        # `running` after message.complete, so a prompt sent right after a turn can
        # find the session still busy. Without queued, Hermes' default busy mode
        # interrupts and redirects instead of queueing (seen live 2026-11-12).
        submit["queued"] = True
        ahead = bool(s.turns)
        s.turns.append(turn)
        try:
            res = await self._live_call(s, "prompt.submit", submit)
        except BaseException:
            # Not submitted, so Hermes has no turn for it.
            if turn in s.turns:
                s.turns.remove(turn)
            raise
        turn.queued = ahead and res.get("status") == "queued"
        try:
            stop, usage = await asyncio.shield(turn.future)
        except asyncio.CancelledError:
            # $/cancel_request or a closed socket: Hermes still runs the turn. Stop it
            # if it's the running one; either way it stays in the FIFO, abandoned,
            # until its message.complete arrives.
            turn.abandoned = True
            turn.future.cancel()
            if s.turns and s.turns[0] is turn:
                asyncio.create_task(self._interrupt(s))
            raise
        stack: dict = {}
        if turn.queued:
            stack["queued"] = True
        if usage:
            stack["usage"] = usage
        out: dict = {"stopReason": stop}
        if stack:
            out["_meta"] = {NS: stack}
        return out

    async def cancel(self, identity: Identity, session_id: str) -> None:
        s = self._sessions.get((identity.profile, session_id))
        if s is None or s.runtime_id is None or not s.turns:
            log.info("session/cancel with no running turn")
            return
        await self._interrupt(s)

    # -- events ----------------------------------------------------------------

    async def _on_event(self, params: dict) -> None:
        """Called once per upstream event, in arrival order. Hands the event to its
        session's queue without awaiting, so the order holds."""
        if params.get("type") == "request.cancel":
            task = self._requests.get(str((params.get("payload") or {}).get("id")))
            if task is not None:
                task.cancel()
            return
        s = self._by_runtime.get(params.get("session_id") or "")
        if s is not None:
            s.events.put_nowait(params)

    async def _event_worker(self, s: HSession) -> None:
        while True:
            params = await s.events.get()
            try:
                await self._handle_event(s, params)
            except Exception:
                log.exception("event %s for a session failed", params.get("type"))

    async def _emit(self, s: HSession, turn: Turn | None, updates: list[dict]) -> None:
        if turn is not None and turn.abandoned:
            return
        for update in updates:
            await self.sink.update(s.stored_id, update)

    async def _handle_event(self, s: HSession, params: dict) -> None:
        etype = params.get("type")
        if isinstance(params.get("seq"), int):
            s.last_seq = max(s.last_seq, params["seq"])
        payload = params.get("payload") or {}
        turn = s.turns[0] if s.turns else None
        if etype == "message.start" and turn is not None:
            turn.started = True
            return
        if etype == "message.complete":
            if turn is None:
                return
            s.turns.popleft()
            await self._emit(s, turn, turn.stream.finish(payload))
            status = payload.get("status")
            if turn.future.done():
                return
            if status == "error":
                turn.future.set_exception(
                    AcpError(
                        INTERNAL_ERROR,
                        "the turn failed",
                        "turn_failed",
                        failureReason=payload.get("failure_reason") or payload.get("error"),
                    )
                )
            else:
                turn.future.set_result((projection.STOP_REASONS.get(status, "end_turn"), payload.get("usage")))
            return
        if etype == "error":
            if turn is not None and not turn.started:
                s.turns.popleft()
                if not turn.future.done():
                    turn.future.set_exception(AcpError(INTERNAL_ERROR, "the turn failed", "turn_failed"))
            return
        if etype == "tool.generating":
            if turn is not None and turn.abandoned:
                return
            await self.sink.notify(
                s.stored_id,
                f"_{NS}/tool.generating",
                {"sessionId": s.stored_id, "name": payload.get("name")},
            )
            return
        stream = turn.stream if turn is not None else projection.TurnStream()
        await self._emit(s, turn, stream.project(etype, payload))

    async def _on_request(self, rid: str, method: str, params: dict) -> dict:
        s = self._by_runtime.get(params.get("session_id") or "")
        if method in ("secret", "sudo", "vault.unlock_prompt", "vault.save_login", "vault.code", "vault.create_login"):
            # Declined until _stack/secret lands (task 2.5); Hermes records a skip.
            return {"value": ""}
        if s is None:
            raise LookupError("request for a session the gateway doesn't hold")
        task = asyncio.current_task()
        self._requests[rid] = task
        try:
            if method == "approval":
                try:
                    answer = await self.sink.request(
                        s.stored_id, "session/request_permission", projection.permission_params(s.stored_id, params)
                    )
                except RequestCancelled:
                    answer = None
                return projection.approval_answer(answer)
            if method == "clarify":
                try:
                    answer = await self.sink.request(
                        s.stored_id, "elicitation/create", projection.clarify_params(s.stored_id, params)
                    )
                except RequestCancelled:
                    answer = None
                return projection.clarify_answer(answer)
            raise LookupError(f"no ACP form for {method}")
        finally:
            self._requests.pop(rid, None)


def _iso(value: object) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    return None


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)
