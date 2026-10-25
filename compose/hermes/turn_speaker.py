"""Speaker names on assistant rows (assistant-stack overlay).

Not upstream Hermes. A client that runs several agents in one session (for
example a Boundary channel) can name who answers each turn:

- ``speaker`` on ``prompt.submit`` is ``{id, display_name}``, cleaned like
  upstream's turn authors. It is refused while a turn runs, because a queued,
  steered or redirected prompt would label another turn's reply.
- Each new assistant row with text that the turn writes gets
  ``display_metadata.speaker``, which ``session.history`` returns, and an
  ``api_content`` sidecar that starts with a ``[speaker: ...]`` line. Upstream
  replays a stored row's sidecar on every later request, so the label is
  byte-stable and survives reloads. The label goes on when the row is first
  written, before any later request can send that row.
- The turn gets a note (upstream's per-turn note channel) naming its speaker
  and explaining the labels. A label the model copies onto the start of its
  own reply is removed from the stored row (not from the live stream).

``gateway.capabilities`` advertises ``turn_speaker``.

``install(server)`` declares the contract field, wraps ``prompt.submit``, the
turn-input step and ``session_persistence._db_flush_row`` at runtime, so no
upstream file other than the server.py install tail is edited.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

SPEAKER_INVALID = 4084
SPEAKER_BUSY = 4085
SPEAKER_ISOLATED = 4086

SPEAKER_FIELDS = ("id", "display_name")
TURN_HEADER = "## Speaker"

# Session and agent keys this overlay owns.
TURN_KEY = "_turn_speaker"
AGENT_KEY = "_assistant_stack_speaker"

_LEADING_LABEL = re.compile(r"\A\s*\[speaker:[^\]\n]*\][ \t]*\n?")


def coerce_speaker(value: Any) -> dict[str, str] | None:
    """``None`` clears. Otherwise an object with ``id`` and/or ``display_name``."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("speaker must be an object or null")
    extra = set(value) - set(SPEAKER_FIELDS)
    if extra:
        raise ValueError(f"speaker has unknown fields: {', '.join(sorted(extra))}")
    from agent.turn_author import _clean_text

    speaker = {}
    for field in SPEAKER_FIELDS:
        raw = value.get(field)
        if raw is not None and not isinstance(raw, str):
            raise ValueError(f"speaker.{field} must be a string")
        cleaned = _clean_text(raw)
        if cleaned:
            # A bracket or newline would end the label early.
            speaker[field] = cleaned.replace("[", "(").replace("]", ")").replace("\n", " ")
    if not speaker:
        raise ValueError("speaker needs an id or a display_name")
    return speaker


def who(speaker: dict[str, str]) -> str:
    name, sid = speaker.get("display_name"), speaker.get("id")
    return f"{name} ({sid})" if name and sid and name != sid else name or sid


def label(speaker: dict[str, str]) -> str:
    return f"[speaker: {who(speaker)}]"


def turn_note(speaker: dict[str, str]) -> str:
    return (
        f"{TURN_HEADER}\n\n"
        f"You are answering as {who(speaker)}. Earlier assistant messages in this "
        "conversation start with a [speaker: ...] line naming the agent that wrote them, and "
        "some were written by other agents. Don't start your reply with a speaker line.")


def strip_leading_label(text: str) -> str:
    return _LEADING_LABEL.sub("", text, count=1)


def stamp_row(msg: dict, speaker: dict[str, str]) -> None:
    """Label one assistant message in place. Rows that already name a speaker,
    rows read back from the DB and display-only rows (summaries, placeholders)
    are left alone."""
    if msg.get("role") != "assistant" or isinstance(msg.get("_row_id"), int) or msg.get("display_kind"):
        return
    metadata = msg.get("display_metadata")
    if isinstance(metadata, dict) and "speaker" in metadata:
        return
    content = msg.get("content")
    sidecar = msg.get("api_content")
    wire = sidecar if isinstance(sidecar, str) and sidecar else content
    if not isinstance(wire, str) or not wire.strip():
        return  # tool calls with no text: nothing to label
    if isinstance(content, str):
        msg["content"] = strip_leading_label(content)
    msg["api_content"] = f"{label(speaker)}\n{strip_leading_label(wire)}"
    msg["display_metadata"] = {**(metadata if isinstance(metadata, dict) else {}), "speaker": dict(speaker)}


def wrap_db_flush_row(orig: Callable) -> Callable:
    def flush_row(agent, msg, is_current_turn_user):
        speaker = getattr(agent, AGENT_KEY, None)
        if speaker and isinstance(msg, dict):
            try:
                stamp_row(msg, speaker)
            except Exception:
                logger.warning("speaker label not applied", exc_info=True)
        return orig(agent, msg, is_current_turn_user)

    flush_row.__wrapped__ = orig
    return flush_row


