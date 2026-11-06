"""Serve the searxng MCP tools from SearXNG, the keyless ring, or both.

Compatibility overlay (not upstream). ``HERMES_WEB_SEARCH_MODE`` picks who
answers ``searxng_web_search`` and ``web_url_read``:

- ``private`` (default): SearXNG and mcp-searxng's plain fetch, unchanged.
- ``private-first``: SearXNG; on failure, Hermes' keyless ring.
- ``keyless-first``: the ring; on failure, SearXNG.
- ``keyless``: the ring only.

The ring is upstream's anonymous free tier (``plugins/web/keyless_mcp.py``:
Exa, Parallel, Firecrawl, Keenable), called in-process so its rotation and
failover stay upstream's. patch-web-search-mode.py makes ``_make_tool_handler``
return ``wrap_handler(...)``. Upstream is imported only inside functions, so
this file imports on the host for scripts/test-web-search-mode.py.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

try:
    from agent.blocked_fetch_hint import hint_for
except ImportError:  # host tests
    from blocked_fetch_hint import hint_for

logger = logging.getLogger(__name__)

ENV = "HERMES_WEB_SEARCH_MODE"
MODES: tuple[str, ...] = ("private", "private-first", "keyless-first", "keyless")
FETCH_TOOL = "mcp__searxng__web_url_read"
SEARCH_FILTERS = ("pageno", "time_range", "language", "safesearch")
READ_FILTERS = ("section", "paragraphRange", "readHeadings")
UNAVAILABLE_MARK = "Some search engines were unavailable"

_warned: set[str] = set()


def current_mode(environ: Mapping[str, str] = os.environ) -> str:
    """The configured mode; an unknown value is ``private``, with one warning per value."""
    raw = environ.get(ENV, "") or ""
    mode = raw.strip().lower()
    if not mode:
        return "private"
    if mode in MODES:
        return mode
    if raw not in _warned:
        _warned.add(raw)
        logger.warning("%s=%r is not one of %s; using private", ENV, raw, ", ".join(MODES))
    return "private"


def _parsed(result: Any) -> Any:
    if isinstance(result, (dict, list)):
        return result
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


def is_tool_error(result: Any) -> bool:
    data = _parsed(result)
    return isinstance(data, dict) and "error" in data


def _result_text(result: Any) -> str:
    data = _parsed(result)
    if isinstance(data, dict):
        value = data.get("result")
        return value if isinstance(value, str) else json.dumps(value) if value is not None else ""
    return result if isinstance(result, str) else ""


def search_failed(result: Any) -> bool:
    """A tool error, or no results while engines were unavailable (suspended, gate busy)."""
    if is_tool_error(result):
        return True
    text = _result_text(result)
    inner = _parsed(text)
    if isinstance(inner, dict) and "results" in inner:
        return not inner.get("results") and bool(inner.get("unresponsive_engines"))
    has_results = any(line.startswith("URL: ") for line in text.splitlines())
    return not has_results and UNAVAILABLE_MARK in text


def fetch_failed(args: Any, result: Any) -> bool:
    """A tool error, or a page the blocked-fetch hint would flag (refused, empty JS page, bot check)."""
    return is_tool_error(result) or hint_for(FETCH_TOOL, args, result) is not None


def _handler_json(text: str) -> str:
    return json.dumps({"result": text}, ensure_ascii=False)


def _set(args: Mapping[str, Any], names: tuple[str, ...]) -> list[str]:
    return [n for n in names if args.get(n) not in (None, "", False)]


def render_search(resp: dict, args: Mapping[str, Any]) -> str:
    """Handler JSON for a successful ring search, in mcp-searxng's text (or compact JSON) format."""
    data = resp.get("data") or {}
    vendor = data.get("served_by") or "unknown"
    rows = data.get("web") or []
    if args.get("response_format") == "json":
        body = {"results": [{"title": r.get("title", ""), "url": r.get("url", ""),
                             "content": r.get("description", "")} for r in rows],
                "served_by": vendor}
        return _handler_json(json.dumps(body, ensure_ascii=False))
    parts = [f"Served by the keyless web tier ({vendor})."]
    ignored = _set(args, SEARCH_FILTERS)
    if ignored:
        parts.append(f"The keyless web tier does not support {', '.join(ignored)}; they were not applied.")
    if rows:
        parts.extend(f"Title: {r.get('title', '')}\nDescription: {r.get('description', '')}\nURL: {r.get('url', '')}"
                     for r in rows)
    else:
        parts.append(f'No results found for "{args.get("query", "")}".')
    return _handler_json("\n\n".join(parts))


def render_extract(entry: dict, args: Mapping[str, Any]) -> str:
    """Handler JSON for a successful ring page read, sliced like mcp-searxng's startChar/maxLength."""
    content = str(entry.get("content") or "")
    start = max(int(args.get("startChar") or 0), 0)
    length = args.get("maxLength")
    content = content[start:start + int(length)] if length else content[start:]
    parts = ["Served by the keyless web tier."]
    if _set(args, READ_FILTERS):
        parts.append("The keyless web tier returned the whole page; section, paragraphRange and readHeadings "
                     "were not applied.")
    parts.append(content)
    return _handler_json("\n\n".join(parts))
