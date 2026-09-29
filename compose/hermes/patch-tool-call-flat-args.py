#!/usr/bin/env python3
"""Normalize weak-model tool_call argument shapes for deferred MCP tools.

Fixes observed in WebUI sessions:

1. Flat siblings — tool_call({name, query}) instead of arguments={query}
2. Nested name — tool_call({arguments: {name, url}}) with no top-level name
3. Short MCP aliases — tool_call({name: "web_url_read", ...}) instead of
   mcp__searxng__web_url_read (not deferrable under the short name)
4. Stringified calls — tool_call({calls: "[{...}]"}) instead of a real array
   (local models often JSON-encode the array as a string; upstream only
   json.loads nested ``arguments``, so validation fails with
   "calls must be a non-empty array")

These are stack-wide compatibility shims: correct calls are unchanged.

Hermes v2026.9+ routes parsing through ``normalize_tool_call_entries``.
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path("/opt/hermes/tools/tool_search_validation.py")
TARGET_LEGACY = Path("/opt/hermes/tools/tool_search.py")
MARKER = "# assistant-stack: tool_call arg shape fixes"
MARKER_STRING_CALLS = "# assistant-stack: tool_call stringified calls"

STACK_ALIASES = '''
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
'''

# Insert just before entries.append in normalize_tool_call_entries.
OLD_APPEND = '''        if not isinstance(raw_args, dict):
            return [], f"tool_call calls[{position}].arguments must be an object"
        entries.append({"name": name, "arguments": raw_args})
'''

NEW_APPEND = '''        if not isinstance(raw_args, dict):
            return [], f"tool_call calls[{position}].arguments must be an object"
        # assistant-stack: tool_call arg shape fixes
        if not name and isinstance(raw_args, dict) and raw_args.get("name"):
            name = str(raw_args.get("name") or "").strip()
            raw_args = {k: v for k, v in raw_args.items() if k != "name"}
        if not raw_args and isinstance(raw, dict):
            _flat = {
                k: v for k, v in raw.items()
                if k not in ("name", "arguments")
            }
            if _flat:
                raw_args = _flat
        if not raw_args and isinstance(args, dict) and position == 0:
            _flat = {
                k: v for k, v in args.items()
                if k not in ("name", "arguments", "calls")
            }
            if _flat:
                raw_args = _flat
        name = _STACK_TOOL_ALIASES.get(name, name)
        entries.append({"name": name, "arguments": raw_args})
'''

# Also tolerate legacy single-shape with flat siblings when name is present
# but arguments missing — upstream already does args.get("arguments").
OLD_LEGACY_SINGLE = '''        if not str(args.get("name") or "").strip():
            return [], "tool_call requires 'calls' (an array of {name, arguments})"
        raw_calls = [{"name": args.get("name"), "arguments": args.get("arguments")}]
'''

NEW_LEGACY_SINGLE = '''        if not str(args.get("name") or "").strip():
            return [], "tool_call requires 'calls' (an array of {name, arguments})"
        # assistant-stack: tool_call arg shape fixes (legacy single)
        _legacy_args = args.get("arguments")
        if _legacy_args is None:
            _legacy_args = {
                k: v for k, v in args.items()
                if k not in ("name", "arguments", "calls")
            } or {}
        raw_calls = [{"name": args.get("name"), "arguments": _legacy_args}]
'''

OLD_CALLS_LIST_CHECK = '''    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list) or not raw_calls:
        return [], "tool_call 'calls' must be a non-empty array of {name, arguments}"
'''

NEW_CALLS_LIST_CHECK = '''    # assistant-stack: tool_call stringified calls
    if isinstance(raw_calls, str):
        try:
            raw_calls = json.loads(raw_calls)
        except json.JSONDecodeError as e:
            return [], f"tool_call 'calls' is not valid JSON: {e}"
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list) or not raw_calls:
        return [], "tool_call 'calls' must be a non-empty array of {name, arguments}"
'''

_AGENT_LOG_RE = re.compile(
    r"[ \t]*# #region agent log\n.*?[ \t]*# #endregion\n",
    re.S,
)


def _ensure_coding_aliases(text: str) -> str:
    if "coding_start_task" in text and "_STACK_TOOL_ALIASES" in text:
        # Expand an older alias map that lacks coding_* entries.
        old = '''_STACK_TOOL_ALIASES = {
    "web_search": "mcp__searxng__searxng_web_search",
    "web_extract": "mcp__searxng__web_url_read",
    "searxng_web_search": "mcp__searxng__searxng_web_search",
    "web_url_read": "mcp__searxng__web_url_read",
}
'''
        if old in text:
            return text.replace(old, STACK_ALIASES.lstrip("\n"), 1)
        return text
    return text


def _strip_debug_ingest(text: str) -> str:
    """Remove temporary debug-session POSTs left by earlier patch builds."""
    return _AGENT_LOG_RE.sub("", text)


def main() -> int:
    if not TARGET.is_file():
        if TARGET_LEGACY.is_file():
            print(f"[patch-tool-call-flat-args] skip: {TARGET} missing; legacy path not auto-applied")
            return 0
        print(f"[patch-tool-call-flat-args] skip missing {TARGET}")
        return 0

    text = TARGET.read_text(encoding="utf-8")
    changed = False

    cleaned = _strip_debug_ingest(text)
    if cleaned != text:
        text = cleaned
        changed = True

    if "_STACK_TOOL_ALIASES" not in text:
        anchor = "def normalize_tool_call_entries("
        if anchor not in text:
            print("[patch-tool-call-flat-args] normalize_tool_call_entries missing")
            return 0
        text = text.replace(anchor, STACK_ALIASES + "\n" + anchor, 1)
        changed = True
    else:
        new_text = _ensure_coding_aliases(text)
        if new_text != text:
            text = new_text
            changed = True

    if MARKER not in text and OLD_APPEND in text:
        text = text.replace(OLD_APPEND, NEW_APPEND, 1)
        changed = True
    if OLD_LEGACY_SINGLE in text:
        text = text.replace(OLD_LEGACY_SINGLE, NEW_LEGACY_SINGLE, 1)
        changed = True

    if MARKER_STRING_CALLS not in text:
        if OLD_CALLS_LIST_CHECK not in text:
            print("[patch-tool-call-flat-args] calls-list needle missing")
        else:
            text = text.replace(OLD_CALLS_LIST_CHECK, NEW_CALLS_LIST_CHECK, 1)
            changed = True

    if changed:
        TARGET.write_text(text, encoding="utf-8")
        print(f"[patch-tool-call-flat-args] patched {TARGET}")
    else:
        print(f"[patch-tool-call-flat-args] already applied ({TARGET})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
