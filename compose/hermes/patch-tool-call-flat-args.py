#!/usr/bin/env python3
"""Normalize weak-model tool_call argument shapes for deferred MCP tools.

Fixes observed in WebUI sessions:

1. Flat siblings — tool_call({name, query}) instead of arguments={query}
2. Nested name — tool_call({arguments: {name, url}}) with no top-level name
3. Short MCP aliases — tool_call({name: "web_url_read", ...}) instead of
   mcp__searxng__web_url_read (not deferrable under the short name)

These are stack-wide compatibility shims: correct calls are unchanged.

Hermes v2026.9+ routes parsing through ``normalize_tool_call_entries``.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/tool_search_validation.py")
TARGET_LEGACY = Path("/opt/hermes/tools/tool_search.py")
MARKER = "# assistant-stack: tool_call arg shape fixes"

STACK_ALIASES = '''
_STACK_TOOL_ALIASES = {
    "web_search": "mcp__searxng__searxng_web_search",
    "web_extract": "mcp__searxng__web_url_read",
    "searxng_web_search": "mcp__searxng__searxng_web_search",
    "web_url_read": "mcp__searxng__web_url_read",
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


def main() -> int:
    if TARGET.is_file():
        text = TARGET.read_text(encoding="utf-8")
        if MARKER in text and "_STACK_TOOL_ALIASES" in text:
            print(f"[patch-tool-call-flat-args] already applied ({TARGET})")
            return 0
        if OLD_APPEND not in text:
            print(f"[patch-tool-call-flat-args] append needle missing in {TARGET}")
            return 0
        if "_STACK_TOOL_ALIASES" not in text:
            # Insert aliases before the function.
            anchor = "def normalize_tool_call_entries("
            if anchor not in text:
                print("[patch-tool-call-flat-args] normalize_tool_call_entries missing")
                return 0
            text = text.replace(anchor, STACK_ALIASES + "\n" + anchor, 1)
        text = text.replace(OLD_APPEND, NEW_APPEND, 1)
        if OLD_LEGACY_SINGLE in text:
            text = text.replace(OLD_LEGACY_SINGLE, NEW_LEGACY_SINGLE, 1)
        TARGET.write_text(text, encoding="utf-8")
        print(f"[patch-tool-call-flat-args] patched {TARGET}")
        return 0

    # Pre-v2026.9: patch resolve_underlying_call in tool_search.py (best-effort).
    if TARGET_LEGACY.is_file():
        print(f"[patch-tool-call-flat-args] skip: {TARGET} missing; legacy path not auto-applied")
        return 0
    print(f"[patch-tool-call-flat-args] skip missing {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
