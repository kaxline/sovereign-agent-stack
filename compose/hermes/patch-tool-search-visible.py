#!/usr/bin/env python3
"""Make tool_search also surface already-visible core tools.

Local models (e.g. Qwen via LM Studio) often call tool_search for capabilities
that are already in the primary tool list. Upstream tool_search only scans the
deferred MCP/plugin catalog, so queries like "send_message" / "Signal" return
empty and the model wrongly concludes the tool does not exist.

When a query matches a visible/core tool, return those hits with an explicit
call-directly hint.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/tool_search.py")
MARKER = "# assistant-stack: also search visible/core tools"

OLD = '''    _, deferrable = classify_tools(current_tool_defs)
    catalog = build_catalog(deferrable)
    hits = search_catalog(catalog, query, limit=limit)
    result: Dict[str, Any] = {
        "query": query,
        "total_available": len(catalog),
        "matches": [_format_search_hit(h) for h in hits],
    }
    if not hits and catalog:
        result["available_sources"] = _available_source_summary(catalog)
        result["hint"] = (
            "No lexical match was found, but the sources above are connected "
            "and their tools remain available. Retry tool_search with the "
            "service name plus a concrete action or object before concluding "
            "the capability is unavailable."
        )
    return json.dumps(result, ensure_ascii=False)
'''

NEW = '''    visible, deferrable = classify_tools(current_tool_defs)
    catalog = build_catalog(deferrable)
    hits = search_catalog(catalog, query, limit=limit)
    result: Dict[str, Any] = {
        "query": query,
        "total_available": len(catalog),
        "matches": [_format_search_hit(h) for h in hits],
    }
    # assistant-stack: also search visible/core tools
    # Deferred-only search makes models miss tools already in the schema
    # (e.g. send_message for Signal) and claim they are unavailable.
    visible_catalog = build_catalog(visible)
    visible_hits = search_catalog(visible_catalog, query, limit=limit)
    # Prefer exact/core messaging tool when the user clearly asked for it.
    q_lower = query.lower()
    if any(k in q_lower for k in ("send_message", "signal", "telegram", "discord", "slack")):
        for td in visible:
            name = ((td.get("function") or {}).get("name") or "")
            if name == "send_message":
                sm_hits = [h for h in build_catalog([td]) if h.name == "send_message"]
                if sm_hits:
                    visible_hits = sm_hits + [h for h in visible_hits if h.name != "send_message"]
                break
    if visible_hits:
        result["already_available"] = [
            {
                **_format_search_hit(h),
                "hint": "Already in your active tool list — call it directly (do not use tool_call).",
            }
            for h in visible_hits
        ]
        if not hits:
            result["hint"] = (
                "Matched tools that are already visible in this session. "
                "Call them directly by name; tool_search only discovers deferred MCP/plugin tools."
            )
    elif not hits and catalog:
        result["available_sources"] = _available_source_summary(catalog)
        result["hint"] = (
            "No lexical match was found, but the sources above are connected "
            "and their tools remain available. Retry tool_search with the "
            "service name plus a concrete action or object before concluding "
            "the capability is unavailable. Also check tools already in your "
            "active list (for example send_message) and call those directly."
        )
    return json.dumps(result, ensure_ascii=False)
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if OLD not in text:
        print(f"skip: expected snippet not found in {TARGET}")
        return 1
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
