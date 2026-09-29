"""JSONL turn tracer for tool-calling evals.

Writes one line per assistant turn to /opt/data/eval/traces.jsonl (host:
data/hermes/eval/). Eval cases embed [eval:<id>] in the user prompt so WebUI
and the scripted harness share the same id.

Installed into /opt/hermes/agent/ by patch-tool-eval-trace.py at cont-init.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

TRACE_PATH = Path(os.environ.get("HERMES_EVAL_TRACE_PATH", "/opt/data/eval/traces.jsonl"))
EVAL_ID_RE = re.compile(r"\[eval:([a-zA-Z0-9_-]+)\]")
BRIDGE_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})

# Same short-name map as patch-tool-call-flat-args.py so score sees MCP names.
_STACK_TOOL_ALIASES = {
    "web_search": "mcp__searxng__searxng_web_search",
    "web_extract": "mcp__searxng__web_url_read",
    "searxng_web_search": "mcp__searxng__searxng_web_search",
    "web_url_read": "mcp__searxng__web_url_read",
    "coding_list_roots": "mcp__opencode__coding_list_roots",
    "coding_start_task": "mcp__opencode__coding_start_task",
    "coding_get_task_status": "mcp__opencode__coding_get_task_status",
    "coding_wait_for_task": "mcp__opencode__coding_wait_for_task",
    "coding_get_task_result": "mcp__opencode__coding_get_task_result",
    "coding_continue_task": "mcp__opencode__coding_continue_task",
}


def extract_eval_id(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    match = EVAL_ID_RE.search(text)
    return match.group(1) if match else None


def _message_text(msg: Any) -> str:
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return ""
    content = getattr(msg, "content", None)
    return content if isinstance(content, str) else ""


def latest_user_eval_id(messages: Optional[Sequence[Any]]) -> Optional[str]:
    if not messages:
        return None
    for msg in reversed(list(messages)):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue
        eid = extract_eval_id(_message_text(msg))
        if eid:
            return eid
    return None


def _parse_jsonish(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw
    return raw


def _tc_name_and_args(tc: Any) -> tuple[Optional[str], Any]:
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name")
        args = tc.get("arguments") if "arguments" in tc else fn.get("arguments")
        return (str(name) if name else None), args
    name = getattr(tc, "name", None)
    args = getattr(tc, "arguments", None)
    fn = getattr(tc, "function", None)
    if not name and fn is not None:
        name = getattr(fn, "name", None)
        if args is None:
            args = getattr(fn, "arguments", None)
    return (str(name) if name else None), args


def _inner_tool_call_names(name: str, args: Any) -> list[str]:
    """Unwrap tool_call({calls: [{name, arguments}]}) to the underlying tool.

    Only the ``calls`` array is the dispatch shape. A ``names`` typo is a
    failed invocation and must not count as a hit.
    """
    if name != "tool_call":
        return []
    parsed = _parse_jsonish(args)
    if not isinstance(parsed, dict):
        return []
    calls = _parse_jsonish(parsed.get("calls"))
    if not isinstance(calls, list):
        return []
    inner: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        raw = call.get("name")
        if not raw:
            continue
        key = str(raw).strip()
        inner.append(_STACK_TOOL_ALIASES.get(key, key))
    return inner


def tool_call_names(tool_calls: Any) -> list[str]:
    names: list[str] = []
    if not tool_calls:
        return names
    for tc in tool_calls:
        name, args = _tc_name_and_args(tc)
        if not name:
            continue
        names.append(name)
        names.extend(_inner_tool_call_names(name, args))
    return names


def _was_recovered(tool_calls: Any) -> bool:
    if not tool_calls:
        return False
    for tc in tool_calls:
        call_id = getattr(tc, "id", None)
        if isinstance(tc, dict):
            call_id = tc.get("id")
        if str(call_id or "").startswith("text_recover_"):
            return True
    return False


def _profile_hint(agent: Any) -> str:
    for attr in ("profile", "profile_name", "_profile_name"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    home = os.environ.get("HERMES_HOME", "")
    if "/profiles/" in home:
        return home.rstrip("/").rsplit("/", 1)[-1]
    return "default"


def append_trace(event: dict) -> None:
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("tool-eval trace write failed", exc_info=True)


def record_web_rewrite(*, from_name: str, to_name: str, eval_id: Optional[str] = None) -> None:
    append_trace(
        {
            "ts": int(time.time() * 1000),
            "kind": "web_rewrite",
            "eval_id": eval_id,
            "from": from_name,
            "to": to_name,
            "rewritten": True,
        }
    )


def record_assistant_turn(
    agent: Any,
    assistant_message: Any,
    *,
    messages: Optional[Sequence[Any]] = None,
    finish_reason: Optional[str] = None,
) -> None:
    if assistant_message is None:
        return

    msgs = messages
    if msgs is None:
        msgs = getattr(agent, "messages", None) or getattr(agent, "_messages", None)

    tool_calls = getattr(assistant_message, "tool_calls", None)
    names = tool_call_names(tool_calls)
    recovered = _was_recovered(tool_calls)
    content = getattr(assistant_message, "content", None)
    content_text = ""
    if isinstance(content, str) and content:
        content_text = content
    elif isinstance(getattr(agent, "_current_streamed_assistant_text", None), str):
        content_text = agent._current_streamed_assistant_text
    content_prefix = content_text.strip()[:200]
    content_len = len(content_text)

    bridge = [n for n in names if n in BRIDGE_TOOLS or n.startswith("tool_")]
    # Narrow to known bridge names only.
    bridge = [n for n in names if n in BRIDGE_TOOLS]

    if names and recovered:
        outcome = "recovered_then_dispatched"
    elif names:
        outcome = "tools_dispatched"
    else:
        outcome = "text_only"

    intent_ack = bool(getattr(agent, "_assistant_stack_intent_ack", False))
    try:
        agent._assistant_stack_intent_ack = False
    except Exception:
        pass

    session_id = (
        getattr(agent, "session_id", None)
        or getattr(agent, "_session_id", None)
        or os.environ.get("HERMES_SESSION_ID")
    )

    event = {
        "ts": int(time.time() * 1000),
        "kind": "assistant_turn",
        "profile": _profile_hint(agent),
        "session_id": session_id,
        "eval_id": latest_user_eval_id(msgs),
        "finish_reason": finish_reason
        or getattr(assistant_message, "finish_reason", None),
        "content_prefix": content_prefix,
        "content_len": content_len,
        "tool_calls": names,
        "recovered": recovered,
        "recovered_names": [n for n in names] if recovered else [],
        "rewritten": False,
        "intent_ack": intent_ack,
        "bridge": bridge,
        "outcome": outcome,
    }
    append_trace(event)
