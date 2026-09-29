#!/usr/bin/env python3
"""Honor tools.tool_search.always_include when classifying deferrable tools.

Hermes v2026.9+ ignores that list. MCP tools (toolset mcp-*) always defer, so
the model must tool_search → tool_describe → tool_call even for stack pins
(SearXNG, LightRAG, OpenCode). This overlay keeps always_include names in the
model-visible schema.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/tool_search.py")
MARKER = "# assistant-stack: honor always_include"

OLD_FIELD = '''    # None = curated default; an explicit list replaces it wholesale ([] = defer no core tools).
    defer_tools: Optional[frozenset] = None
'''

NEW_FIELD = '''    # None = curated default; an explicit list replaces it wholesale ([] = defer no core tools).
    defer_tools: Optional[frozenset] = None
    # assistant-stack: honor always_include
    always_include: frozenset = frozenset()
'''

OLD_FROM_RAW = '''        defer_raw = raw.get("defer")
        return cls(
            enabled=_tri_state(raw.get("enabled", "auto")),
            threshold_pct=max(0.0, min(100.0, _safe_float(raw.get("threshold_pct"), 5.0))),
            search_default_limit=_clamped_int(
                raw.get("search_default_limit"), 5, 1, max_search_limit),
            max_search_limit=max_search_limit,
            listing=_tri_state(raw.get("listing", "auto")),
            listing_max_tokens=_clamped_int(raw.get("listing_max_tokens"), 4000, 200, 60000),
            defer_tools=(frozenset(str(n).strip() for n in defer_raw if str(n).strip())
                         if isinstance(defer_raw, (list, tuple, set)) else None))
'''

NEW_FROM_RAW = '''        defer_raw = raw.get("defer")
        include_raw = raw.get("always_include")
        always_include = (
            frozenset(str(n).strip() for n in include_raw if str(n).strip())
            if isinstance(include_raw, (list, tuple, set)) else frozenset()
        )
        return cls(
            enabled=_tri_state(raw.get("enabled", "auto")),
            threshold_pct=max(0.0, min(100.0, _safe_float(raw.get("threshold_pct"), 5.0))),
            search_default_limit=_clamped_int(
                raw.get("search_default_limit"), 5, 1, max_search_limit),
            max_search_limit=max_search_limit,
            listing=_tri_state(raw.get("listing", "auto")),
            listing_max_tokens=_clamped_int(raw.get("listing_max_tokens"), 4000, 200, 60000),
            defer_tools=(frozenset(str(n).strip() for n in defer_raw if str(n).strip())
                         if isinstance(defer_raw, (list, tuple, set)) else None),
            always_include=always_include)
'''

OLD_IS_DEFERRABLE = '''    if name in BRIDGE_TOOL_NAMES:
        return False
    if defer_tools is not None and name in defer_tools:
        return True
'''

NEW_IS_DEFERRABLE = '''    if name in BRIDGE_TOOL_NAMES:
        return False
    # assistant-stack: honor always_include
    if name in _always_include_names():
        return False
    if defer_tools is not None and name in defer_tools:
        return True
'''

HELPER = '''
def _always_include_names() -> frozenset:
    """assistant-stack: tools.tool_search.always_include (MCP pins stay eager)."""
    try:
        return load_config_readonly().always_include
    except Exception:
        return frozenset()

'''


def main() -> int:
    if not TARGET.is_file():
        print(f"[patch-tool-search-always-include] skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[patch-tool-search-always-include] already applied ({TARGET})")
        return 0
    if OLD_FIELD not in text or OLD_FROM_RAW not in text or OLD_IS_DEFERRABLE not in text:
        print(f"[patch-tool-search-always-include] warn: expected needles missing in {TARGET}")
        return 0
    text = text.replace(OLD_FIELD, NEW_FIELD, 1)
    text = text.replace(OLD_FROM_RAW, NEW_FROM_RAW, 1)
    # Insert helper just before is_deferrable_tool_name.
    needle = "def is_deferrable_tool_name("
    if needle not in text:
        print(f"[patch-tool-search-always-include] warn: is_deferrable_tool_name missing")
        return 0
    text = text.replace(needle, HELPER + needle, 1)
    text = text.replace(OLD_IS_DEFERRABLE, NEW_IS_DEFERRABLE, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"[patch-tool-search-always-include] patched {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
