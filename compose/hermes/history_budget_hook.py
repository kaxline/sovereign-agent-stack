"""Gateway glue for the per-session history budget.

Loaded from the patched Hermes tree. The pure window lives in
``history_budget`` so it can be tested without importing Hermes.

``install(server)`` runs from the tui_gateway/server.py tail. It declares the
budget fields on the wire contracts and wraps ``session.context_breakdown``
on the live server module, so those two need no text anchors.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from agent.history_budget import (
    HistoryWindow,
    agent_has_history_budget,
    apply_history_budget,
    empty_report,
    message_row_id,
)

logger = logging.getLogger(__name__)

_SUMMARY_PASSES = 2


def stamp_agent_budget(agent: Any, session: Any, session_db: Any, session_id: Any) -> None:
    """Copy the stored budget onto the agent and its compressor."""
    budget = _budget_from_session(session)
    if budget is None and not (isinstance(session, dict) and "history_budget" in session):
        budget = _budget_from_db(session_db, session_id)
    if not _positive(budget):
        budget = None
    agent.history_budget = budget
    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None:
        compressor._history_budget_blocks_archive = budget is not None
        compressor._history_budget_agent = agent


def select_context(
    compressor: Any,
    request_messages: list,
    *,
    conversation_messages: Any = None,
    incoming_message: Any = None,
    budget_tokens: int = 0,
) -> Optional[list]:
    """Request-only history window. ``None`` leaves the request unchanged."""
    del incoming_message, budget_tokens
    try:
        budget = _compressor_budget(compressor)
        compressor._history_budget_blocks_archive = budget is not None
        if budget is None:
            compressor._history_budget_report = empty_report(None)
            return None
        return _window_request(compressor, request_messages, conversation_messages, budget)
    except Exception:
        logger.warning("history budget select_context failed", exc_info=True)
        compressor._history_budget_report = empty_report(
            _positive_or_none(getattr(compressor, "_history_budget_runtime", None))
        )
        return None


def breakdown_fields(session: Any, agent: Any, history: Any) -> dict:
    """Next-reply cut from the current transcript. Does not call the summarizer."""
    budget = _budget_from_agent(agent)
    if budget is None:
        budget = _budget_from_session(session)
    if budget is None and not (isinstance(session, dict) and "history_budget" in session):
        budget = _budget_from_db(getattr(agent, "session_db", None), getattr(agent, "session_id", None))
    if budget is None:
        return empty_report(None)
    summary, through = _summary_cache(session, agent)
    window = apply_history_budget(
        list(history or []),
        budget,
        estimate=_estimate,
        summary_text=summary,
        summary_covers_through=through,
    )
    return window.report


def complete_fields(agent: Any) -> dict:
    """Cut actually sent on the turn that just finished."""
    compressor = getattr(agent, "context_compressor", None)
    report = getattr(compressor, "_history_budget_report", None) if compressor is not None else None
    if isinstance(report, dict) and "history_budget" in report:
        return {key: report.get(key) for key in empty_report()}
    budget = _budget_from_agent(agent)
    return empty_report(budget)


def _window_request(compressor: Any, request_messages: list, conversation_messages: Any, budget: int) -> Optional[list]:
    conversation = [msg for msg in (conversation_messages or []) if isinstance(msg, dict)]
    if not conversation or len(request_messages) < len(conversation):
        window = apply_history_budget([], budget, estimate=_estimate)
        compressor._history_budget_report = window.report
        return None
    head = list(request_messages[:-len(conversation)])
    history = _stamp_row_ids(request_messages[-len(conversation):], conversation)
    summary, through = _summary_cache_from_compressor(compressor)
    fit = _fit_tokens(compressor, head, budget)
    window = _refresh_until_covered(compressor, history, budget, summary, through, fit)
    compressor._history_budget_report = window.report
    if not window.excluded and not window.report.get("history_summarised"):
        return None
    cleaned = []
    for message in window.messages:
        copy = dict(message)
        copy.pop("_row_id", None)
        copy.pop("row_id", None)
        cleaned.append(copy)
    return head + cleaned


def _refresh_until_covered(
    compressor: Any,
    history: list,
    budget: int,
    summary: Optional[str],
    through: Optional[int],
    fit: Optional[int],
) -> HistoryWindow:
    window = apply_history_budget(
        history, budget, estimate=_estimate,
        summary_text=summary, summary_covers_through=through, fit_tokens=fit,
    )
    for _ in range(_SUMMARY_PASSES):
        if not window.needs_refresh:
            return window
        pending = _rows_after(window.excluded, through)
        fresh = _summarize(compressor, pending or window.excluded, summary)
        if not fresh:
            return window
        summary = fresh
        through = window.summary_through_row_id
        _store_summary(compressor, summary, through)
        window = apply_history_budget(
            history, budget, estimate=_estimate,
            summary_text=summary, summary_covers_through=through, fit_tokens=fit,
        )
    return window


def _rows_after(excluded: list, through: Optional[int]) -> list:
    """Rows the cached summary does not yet cover."""
    if through is None:
        return list(excluded)
    return [
        msg for msg in excluded
        if (message_row_id(msg) is None) or message_row_id(msg) > through
    ]


def _summarize(compressor: Any, excluded: list, previous: Optional[str]) -> Optional[str]:
    generate = getattr(compressor, "_generate_summary", None)
    if not callable(generate) or not excluded:
        return None
    compressor._previous_summary = previous or ""
    try:
        text = generate(excluded)
    except Exception as exc:
        if type(exc).__name__ == "AuxiliaryExplicitCancellation":
            raise
        logger.info("history budget summary failed: %s", exc)
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    return text.strip()


def _store_summary(compressor: Any, text: str, through: Optional[int]) -> None:
    compressor._history_summary = text
    compressor._history_summary_through_row_id = through
    db = getattr(compressor, "_session_db", None)
    sid = getattr(compressor, "_session_id", "")
    patch = getattr(db, "patch_session_model_config", None)
    if not sid or not callable(patch):
        return
    try:
        patch(sid, {
            "history_summary": text,
            "history_summary_through_row_id": through,
        })
    except Exception:
        logger.debug("history budget summary persist failed", exc_info=True)


def _fit_tokens(compressor: Any, head: list, budget: int) -> Optional[int]:
    context_length = int(getattr(compressor, "context_length", 0) or 0)
    if context_length <= 0:
        return None
    system_tokens = _estimate(head) if head else 0
    tool_tokens = _tool_tokens(compressor)
    room = context_length - system_tokens - tool_tokens
    if room >= budget:
        return None
    return max(0, room)


def _tool_tokens(compressor: Any) -> int:
    agent = getattr(compressor, "_history_budget_agent", None)
    tools = list(getattr(agent, "tools", None) or [])
    if not tools:
        return 0
    try:
        import json
        payload = json.dumps(tools, default=str)
    except Exception:
        payload = str(tools)
    return _estimate([{"role": "user", "content": payload}])


def _stamp_row_ids(history: list, conversation: list) -> list:
    stamped = []
    offset = len(history) - len(conversation)
    for index, message in enumerate(history):
        copy = dict(message)
        source_index = index - offset
        source = conversation[source_index] if 0 <= source_index < len(conversation) else None
        if isinstance(source, dict):
            row_id = message_row_id(source)
            if row_id is not None:
                copy["_row_id"] = row_id
        stamped.append(copy)
    return stamped


def _compressor_budget(compressor: Any) -> Optional[int]:
    agent = getattr(compressor, "_history_budget_agent", None)
    budget = _budget_from_agent(agent)
    if budget is not None:
        return budget
    if agent is not None and "history_budget" in getattr(agent, "__dict__", {}):
        return None
    return _budget_from_db(getattr(compressor, "_session_db", None), getattr(compressor, "_session_id", ""))


def _budget_from_agent(agent: Any) -> Optional[int]:
    if agent is None or not agent_has_history_budget(agent):
        return None
    return _positive_or_none(getattr(agent, "history_budget", None)) or _budget_from_db(
        getattr(agent, "session_db", None), getattr(agent, "session_id", None)
    )


def _budget_from_session(session: Any) -> Optional[int]:
    if not isinstance(session, dict):
        return None
    return _positive_or_none(session.get("history_budget"))


def _budget_from_db(session_db: Any, session_id: Any) -> Optional[int]:
    getter = getattr(session_db, "get_session_model_config_value", None)
    if not session_id or not callable(getter):
        return None
    try:
        return _positive_or_none(getter(session_id, "history_budget"))
    except Exception:
        logger.debug("history budget read failed", exc_info=True)
        return None


def _summary_cache(session: Any, agent: Any) -> tuple[Optional[str], Optional[int]]:
    compressor = getattr(agent, "context_compressor", None) if agent is not None else None
    text, through = _summary_cache_from_compressor(compressor) if compressor is not None else (None, None)
    if text:
        return text, through
    if isinstance(session, dict):
        cached = session.get("history_summary")
        row = session.get("history_summary_through_row_id")
        if isinstance(cached, str) and cached.strip():
            return cached, row if isinstance(row, int) and not isinstance(row, bool) else None
    return None, None


def _summary_cache_from_compressor(compressor: Any) -> tuple[Optional[str], Optional[int]]:
    if compressor is None:
        return None, None
    text = getattr(compressor, "_history_summary", None)
    through = getattr(compressor, "_history_summary_through_row_id", None)
    if isinstance(text, str) and text.strip():
        return text, through if isinstance(through, int) and not isinstance(through, bool) else None
    db = getattr(compressor, "_session_db", None)
    sid = getattr(compressor, "_session_id", "")
    getter = getattr(db, "get_session_model_config_value", None)
    if not sid or not callable(getter):
        return None, None
    try:
        cached = getter(sid, "history_summary")
        row = getter(sid, "history_summary_through_row_id")
    except Exception:
        logger.debug("history summary read failed", exc_info=True)
        return None, None
    if not isinstance(cached, str) or not cached.strip():
        return None, None
    row_id = row if isinstance(row, int) and not isinstance(row, bool) else None
    compressor._history_summary = cached
    compressor._history_summary_through_row_id = row_id
    return cached, row_id


def _estimate(messages: list) -> int:
    from agent.model_metadata import estimate_messages_tokens_rough
    return int(estimate_messages_tokens_rough(messages))


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive_or_none(value: Any) -> Optional[int]:
    return value if _positive(value) else None


# ── gateway install ───────────────────────────────────────────────────────────

BUDGET_METHODS = ("session.create", "session.resume")
REPORT_FIELDS = ("history_budget", "history_tokens", "history_first_row_id", "history_summarised")


def declare_contracts() -> None:
    """Add ``history_budget`` to the create/resume params and the report fields to the
    ``session.context_breakdown`` result and ``message.complete`` payload. Each extends
    whatever is registered now, because other overlays extend the same contracts."""
    from pydantic import create_model
    from tui_gateway.contracts import registry

    report = {
        "history_budget": (int | None, None),
        "history_tokens": (int | None, None),
        "history_first_row_id": (int | None, None),
        "history_summarised": (bool | None, None),
    }
    for name in BUDGET_METHODS:
        contract = registry.METHODS[name]
        if "history_budget" not in contract.params.model_fields:
            params = create_model(
                contract.params.__name__, __base__=contract.params, __module__=__name__,
                history_budget=(int | None, None))
            registry.METHODS[name] = registry.MethodContract(name, params, contract.result, contract.doc)
    breakdown = registry.METHODS["session.context_breakdown"]
    if not set(REPORT_FIELDS) <= set(breakdown.result.model_fields):
        result = create_model(
            breakdown.result.__name__, __base__=breakdown.result, __module__=__name__, **report)
        registry.METHODS[breakdown.name] = registry.MethodContract(
            breakdown.name, breakdown.params, result, breakdown.doc)
    complete = registry.EVENTS["message.complete"]
    if not set(REPORT_FIELDS) <= set(complete.payload.model_fields):
        payload = create_model(
            complete.payload.__name__, __base__=complete.payload, __module__=__name__, **report)
        registry.EVENTS[complete.name] = registry.EventContract(complete.name, payload, complete.doc)


def _breakdown_report(session: Any) -> dict:
    if not isinstance(session, dict):
        return empty_report(None)
    try:
        lock = session.get("history_lock")
        if lock is not None:
            with lock:
                history = list(session.get("history") or [])
        else:
            history = list(session.get("history") or [])
        return breakdown_fields(session, session.get("agent"), history)
    except Exception:
        logger.debug("history budget breakdown failed", exc_info=True)
        return empty_report(None)


def wrap_context_breakdown(server: Any, orig: Callable) -> Callable:
    """Add the next-reply cut to every successful ``session.context_breakdown`` result."""

    def handler(rid, params: dict) -> dict:
        response = orig(rid, params)
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict):
            sid = (params.get("session_id") if isinstance(params, dict) else None) or ""
            result.update(_breakdown_report(server._sessions.get(sid)))
        return response

    handler.__wrapped__ = orig
    return handler


def install(server: Any) -> None:
    if getattr(server, "_history_budget_installed", False):
        return
    declare_contracts()
    name = "session.context_breakdown"
    server.register_method(name, wrap_context_breakdown(server, server._methods[name]))
    server._history_budget_installed = True
