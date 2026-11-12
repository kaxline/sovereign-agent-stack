"""Hermes tui_gateway events and history rows → ACP ``SessionUpdate`` objects.

Pure functions: no I/O, so the mapping is tested on its own. The turn state
the stream needs (whether any delta arrived, open tool calls) lives in
``TurnStream``. Field names follow ``tui_gateway/contracts/events.py`` and
``contracts/common.py`` at v0.23.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import NS

# Hermes tool names → ACP ToolKind. Anything else is "other".
TOOL_KINDS = {
    "read_file": "read",
    "search_files": "search",
    "write_file": "edit",
    "patch": "edit",
    "terminal": "execute",
    "execute_code": "execute",
    "process": "execute",
    "web_search": "search",
    "web_extract": "fetch",
    "web_url_read": "fetch",
    "browser_navigate": "fetch",
    "delegate_task": "think",
}


def tool_kind(name: str | None) -> str:
    return TOOL_KINDS.get(name or "", "other")


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _stack(**fields) -> dict:
    return {NS: {k: v for k, v in fields.items() if v is not None}}


@dataclass
class TurnStream:
    """One running turn's projection state."""

    saw_text: bool = False
    tools: dict[str, str] = field(default_factory=dict)  # tool_id -> name

    def project(self, etype: str, payload: dict) -> list[dict]:
        """ACP updates for one Hermes event; [] when it has no ACP form."""
        if etype == "message.delta":
            text = payload.get("text")
            if not text:
                return []
            self.saw_text = True
            return [{"sessionUpdate": "agent_message_chunk", "content": _text(text), **_mid(payload)}]
        if etype in ("reasoning.delta", "thinking.delta"):
            text = payload.get("text")
            if not text:
                return []
            return [{"sessionUpdate": "agent_thought_chunk", "content": _text(text), **_mid(payload)}]
        if etype == "tool.start":
            tid = str(payload.get("tool_id") or "")
            if not tid:
                return []
            name = payload.get("name") or "tool"
            self.tools[tid] = name
            update = {
                "sessionUpdate": "tool_call",
                "toolCallId": tid,
                "title": payload.get("context") or payload.get("preview") or name,
                "name": name,
                "kind": tool_kind(name),
                "status": "in_progress",
            }
            if payload.get("args") is not None:
                update["rawInput"] = payload["args"]
            return [update]
        if etype == "tool.complete":
            tid = str(payload.get("tool_id") or "")
            if not tid:
                return []
            self.tools.pop(tid, None)
            failed = bool(payload.get("error"))
            update = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "status": "failed" if failed else "completed",
            }
            output = {k: payload[k] for k in ("result_text", "summary", "result") if payload.get(k) is not None}
            if output:
                update["rawOutput"] = output
            if payload.get("result_text"):
                update["content"] = [{"type": "content", "content": _text(str(payload["result_text"]))}]
            meta = _stack(durationS=payload.get("duration_s"), summary=payload.get("summary"))
            if meta[NS]:
                update["_meta"] = meta
            return [update]
        if etype == "session.usage":
            return usage_updates(payload.get("usage"))
        return []

    def finish(self, payload: dict) -> list[dict]:
        """Updates for ``message.complete``: the final text when nothing streamed
        (a non-streaming provider), and the turn's usage."""
        out: list[dict] = []
        text = payload.get("text")
        if text and not self.saw_text and payload.get("status") == "complete":
            out.append({"sessionUpdate": "agent_message_chunk", "content": _text(text), **_mid(payload)})
        for tid in list(self.tools):
            out.append({"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": "failed"})
        self.tools.clear()
        out += usage_updates(payload.get("usage"))
        return out


def _mid(payload: dict) -> dict:
    turn, seg = payload.get("turn_id"), payload.get("segment")
    if turn is None:
        return {}
    return {"messageId": f"{turn}:{seg}" if seg is not None else str(turn)}


def usage_updates(usage: object) -> list[dict]:
    if not isinstance(usage, dict):
        return []
    used = usage.get("context_used") or usage.get("context_tokens")
    size = usage.get("context_max") or usage.get("context_window") or usage.get("context_length")
    if not isinstance(used, int) or not isinstance(size, int):
        return []
    meta = _stack(
        avgTps=usage.get("avg_tps"),
        inputTokens=usage.get("input") if usage.get("input") is not None else usage.get("prompt"),
        outputTokens=usage.get("output") if usage.get("output") is not None else usage.get("completion"),
    )
    update = {"sessionUpdate": "usage_update", "used": used, "size": size}
    if meta[NS]:
        update["_meta"] = meta
    return [update]


