"""An in-memory Backend for the gateway's front-half tests (no Hermes)."""

from __future__ import annotations

import asyncio
import itertools

from client_gateway.backend import Backend, Identity, RequestCancelled
from client_gateway.errors import RESOURCE_NOT_FOUND, AcpError

DEFAULT = Identity(slug=None, name="hermes", title="Assistant", profile="default")
AGENT = Identity(slug="alpha", name="alpha", title="Alpha", profile="alpha")


class FakeBackend(Backend):
    def __init__(self) -> None:
        self.identities = {None: DEFAULT, "alpha": AGENT}
        self.sessions: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.permission_answers: list[dict] = []
        self._ids = itertools.count(1)
        self._turns: dict[str, asyncio.Event] = {}

    async def describe_identity(self, slug):
        return self.identities.get(slug)

    def _get(self, identity, sid):
        s = self.sessions.get(sid)
        if s is None or s["profile"] != identity.profile:
            raise AcpError(RESOURCE_NOT_FOUND, "session not found", "session_not_found")
        return s

    async def list_sessions(self, identity, params):
        return {
            "sessions": [
                {"sessionId": sid, "cwd": s["cwd"], "title": s.get("title")}
                for sid, s in self.sessions.items()
                if s["profile"] == identity.profile
            ]
        }

    async def new_session(self, identity, params):
        sid = f"s{next(self._ids)}"
        self.sessions[sid] = {"profile": identity.profile, "cwd": params.get("cwd", "/"), "history": []}
        return {"sessionId": sid}

    async def load_session(self, identity, params, replay):
        s = self._get(identity, params["sessionId"])
        if replay:
            for text in s["history"]:
                await self.sink.update(
                    params["sessionId"],
                    {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
                )
        return {}

    async def close_session(self, identity, session_id):
        self._get(identity, session_id)

    async def delete_session(self, identity, session_id):
        self._get(identity, session_id)
        del self.sessions[session_id]

    async def prompt(self, identity, session_id, blocks, meta):
        s = self._get(identity, session_id)
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        stop = asyncio.Event()
        self._turns[session_id] = stop
        try:
            if text.startswith("ask"):
                try:
                    answer = await self.sink.request(
                        session_id,
                        "session/request_permission",
                        {
                            "sessionId": session_id,
                            "toolCall": {"toolCallId": "t1", "title": "run ls", "kind": "execute"},
                            "options": [
                                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                            ],
                        },
                    )
                except RequestCancelled:
                    answer = {"outcome": {"outcome": "cancelled"}}
                self.permission_answers.append(answer)
            if text.startswith("wait"):
                await stop.wait()
                return {"stopReason": "cancelled"}
            reply = f"echo: {text}"
            s["history"].append(reply)
            await self.sink.update(
                session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}}
            )
            return {"stopReason": "end_turn"}
        finally:
            self._turns.pop(session_id, None)

    async def cancel(self, identity, session_id):
        self.cancelled.append(session_id)
        ev = self._turns.get(session_id)
        if ev:
            ev.set()
