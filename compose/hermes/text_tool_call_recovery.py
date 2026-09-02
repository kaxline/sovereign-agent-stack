"""Parse tool-call mimicry from assistant text into structured tool_calls.

Compatibility overlay for this stack — not destined for Hermes upstream.
Nous closed content-bound promotion as out of scope (#29115 / #35129);
weak OpenRouter models (e.g. Llama 3.3 70B) still emit Python- or XML-style
calls as plain text under large Hermes prompts. Recover the first valid
call so the normal tool-dispatch path can run.

Supported shapes (first match wins):
  search_files(target="files", pattern="*", path="/opt/data")
  <function/name="search_files" parameters="{...}"></function>
  <function name="search_files">{"pattern": "*"}</function>
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Python-style: name(k=v, k2="...")
_PYTHON_CALL_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z_][\w]*)\s*\((.*)\)\s*$",
    re.DOTALL,
)

# Boundary/OpenRouter Llama XML mimic: <function/name="..." parameters="{...}">
_XML_SLASH_NAME_RE = re.compile(
    r'<function\s*/\s*name\s*=\s*["\']([^"\']+)["\']\s+'
    r'parameters\s*=\s*["\'](\{.*?\})["\']\s*>\s*</function>',
    re.DOTALL | re.IGNORECASE,
)

# Gemma-ish: <function name="...">JSON</function>
_XML_FUNCTION_NAME_RE = re.compile(
    r'<function\s+name\s*=\s*["\']([^"\']+)["\']\s*>\s*(\{.*?\})\s*</function>',
    re.DOTALL | re.IGNORECASE,
)


def _normalize_valid_names(valid_tool_names: Any) -> set[str]:
    if not valid_tool_names:
        return set()
    if isinstance(valid_tool_names, dict):
        return {str(k) for k in valid_tool_names.keys()}
    if isinstance(valid_tool_names, (set, frozenset, list, tuple)):
        return {str(n) for n in valid_tool_names}
    return set()


def _parse_python_kwargs(arg_src: str) -> Optional[dict]:
    src = (arg_src or "").strip()
    if not src:
        return {}
    try:
        tree = ast.parse(f"f({src})", mode="eval")
    except SyntaxError:
        return None
    if not isinstance(tree, ast.Expression) or not isinstance(tree.body, ast.Call):
        return None
    call = tree.body
    if call.args:
        # Positional args are too ambiguous for recovery.
        return None
    out: dict[str, Any] = {}
    for kw in call.keywords:
        if kw.arg is None:
            return None
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except Exception:
            return None
    return out


def _parse_json_object(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if not text:
        return {}
    # Models sometimes escape quotes inside the attribute value.
    candidates = [text, text.replace('\\"', '"').replace("\\'", "'")]
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except Exception:
            try:
                obj = ast.literal_eval(candidate)
            except Exception:
                continue
        if isinstance(obj, dict):
            return obj
    return None


def iter_mimicked_calls(text: str) -> Iterable[tuple[str, dict]]:
    """Yield (name, arguments_dict) for recognized mimic patterns in text."""
    if not isinstance(text, str) or not text.strip():
        return

    for match in _XML_SLASH_NAME_RE.finditer(text):
        name = match.group(1).strip()
        args = _parse_json_object(match.group(2))
        if name and args is not None:
            yield name, args

    for match in _XML_FUNCTION_NAME_RE.finditer(text):
        name = match.group(1).strip()
        args = _parse_json_object(match.group(2))
        if name and args is not None:
            yield name, args

    stripped = text.strip()
    # Prefer a whole-message Python call, then a line-local one.
    for candidate in (stripped, *(ln.strip() for ln in stripped.splitlines() if ln.strip())):
        match = _PYTHON_CALL_RE.match(candidate)
        if not match:
            continue
        name = match.group(1)
        # Avoid treating prose like print(...) as tools; name gate happens later.
        args = _parse_python_kwargs(match.group(2))
        if args is not None:
            yield name, args


def collect_candidate_text(assistant_message: Any, streamed_text: str = "") -> str:
    """Merge content / reasoning / streamed buffers for mimic detection."""
    parts: list[str] = []
    for attr in ("content", "reasoning", "reasoning_content"):
        value = getattr(assistant_message, attr, None)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if isinstance(streamed_text, str) and streamed_text.strip():
        parts.append(streamed_text)
    # Preserve order but drop exact duplicates.
    seen: set[str] = set()
    uniq: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            uniq.append(part)
    return "\n".join(uniq)


def recover_tool_calls(
    text: str,
    valid_tool_names: Any,
    *,
    max_calls: int = 1,
) -> List[Any]:
    """Return structured ToolCall objects for the first valid mimic call(s)."""
    names = _normalize_valid_names(valid_tool_names)
    if not names or not text:
        return []

    try:
        from agent.transports.types import ToolCall
    except Exception:
        logger.debug("text-tool-call recovery: ToolCall type unavailable", exc_info=True)
        return []

    recovered: list[Any] = []
    for name, args in iter_mimicked_calls(text):
        if name not in names:
            continue
        try:
            arguments = json.dumps(args, ensure_ascii=False)
        except Exception:
            continue
        call_id = f"text_recover_{len(recovered) + 1}"
        recovered.append(ToolCall(id=call_id, name=name, arguments=arguments))
        if len(recovered) >= max_calls:
            break
    return recovered


def try_recover_assistant_tool_calls(agent: Any, assistant_message: Any) -> Sequence[Any]:
    """If tool_calls is empty, try promoting text-mimicked calls onto the message.

    Returns the recovered list (also assigned onto assistant_message) or ().
    """
    if assistant_message is None:
        return ()
    existing = getattr(assistant_message, "tool_calls", None)
    if existing:
        return ()

    streamed = getattr(agent, "_current_streamed_assistant_text", "") or ""
    text = collect_candidate_text(assistant_message, streamed)
    if not text.strip():
        return ()

    recovered = recover_tool_calls(
        text,
        getattr(agent, "valid_tool_names", None),
        max_calls=1,
    )
    if not recovered:
        return ()

    assistant_message.tool_calls = list(recovered)
    # Clear mimic text so it is not also treated as the final answer if
    # dispatch somehow falls through.
    for attr in ("content",):
        value = getattr(assistant_message, attr, None)
        if isinstance(value, str) and value.strip():
            for name, _ in iter_mimicked_calls(value):
                if name in _normalize_valid_names(getattr(agent, "valid_tool_names", None)):
                    try:
                        assistant_message.content = ""
                    except Exception:
                        pass
                    break

    names = [getattr(tc, "name", None) or tc.function.name for tc in recovered]
    logger.info(
        "Recovered %d text-mimicked tool call(s) from assistant text: %s",
        len(recovered),
        ", ".join(names),
    )
    try:
        agent._emit_status(
            f"↻ Recovered text-mimicked tool call — executing {names[0]}"
        )
    except Exception:
        pass
    return recovered