STOP_REASONS = {"complete": "end_turn", "interrupted": "cancelled"}


def history_updates(messages: list) -> list[dict]:
    """``session.resume``/``session.history`` rows → replay updates.

    User, assistant and reasoning text become chunks with the row id as
    ``messageId``; tool rows become a completed ``tool_call`` each. Hidden rows
    never arrive here (Hermes drops them)."""
    out: list[dict] = []
    for i, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        mid = {"messageId": f"row:{m['row_id']}"} if m.get("row_id") is not None else {}
        if role == "user" and m.get("text"):
            out.append({"sessionUpdate": "user_message_chunk", "content": _text(m["text"]), **mid})
        elif role == "assistant":
            reasoning = m.get("reasoning") or m.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                out.append({"sessionUpdate": "agent_thought_chunk", "content": _text(reasoning), **mid})
            if m.get("text"):
                out.append({"sessionUpdate": "agent_message_chunk", "content": _text(m["text"]), **mid})
        elif role == "tool":
            name = m.get("name") or "tool"
            update = {
                "sessionUpdate": "tool_call",
                "toolCallId": str(m.get("tool_call_id") or f"history-{i}"),
                "title": m.get("context") or name,
                "name": name,
                "kind": tool_kind(name),
                "status": "completed",
            }
            if m.get("args") is not None:
                update["rawInput"] = m["args"]
            out.append(update)
    return out


# -- server requests -------------------------------------------------------------

APPROVAL_OPTIONS = {
    "once": {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
    "session": {
        "optionId": "allow_session",
        "name": "Allow for this session",
        "kind": "allow_always",
        "_meta": {NS: {"scope": "session"}},
    },
    "always": {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
    "deny": {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
}
OPTION_TO_CHOICE = {"allow_once": "once", "allow_session": "session", "allow_always": "always", "deny": "deny"}
_APPROVAL_FIELDS = ("session_id", "request_id", "choices", "gateway_session_id")


def permission_params(session_id: str, params: dict) -> dict:
    """``approval`` → ``session/request_permission`` params. The command, tool and
    any MCP details go in the tool call's ``rawInput``."""
    choices = [c for c in params.get("choices") or ["once", "deny"] if c in APPROVAL_OPTIONS]
    if "deny" not in choices:
        choices.append("deny")
    raw = {k: v for k, v in params.items() if k not in _APPROVAL_FIELDS}
    title = params.get("description") or params.get("command") or "Approve this action?"
    return {
        "sessionId": session_id,
        "toolCall": {
            "toolCallId": f"approval:{params.get('request_id') or ''}",
            "title": str(title),
            "kind": "execute" if params.get("command") else "other",
            "status": "pending",
            "rawInput": raw,
        },
        "options": [APPROVAL_OPTIONS[c] for c in choices],
    }


def approval_answer(result: dict | None) -> dict:
    outcome = (result or {}).get("outcome") or {}
    if outcome.get("outcome") == "selected":
        return {"choice": OPTION_TO_CHOICE.get(outcome.get("optionId"), "deny")}
    return {"choice": "deny"}


def clarify_params(session_id: str, params: dict) -> dict:
    """``clarify`` → form-mode ``elicitation/create``: one property per question."""
    props: dict = {}
    questions = params.get("questions") or []
    for q in questions:
        qid = str(q.get("qid"))
        choices = q.get("choices")
        if choices and q.get("multi_select"):
            props[qid] = {"type": "array", "title": q.get("question", ""), "items": {"type": "string", "enum": list(choices)}}
        elif choices:
            props[qid] = {"type": "string", "title": q.get("question", ""), "enum": list(choices)}
        else:
            props[qid] = {"type": "string", "title": q.get("question", "")}
    message = questions[0].get("question", "") if len(questions) == 1 else "The assistant has questions."
    return {
        "sessionId": session_id,
        "mode": "form",
        "message": message,
        "requestedSchema": {"type": "object", "properties": props},
    }


def clarify_answer(result: dict | None) -> dict:
    """An accepted form answers every question (missing ones as skipped);
    decline or cancel sends no answers, which Hermes treats as cancel-all."""
    if not result or result.get("action") != "accept":
        return {"outcome": "cancelled"}
    content = result.get("content") or {}
    answers = {}
    for k, v in content.items():
        answers[k] = ", ".join(map(str, v)) if isinstance(v, list) else (None if v is None else str(v))
    return {"answers": answers, "outcome": "submitted"}
