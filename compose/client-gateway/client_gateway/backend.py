"""The seam between the runtime-neutral front half and a runtime.

A backend speaks ACP shapes: ``new_session`` returns a ``NewSessionResponse``
body, events reach the front half as ``SessionUpdate`` objects through the
``EventSink``, and server requests (permission, elicitation) go through the
sink's ``request``, which resolves with the first client answer. Hermes is the
only backend for now (``client_gateway.hermes``); a runtime that speaks ACP
natively later gets a thin one that proxies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Identity:
    """Who answers on an endpoint. ``slug`` is None for the built-in assistant."""

    slug: str | None
    name: str
    title: str
    profile: str


class EventSink(Protocol):
    async def update(self, session_id: str, update: dict, meta: dict | None = None) -> None:
        """Send ``session/update`` to every connection watching the session."""

    async def request(self, session_id: str, method: str, params: dict) -> dict:
        """Ask every watching connection; the first answer wins and the others are
        cancelled. Raises ``RequestCancelled`` when nobody is watching or every
        client cancels."""

    async def notify(self, session_id: str, method: str, params: dict) -> None:
        """Send an extension notification to every connection watching the session."""


class RequestCancelled(Exception):
    """A server request ended without an answer."""


class Backend(ABC):
    """Async throughout. Errors are raised as ``errors.AcpError``."""

    sink: EventSink

    def bind(self, sink: EventSink) -> None:
        self.sink = sink

    async def start(self) -> None:
        """Connect upstream; called once before the gateway serves."""

    async def stop(self) -> None:
        """Release upstream resources."""

    @abstractmethod
    async def describe_identity(self, slug: str | None) -> Identity | None:
        """The identity behind an endpoint, or None when it doesn't exist."""

    @abstractmethod
    async def list_sessions(self, identity: Identity, params: dict) -> dict:
        """A ``ListSessionsResponse`` body."""

    @abstractmethod
    async def new_session(self, identity: Identity, params: dict) -> dict:
        """A ``NewSessionResponse`` body; params are ``NewSessionRequest``."""

    @abstractmethod
    async def load_session(self, identity: Identity, params: dict, replay: bool) -> dict:
        """``session/load`` (replay=True, history sent through the sink before
        returning) or ``session/resume`` (replay=False). Returns the response body."""

    @abstractmethod
    async def close_session(self, identity: Identity, session_id: str) -> None: ...

    @abstractmethod
    async def delete_session(self, identity: Identity, session_id: str) -> None: ...

    @abstractmethod
    async def prompt(self, identity: Identity, session_id: str, blocks: list, meta: dict) -> dict:
        """Runs one turn; updates go through the sink. Returns a ``PromptResponse`` body."""

    @abstractmethod
    async def cancel(self, identity: Identity, session_id: str) -> None: ...
