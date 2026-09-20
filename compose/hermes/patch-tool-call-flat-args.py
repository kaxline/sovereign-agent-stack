#!/usr/bin/env python3
"""Normalize weak-model tool_call argument shapes for deferred MCP tools.

Fixes observed in WebUI sessions:

1. Flat siblings — tool_call({name, query}) instead of arguments={query}
2. Nested name — tool_call({arguments: {name, url}}) with no top-level name
3. Short MCP aliases — tool_call({name: "web_url_read", ...}) instead of
   mcp__searxng__web_url_read (not deferrable under the short name)

These are stack-wide compatibility shims: correct calls are unchanged.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/tool_search.py")
MARKER = "# assistant-stack: tool_call arg shape fixes"
OLD_MARKERS = (
    MARKER,
    "# assistant-stack: promote flat tool_call sibling args",
    "# assistant-stack: debug-53781c tool_call shape",
    "# assistant-stack: debug-53781c resolve return",
)

# Match the stock resolve_underlying_call body (with or without prior patches
# already stripped). We replace from name= through the final return.
ANCHOR_START = "    name = str(args.get(\"name\") or \"\").strip()\n"
ANCHOR_END = "    return name, raw_args, None\n"

REPLACEMENT = '''    name = str(args.get("name") or "").strip()
    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return None, {}, f"tool_call 'arguments' is not valid JSON: {e}"
    if not isinstance(raw_args, dict):
        return None, {}, "tool_call 'arguments' must be an object"
    # assistant-stack: tool_call arg shape fixes
    # Lift name nested inside arguments when the top-level name is missing.
    if not name and isinstance(raw_args, dict) and raw_args.get("name"):
        name = str(raw_args.get("name") or "").strip()
        raw_args = {k: v for k, v in raw_args.items() if k != "name"}
    # Models often put tool params next to name instead of under arguments.
    if not raw_args:
        _flat = {
            k: v for k, v in (args or {}).items()
            if k not in ("name", "arguments")
        }
        if _flat:
            raw_args = _flat
    # Short names from prompts / training priors → real MCP tool ids.
    _STACK_TOOL_ALIASES = {
        "web_search": "mcp__searxng__searxng_web_search",
        "web_extract": "mcp__searxng__web_url_read",
        "searxng_web_search": "mcp__searxng__searxng_web_search",
        "web_url_read": "mcp__searxng__web_url_read",
    }
    if name in _STACK_TOOL_ALIASES:
        name = _STACK_TOOL_ALIASES[name]
    if not name:
        return None, {}, "tool_call requires a 'name' argument"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, f"tool_call cannot invoke '{name}' (it is itself a bridge tool)"
    if not is_deferrable_tool_name(name):
        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call."
        )
    return name, raw_args, None
'''


def _strip_debug_regions(text: str) -> str:
    """Drop folded debug regions and old assistant-stack markers in resolve."""
    # Remove #region agent log … #endregion blocks that mention debug-53781c
    needle = "# assistant-stack: debug-53781c"
    while needle in text:
        start = text.find(needle)
        region_open = text.rfind("# #region", 0, start)
        end = text.find("# #endregion", start)
        if region_open < 0 or end < 0:
            # Fall through: delete the marker line only
            line_start = text.rfind("\n", 0, start)
            line_end = text.find("\n", start)
            if line_end < 0:
                break
            text = text[: line_start + 1] + text[line_end + 1 :]
            continue
        end = text.find("\n", end)
        if end < 0:
            break
        text = text[:region_open] + text[end + 1 :]
    # Orphaned debug helper left ahead of resolve_underlying_call
    anchor = "\ndef _agent_debug_53781c_ts("
    while anchor in text:
        start = text.find(anchor)
        nxt = text.find("\ndef ", start + 1)
        if nxt < 0:
            break
        text = text[:start] + text[nxt:]
    return text


def _strip_old_flat_promote(text: str) -> str:
    old = '''    # assistant-stack: promote flat tool_call sibling args
    # Models often put tool params next to name instead of under arguments.
    if not raw_args:
        _flat = {
            k: v for k, v in (args or {}).items()
            if k not in ("name", "arguments")
        }
        if _flat:
            raw_args = _flat
'''
    if old in text:
        text = text.replace(old, "", 1)
    return text


def main() -> int:
    if not TARGET.is_file():
        print(f"[patch-tool-call-flat-args] skip missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if (
        MARKER in text
        and "_STACK_TOOL_ALIASES" in text
        and "_agent_debug_53781c_ts(" not in text
    ):
        print(f"[patch-tool-call-flat-args] already applied ({TARGET})")
        return 0

    text = _strip_debug_regions(text)
    text = _strip_old_flat_promote(text)

    # Replace the body of resolve_underlying_call from name= to return.
    fn = text.find("def resolve_underlying_call(")
    if fn < 0:
        print("[patch-tool-call-flat-args] resolve_underlying_call missing")
        return 1
    start = text.find(ANCHOR_START, fn)
    if start < 0:
        print("[patch-tool-call-flat-args] name-assign needle missing")
        return 1
    end = text.find(ANCHOR_END, start)
    if end < 0:
        print("[patch-tool-call-flat-args] return needle missing")
        return 1
    end = end + len(ANCHOR_END)
    text = text[:start] + REPLACEMENT + text[end:]
    TARGET.write_text(text, encoding="utf-8")
    print(f"[patch-tool-call-flat-args] patched {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