def _is_ok(response: Any) -> bool:
    return isinstance(response, dict) and isinstance(response.get("result"), dict)


def _uses_isolation(server: Any, session: dict) -> bool:
    try:
        return bool(server._session_uses_compute_host(
            session, server._load_dashboard_process_isolation_config()))
    except Exception:
        return False


_INTERNAL_SUBMIT_KEYS = ("_hosted_task", "_hosted_terminal_callback", "_turn_author")


def wrap_prompt_submit(server: Any, orig: Callable) -> Callable:
    def handler(rid, params: dict) -> dict:
        if not isinstance(params, dict) or params.get("speaker") is None:
            return orig(rid, params)
        session = server._sessions.get(params.get("session_id") or "")
        if session is None or any(params.get(k) is not None for k in _INTERNAL_SUBMIT_KEYS):
            return orig(rid, params)
        try:
            speaker = coerce_speaker(params.get("speaker"))
        except ValueError as exc:
            return server._err(rid, SPEAKER_INVALID, str(exc))
        if _uses_isolation(server, session):
            return server._err(rid, SPEAKER_ISOLATED, "speaker is not supported on isolated compute-host turns")
        if session.get("running"):
            return server._err(
                rid, SPEAKER_BUSY,
                "session is busy; a prompt with a speaker cannot be queued, steered or redirected. "
                "Resubmit after message.complete.")

        session[TURN_KEY] = speaker
        response = orig(rid, params)
        status = response["result"].get("status") if _is_ok(response) else None
        if status != "streaming" and session.get(TURN_KEY) is speaker:
            session.pop(TURN_KEY, None)
            if status is not None:
                logger.warning(
                    "speaker dropped: prompt.submit for session %s answered %r, not a new turn",
                    params.get("session_id"), status)
        return response

    handler.__wrapped__ = orig
    return handler


def wrap_prepare_turn_input(orig: Callable) -> Callable:
    """Bind the turn's speaker (or none) to the turn's final agent."""

    def prepare(sid, session, st, text, images):
        speaker = session.pop(TURN_KEY, None) if isinstance(session, dict) else None
        result = orig(sid, session, st, text, images)
        agent = getattr(st, "agent", None) or (session.get("agent") if isinstance(session, dict) else None)
        if agent is not None:
            try:
                # Every turn rebinds, so a cached agent never labels a later turn with an old speaker.
                setattr(agent, AGENT_KEY, speaker if result is not None else None)
                if speaker and result is not None:
                    existing = getattr(agent, "_gateway_turn_context_notes", "") or ""
                    notes = [n for n in (existing, turn_note(speaker)) if n]
                    agent._gateway_turn_context_notes = "\n\n".join(notes)
            except Exception:
                logger.warning("speaker not applied for session %s", sid, exc_info=True)
        return result

    prepare.__wrapped__ = orig
    return prepare


CAPABILITIES = {"turn_speaker": True}


def declare_contracts() -> None:
    """Add ``speaker`` to prompt.submit and the capability flag to the
    capabilities result, extending whatever other overlays registered."""
    from pydantic import create_model
    from tui_gateway.contracts import registry
    from tui_gateway.contracts.base import JsonValue

    contract = registry.METHODS["prompt.submit"]
    if "speaker" not in contract.params.model_fields:
        params = create_model(
            contract.params.__name__, __base__=contract.params, __module__=__name__,
            speaker=(dict[str, JsonValue] | None, None))
        registry.METHODS["prompt.submit"] = registry.MethodContract(
            "prompt.submit", params, contract.result, contract.doc)
    caps = registry.METHODS["gateway.capabilities"]
    if not set(CAPABILITIES) <= set(caps.result.model_fields):
        result = create_model(
            caps.result.__name__, __base__=caps.result, __module__=__name__,
            **{key: (bool, ...) for key in CAPABILITIES})
        registry.METHODS[caps.name] = registry.MethodContract(caps.name, caps.params, result, caps.doc)


def wrap_capabilities(handler: Callable) -> Callable:
    def caps(rid, params: dict) -> dict:
        response = handler(rid, params)
        if _is_ok(response):
            response["result"].update(CAPABILITIES)
        return response

    caps.__wrapped__ = handler
    return caps


def install(server: Any) -> None:
    if getattr(server, "_turn_speaker_installed", False):
        return
    from agent import session_persistence

    declare_contracts()
    methods = server._methods
    methods["prompt.submit"] = wrap_prompt_submit(server, methods["prompt.submit"])
    server.register_method("gateway.capabilities", wrap_capabilities(methods["gateway.capabilities"]))
    server._prepare_turn_input = wrap_prepare_turn_input(server._prepare_turn_input)
    # _db_flush_collect looks _db_flush_row up in its module globals at call time.
    session_persistence._db_flush_row = wrap_db_flush_row(session_persistence._db_flush_row)
    server._turn_speaker_installed = True
