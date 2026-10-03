"""Per-session history window for the Hermes gateway overlay.

The stored transcript is not modified. Callers pass an estimator
``estimate(messages) -> int`` (Hermes uses ``estimate_messages_tokens_rough``).
A budget of ``None`` leaves the message list unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

Estimate = Callable[[Sequence[Dict[str, Any]]], int]

REPORT_KEYS = (
    "history_budget",
    "history_tokens",
    "history_first_row_id",
    "history_summarised",
)


def empty_report(budget: Optional[int] = None) -> Dict[str, Any]:
    return {
        "history_budget": budget,
        "history_tokens": None,
        "history_first_row_id": None,
        "history_summarised": False,
    }


def coerce_history_budget(value: Any) -> Optional[int]:
    """Accept a positive int or null. Anything else is an RPC error."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("history_budget must be a positive integer or null")
    return value


def message_row_id(message: Dict[str, Any]) -> Optional[int]:
    raw = message.get("_row_id", message.get("row_id"))
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def agent_has_history_budget(agent: Any) -> bool:
    """True when this agent must not archive its stored transcript.

    An explicit ``history_budget`` attribute wins (including ``None``).
    A missing attribute falls back to the session row's ``model_config``.
    """
    if agent is None:
        return False
    attrs = getattr(agent, "__dict__", {})
    if isinstance(attrs, dict) and "history_budget" in attrs:
        return _positive(attrs.get("history_budget"))
    db = getattr(agent, "session_db", None)
    sid = getattr(agent, "session_id", None)
    getter = getattr(db, "get_session_model_config_value", None)
    if not callable(getter) or not sid:
        return False
    try:
        stored = getter(sid, "history_budget")
    except Exception:
        return False
    return _positive(stored)


@dataclass
class HistoryWindow:
    """History portion of one request (summary message, then verbatim rows)."""

    messages: List[Dict[str, Any]]
    report: Dict[str, Any]
    excluded: List[Dict[str, Any]]
    summary_through_row_id: Optional[int]
    needs_refresh: bool


def apply_history_budget(
    messages: List[Dict[str, Any]],
    budget: Optional[int],
    *,
    estimate: Estimate,
    summary_text: Optional[str] = None,
    summary_covers_through: Optional[int] = None,
    fit_tokens: Optional[int] = None,
) -> HistoryWindow:
    """Keep a newest-first tail whose estimated tokens fit in ``budget``.

    ``fit_tokens``, when set, tightens the ceiling so the history still fits
    the model window after the system prompt and tools. The newest user
    message is always kept, and an assistant tool-call is never split from
    its tool results. Those two cases may report ``history_tokens`` above
    the budget. Summary text, when it covers the excluded prefix, counts
    toward the budget and is capped at about a quarter of the ceiling.
    """
    if budget is None:
        return HistoryWindow(messages, empty_report(None), [], None, False)

    limit = budget if fit_tokens is None else min(budget, fit_tokens)
    if limit < 0:
        limit = 0
    groups = _groups(messages)
    tail_groups, excluded_groups = _pack(groups, limit, estimate)
    excluded = [msg for group in excluded_groups for msg in group]
    through = _last_row_id(excluded)
    summary = _usable_summary(
        summary_text, through, summary_covers_through, limit, estimate
    )
    # A summary that still covers the tighter cut shares the ceiling with the
    # verbatim tail. If it would drop rows the cache does not cover yet, keep
    # the wider tail and ask the caller to refresh before injecting it.
    if summary and excluded:
        summary_cost = estimate([summary])
        tight_tail, tight_excluded_groups = _pack(
            groups, max(0, limit - summary_cost), estimate
        )
        tight_excluded = [msg for group in tight_excluded_groups for msg in group]
        tight_through = _last_row_id(tight_excluded)
        if _summary_covers(summary_covers_through, tight_through):
            tail_groups = tight_tail
            excluded = tight_excluded
            through = tight_through
        else:
            summary = None
            through = tight_through
            excluded = tight_excluded
    verbatim = [dict(msg) for group in tail_groups for msg in group]
    sent = _with_summary(summary, verbatim, excluded)
    summarised = bool(summary and excluded and sent and _is_summary_message(sent[0], summary))
    report = _report(budget, sent, verbatim, estimate, summarised=summarised)
    needs_refresh = bool(excluded) and not _summary_covers(summary_covers_through, through)
    return HistoryWindow(sent, report, [dict(msg) for msg in excluded], through, needs_refresh)


