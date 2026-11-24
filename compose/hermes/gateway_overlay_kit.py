"""Shared helpers for the gateway overlays that extend prompt.submit and gateway.capabilities.

Stack-only, not upstream. client_context, turn_speaker and gateway_liveness
import it. Each of their patch scripts copies it to ``agent/gateway_overlay_kit.py``
beside the overlay it installs; host tests import it from compose/hermes.

A turn-scoped submit field (client context, speaker) is validated when
``prompt.submit`` arrives, stashed on the session record for the turn it starts,
and picked up by the turn-input step once the turn's agent is final.
``wrap_turn_scoped_submit`` and ``wrap_turn_scoped_prepare`` hold both halves;
the overlay supplies the check and what to do with the value.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

# Keys the gateway sets on its own prompt.submit calls (hosted rooms, turn authors).
INTERNAL_SUBMIT_KEYS = ("_hosted_task", "_hosted_terminal_callback", "_turn_author")


class Refuse(Exception):
    """Raised by a submit check to answer ``prompt.submit`` with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def is_ok(response: Any) -> bool:
    return isinstance(response, dict) and isinstance(response.get("result"), dict)


def is_internal_submit(params: dict) -> bool:
    return any(params.get(k) is not None for k in INTERNAL_SUBMIT_KEYS)


def uses_isolation(server: Any, session: dict) -> bool:
    try:
        return bool(server._session_uses_compute_host(
            session, server._load_dashboard_process_isolation_config()))
    except Exception:
        return False


def wrap_turn_scoped_submit(server: Any, orig: Callable, *, key: str,
                            check: Callable[[dict, dict], Any], busy_code: int, busy_noun: str,
                            label: str, logger: logging.Logger) -> Callable:
    """Wrap ``prompt.submit`` so ``check(params, session)``'s value rides the turn it starts.

    ``check`` returns the value to stash under ``session[key]``, ``None`` to pass the call
    through unchanged, or raises ``Refuse``. Non-dict params and unknown sessions go
    straight to upstream. A value is refused with *busy_code* while a turn runs, because a
    queued, steered or redirected prompt would apply it to another turn. If the reply did
    not start a new turn, the stashed value is dropped (and logged when the reply had a status).
    """

    def handler(rid, params: dict) -> dict:
        if not isinstance(params, dict):
            return orig(rid, params)
        session = server._sessions.get(params.get("session_id") or "")
        if session is None:
            return orig(rid, params)  # upstream answers 4001
        try:
            value = check(params, session)
        except Refuse as exc:
            if exc.data is None:
                return server._err(rid, exc.code, exc.message)
            return server._err(rid, exc.code, exc.message, exc.data)
        if value is None:
            return orig(rid, params)
        if session.get("running"):
            return server._err(
                rid, busy_code,
                f"session is busy; a prompt with {busy_noun} cannot be queued, steered or redirected. "
                "Resubmit after message.complete.")

        session[key] = value
        response = orig(rid, params)
        status = response["result"].get("status") if is_ok(response) else None
        if status != "streaming" and session.get(key) is value:
            session.pop(key, None)
            if status is not None:
                logger.warning(
                    "%s dropped: prompt.submit for session %s answered %r, not a new turn",
                    label, params.get("session_id"), status)
        return response

    handler.__wrapped__ = orig
    return handler


def wrap_turn_scoped_prepare(orig: Callable, *, key: str, apply: Callable[[Any, dict, Any, Any], None],
                             label: str, logger: logging.Logger) -> Callable:
    """Wrap the turn-input step: take ``session[key]``, run upstream, then
    ``apply(agent, session, value, result)`` on the turn's final agent (after any model
    switch). ``value`` is ``None`` when the turn has none; ``result`` is ``None`` when
    upstream did not start the turn. A failing ``apply`` is logged and the turn goes on."""

    def prepare(sid, session, st, text, images):
        value = session.pop(key, None) if isinstance(session, dict) else None
        result = orig(sid, session, st, text, images)
        agent = getattr(st, "agent", None) or (session.get("agent") if isinstance(session, dict) else None)
        if agent is not None:
            try:
                apply(agent, session, value, result)
            except Exception:
                logger.warning("%s not applied for session %s", label, sid, exc_info=True)
        return result

    prepare.__wrapped__ = orig
    return prepare


def add_turn_note(agent: Any, note: str) -> None:
    """Append *note* to the turn's notes (upstream's ``_gateway_turn_context_notes`` channel)."""
    existing = getattr(agent, "_gateway_turn_context_notes", "") or ""
    agent._gateway_turn_context_notes = "\n\n".join(n for n in (existing, note) if n)


def extend_capabilities(fields: dict[str, type], module: str) -> None:
    """Add *fields* to the registered ``gateway.capabilities`` result model.

    Extends whatever is registered now, because several overlays add capabilities and the
    result model refuses fields it does not declare. A no-op once all fields are present.
    """
    from pydantic import create_model
    from tui_gateway.contracts import registry

    caps = registry.METHODS["gateway.capabilities"]
    if not set(fields) <= set(caps.result.model_fields):
        result = create_model(
            caps.result.__name__, __base__=caps.result, __module__=module,
            **{name: (kind, ...) for name, kind in fields.items()})
        registry.METHODS[caps.name] = registry.MethodContract(caps.name, caps.params, result, caps.doc)


def wrap_capabilities(handler: Callable, flags: dict | Callable[[], dict]) -> Callable:
    """Add *flags* (or what ``flags()`` returns at call time) to a ``gateway.capabilities`` result."""

    def caps(rid, params: dict) -> dict:
        response = handler(rid, params)
        if is_ok(response):
            response["result"].update(flags() if callable(flags) else flags)
        return response

    caps.__wrapped__ = handler
    return caps
