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

from pathlib import Path

from overlay_patch import ROOT, edit, install

TARGET = ROOT / "tools" / "tool_search_validation.py"
MARKER = "# assistant-stack: tool_call arg shape fixes"
TAG = "[patch-tool-call-flat-args]"

ALIASES_SRC = Path("/bootstrap/stack_tool_aliases.py")
ALIASES_DST = ROOT / "tools" / "stack_tool_aliases.py"

STACK_ALIASES = '''
from tools.stack_tool_aliases import STACK_TOOL_ALIASES as _STACK_TOOL_ALIASES
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

def main() -> None:
    install(ALIASES_SRC, ALIASES_DST, TAG)
    anchor = "def normalize_tool_call_entries("
    edit(TARGET, [(anchor, STACK_ALIASES + "\n" + anchor), (OLD_APPEND, NEW_APPEND),
                  (OLD_LEGACY_SINGLE, NEW_LEGACY_SINGLE), (OLD_CALLS_LIST_CHECK, NEW_CALLS_LIST_CHECK)],
         MARKER, TAG)


if __name__ == "__main__":
    main()
