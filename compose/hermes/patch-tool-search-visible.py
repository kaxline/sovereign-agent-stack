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

# v0.22 multi-query dispatch.
OLD = '''    catalog = build_catalog(_deferrable_in(current_tool_defs))
    remote_entries: list[list[CatalogEntry]] = [[] for _ in queries]
    hosted_failure: Optional[str] = None
    if connections_in_scope(current_tool_defs):
        remote_entries, hosted_failure = connector_entries_by_group(
            queries, connector_search=connector_search, call=call)
    results: list[dict[str, Any]] = []
    tools_map: dict[str, dict[str, Any]] = {}
    available_sources = _available_source_summary(catalog)
    for position, query in enumerate(queries):
        corpus = catalog + remote_entries[position]
        hits = search_catalog(corpus, query, limit=limit)
        for h in hits:
            tools_map.setdefault(h.name, _shared_tool_record(h))
        matches = [h.name for h in hits]
        group: dict[str, Any] = {"query": query, "matches": matches}
        if not matches and available_sources:
            group["available_sources"] = available_sources
            group["hint"] = (
                "This query returned no lexical matches, but the sources above "
                "are connected and their tools remain available. Retry "
                "tool_search with the service name plus a concrete action or "
                "object before concluding the capability is unavailable.")
        results.append(group)
'''

NEW = '''    catalog = build_catalog(_deferrable_in(current_tool_defs))
    visible_catalog = build_catalog(_visible_in(current_tool_defs))
    remote_entries: list[list[CatalogEntry]] = [[] for _ in queries]
    hosted_failure: Optional[str] = None
    if connections_in_scope(current_tool_defs):
        remote_entries, hosted_failure = connector_entries_by_group(
            queries, connector_search=connector_search, call=call)
    results: list[dict[str, Any]] = []
    tools_map: dict[str, dict[str, Any]] = {}
    available_sources = _available_source_summary(catalog)
    for position, query in enumerate(queries):
        corpus = catalog + remote_entries[position]
        hits = search_catalog(corpus, query, limit=limit)
        for h in hits:
            tools_map.setdefault(h.name, _shared_tool_record(h))
        matches = [h.name for h in hits]
        group: dict[str, Any] = {"query": query, "matches": matches}
        # assistant-stack: also search visible/core tools
        visible_hits = search_catalog(visible_catalog, query, limit=limit)
        q_lower = query.lower()
        if any(k in q_lower for k in ("send_message", "signal", "telegram", "discord", "slack")):
            for h in list(visible_hits):
                if h.name == "send_message":
                    visible_hits = [h] + [x for x in visible_hits if x.name != "send_message"]
                    break
        if visible_hits:
            for h in visible_hits:
                tools_map.setdefault(h.name, {
                    **_shared_tool_record(h),
                    "hint": "Already in your active tool list — call it directly (do not use tool_call).",
                })
            group["already_available"] = [h.name for h in visible_hits]
            if not matches:
                group["hint"] = (
                    "Matched tools that are already visible in this session. "
                    "Call them directly by name; tool_search only discovers deferred MCP/plugin tools."
                )
        elif not matches and available_sources:
            group["available_sources"] = available_sources
            group["hint"] = (
                "This query returned no lexical matches, but the sources above "
                "are connected and their tools remain available. Retry "
                "tool_search with the service name plus a concrete action or "
                "object before concluding the capability is unavailable. "
                "Also check tools already in your active list (for example "
                "send_message) and call those directly.")
        results.append(group)
'''

HELPER_NEEDLE = '''def _deferrable_in(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
'''

HELPER_INSERT = '''def _visible_in(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """assistant-stack: tools already in the model-facing (non-deferred) list."""
    return classify_tools(tool_defs, load_config_readonly().effective_defer_tools)[0]


def _deferrable_in(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if OLD in text and HELPER_NEEDLE in text:
        text = text.replace(HELPER_NEEDLE, HELPER_INSERT, 1)
        text = text.replace(OLD, NEW, 1)
        TARGET.write_text(text, encoding="utf-8")
        print(f"patched: {TARGET}")
        return 0
    print(f"skip: expected snippet not found in {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