def _with_summary(
    summary: Optional[Dict[str, Any]],
    verbatim: List[Dict[str, Any]],
    excluded: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not summary or not excluded:
        return list(verbatim)
    next_role = str(verbatim[0].get("role")) if verbatim else "user"
    # Adjacent user rows are merged by Hermes after this hook. An assistant
    # summary keeps the first verbatim user row intact.
    role = "assistant" if next_role == "user" else "user"
    return [{"role": role, "content": summary["content"]}, *verbatim]


def _is_summary_message(message: Dict[str, Any], summary: Dict[str, Any]) -> bool:
    return message.get("content") == summary.get("content") and message_row_id(message) is None


def _usable_summary(
    summary_text: Optional[str],
    through: Optional[int],
    summary_covers_through: Optional[int],
    limit: int,
    estimate: Estimate,
) -> Optional[Dict[str, Any]]:
    if not excluded_prefix_needs_summary(summary_text, through, summary_covers_through):
        return None
    cap = max(1, limit // 4) if limit >= 4 else limit
    if cap <= 0:
        return None
    text = _truncate_to_tokens(summary_text or "", cap, estimate)
    if not text:
        return None
    return {"role": "user", "content": text}


def excluded_prefix_needs_summary(
    summary_text: Optional[str],
    through: Optional[int],
    summary_covers_through: Optional[int],
) -> bool:
    """True when a cached summary may be injected for this excluded prefix."""
    if not summary_text or not str(summary_text).strip():
        return False
    return _summary_covers(summary_covers_through, through)


def _summary_covers(summary_covers_through: Optional[int], through: Optional[int]) -> bool:
    """True when the cached summary already includes the excluded prefix.

    No excluded row id means there is nothing further to cover. A cache
    without a coverage id does not cover a real prefix.
    """
    if through is None:
        return True
    if summary_covers_through is None:
        return False
    return summary_covers_through >= through


def _report(
    budget: int,
    sent: List[Dict[str, Any]],
    verbatim: List[Dict[str, Any]],
    estimate: Estimate,
    *,
    summarised: bool,
) -> Dict[str, Any]:
    first = next((message_row_id(msg) for msg in verbatim if message_row_id(msg) is not None), None)
    tokens = estimate(sent) if sent else 0
    return {
        "history_budget": budget,
        "history_tokens": tokens,
        "history_first_row_id": first,
        "history_summarised": summarised,
    }


def _groups(messages: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Assistant tool-call plus the following tool results stay one group."""
    groups: List[List[Dict[str, Any]]] = []
    index = 0
    count = len(messages)
    while index < count:
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            end = index + 1
            while end < count and messages[end].get("role") == "tool":
                end += 1
            groups.append(list(messages[index:end]))
            index = end
        else:
            groups.append([message])
            index += 1
    return groups


def _pack(
    groups: Sequence[Sequence[Dict[str, Any]]],
    token_limit: int,
    estimate: Estimate,
) -> tuple[List[Sequence[Dict[str, Any]]], List[Sequence[Dict[str, Any]]]]:
    """Return ``(kept groups, excluded prefix groups)``."""
    if not groups:
        return [], []
    must = len(groups) - 1
    for index in range(len(groups) - 1, -1, -1):
        if any(msg.get("role") == "user" for msg in groups[index]):
            must = index
            break
    used = sum(estimate(group) for group in groups[must:])
    start = must
    index = must - 1
    while index >= 0:
        cost = estimate(groups[index])
        if used + cost <= token_limit:
            used += cost
            start = index
            index -= 1
            continue
        break
    return list(groups[start:]), list(groups[:start])


def _last_row_id(messages: Sequence[Dict[str, Any]]) -> Optional[int]:
    for message in reversed(messages):
        row_id = message_row_id(message)
        if row_id is not None:
            return row_id
    return None


def _truncate_to_tokens(text: str, cap: int, estimate: Estimate) -> str:
    if cap <= 0 or not text:
        return ""
    if estimate([{"role": "user", "content": text}]) <= cap:
        return text
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        chunk = text[:mid]
        if estimate([{"role": "user", "content": chunk}]) <= cap:
            best = chunk
            lo = mid + 1
        else:
            hi = mid - 1
    return best.strip()


def _positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
