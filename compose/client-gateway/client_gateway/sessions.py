"""Which connections watch which session, fan-out, and first-answer-wins requests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .backend import RequestCancelled
from .errors import AcpError

if TYPE_CHECKING:
    from .connection import Connection

log = logging.getLogger("client_gateway.sessions")


class SessionHub:
    """The ``EventSink`` the backend writes to."""

    def __init__(self) -> None:
        self._watchers: dict[str, set[Connection]] = {}

    def attach(self, session_id: str, conn: Connection) -> None:
        self._watchers.setdefault(session_id, set()).add(conn)

    def detach(self, session_id: str, conn: Connection) -> None:
        conns = self._watchers.get(session_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                del self._watchers[session_id]

    def detach_all(self, conn: Connection) -> None:
        for sid in [s for s, conns in self._watchers.items() if conn in conns]:
            self.detach(sid, conn)

    def watchers(self, session_id: str) -> list[Connection]:
        return list(self._watchers.get(session_id, ()))

    async def update(self, session_id: str, update: dict, meta: dict | None = None) -> None:
        params: dict = {"sessionId": session_id, "update": update}
        if meta:
            params["_meta"] = meta
        await self.notify(session_id, "session/update", params)

    async def notify(self, session_id: str, method: str, params: dict) -> None:
        for conn in self.watchers(session_id):
            await conn.send_notification(method, params)

    async def request(self, session_id: str, method: str, params: dict) -> dict:
        conns = self.watchers(session_id)
        if not conns:
            raise RequestCancelled("no client is watching the session")
        pending = {asyncio.ensure_future(c.send_request(method, params)): c for c in conns}
        winner: dict | None = None
        try:
            while pending and winner is None:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    pending.pop(fut)
                    if winner is None and not fut.cancelled() and fut.exception() is None:
                        winner = fut.result()
                    elif fut.exception() is not None and not isinstance(fut.exception(), AcpError):
                        log.debug("%s to a watcher failed: %s", method, type(fut.exception()).__name__)
        finally:
            # The other clients' requests end: $/cancel_request tells them to stop asking.
            for fut in pending:
                fut.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if winner is None:
            raise RequestCancelled(f"no client answered {method}")
        return winner
