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
import re
from typing import Any, Callable, Mapping

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


# Errors that mean SearXNG (or the MCP server in front of it) is unavailable. Argument errors and
# configuration errors are not here: SearXNG is healthy, and sending the query out would only leak it.
_MCP_DOWN = r"MCP server 'searxng'|transport is down|not connected"
_SEARCH_UNAVAILABLE = re.compile(
    _MCP_DOWN + r"|Connection Error|DNS Error|Timeout Error|Network Error|SearXNG Error \((?:403|429|5\d\d)\)"
    r"|Search Degraded|SearXNG Response Error|SearXNG Data Error|^(?:Runtime|Timeout|Connection|OS)Error: ")
# For a page read, the target's own 404 or missing hostname is the page's answer, not a block.
_FETCH_UNAVAILABLE = re.compile(_MCP_DOWN + r"|Connection Error|Timeout Error|Network Error"
                                r"|^(?:Runtime|Timeout|Connection|OS)Error: ")


def _error_text(result: Any) -> str:
    data = _parsed(result)
    return str(data["error"]) if isinstance(data, dict) and "error" in data else ""


def search_failed(result: Any) -> bool:
    """SearXNG unavailable (breaker, transport, engines blocked, gate busy); not an argument error."""
    if is_tool_error(result):
        return bool(_SEARCH_UNAVAILABLE.search(_error_text(result)))
    text = _result_text(result)
    inner = _parsed(text)
    if isinstance(inner, dict) and "results" in inner:
        return not inner.get("results") and bool(inner.get("unresponsive_engines"))
    has_results = any(line.startswith("URL: ") for line in text.splitlines())
    return not has_results and UNAVAILABLE_MARK in text


def fetch_failed(args: Any, result: Any) -> bool:
    """A page the blocked-fetch hint would flag (refused, empty JS page, bot check), or the fetch path down."""
    if hint_for(FETCH_TOOL, args, result) is not None:
        return True
    return is_tool_error(result) and bool(_FETCH_UNAVAILABLE.search(_error_text(result)))


def _reason(result: Any) -> str:
    """Why a side failed, in words that match none of the pause or blocked-fetch hints' patterns."""
    error = _error_text(result)
    text = error or _result_text(result)
    status = re.search(r"Error \((\d{3})\)", text)
    if re.search(_MCP_DOWN, text):
        return "the searxng MCP server is paused or unreachable"
    if status:
        return f"HTTP {status.group(1)}"
    if "Timeout" in text:
        return "it timed out"
    if "Connection Error" in text or "Network Error" in text:
        return "a network error"
    if "Search Degraded" in text or UNAVAILABLE_MARK in text:
        return "its search engines were unavailable"
    if "appears empty" in text:
        return "the page came back empty, most likely because it needs JavaScript"
    if re.match(r"\w+Error: ", error):
        return "the searxng tool raised an error"
    if not error:
        return "it returned a bot-check page"
    return "it failed"


def _handler_json(text: str) -> str:
    return json.dumps({"result": text}, ensure_ascii=False)


def _int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


SERVER = "searxng"
SEARCH_TOOL_NAME = "searxng_web_search"
READ_TOOL_NAME = "web_url_read"
RING_START = "exa"  # first ring vendor; upstream's cursor decides the real order unless a vendor is pinned
DEFAULT_LIMIT = 10
DISABLED = "keyless web tier disabled (web.keyless_fallback: false)"
SEARXNG_SIDE, RING_SIDE = "SearXNG", "the keyless web tier"


def _tool_error(message: str) -> str:
    try:
        from tools.registry import tool_error
    except ImportError:  # host tests
        return json.dumps({"error": message})
    return tool_error(message)


# --- Seams (replaced by the host test) ------------------------------------------------------------
def _ring_search(query: str, limit: int) -> dict:
    from plugins.web.keyless_mcp import search_with_failover
    return search_with_failover(RING_START, query, limit)


def _ring_extract(url: str) -> dict:
    from plugins.web.keyless_mcp import extract_with_failover
    results = extract_with_failover(RING_START, [url])
    return results[0] if results else {"url": url, "error": "keyless web tier returned no page"}


def _ring_enabled() -> bool:
    from plugins.web.keyless_mcp import keyless_enabled
    return keyless_enabled()


def _url_refusal(url: str) -> str | None:
    """Upstream's refusal for a URL that must not leave the machine (secrets, private address), else None."""
    from tools.web_tools_extract import _validate_extract_urls
    blocked = _validate_extract_urls([url])[3]
    if blocked is not None:
        return blocked if isinstance(blocked, str) else json.dumps(blocked)
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        return _tool_error("Blocked: URL targets a private or internal network address")
    return None


# --- Wrapper --------------------------------------------------------------------------------------
def _failure_text(result: Any) -> str:
    data = _parsed(result)
    if isinstance(data, dict) and "error" in data:
        return str(data["error"])
    return _result_text(result)


def _with_note(result: str, note: str) -> str:
    """``result`` (handler JSON) with ``note`` as its first paragraph, or a ``note`` key for JSON output."""
    data = _parsed(result)
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        return result
    inner = _parsed(data["result"])
    if isinstance(inner, dict):
        inner["note"] = note
        data["result"] = json.dumps(inner, ensure_ascii=False)
    else:
        data["result"] = f"{note}\n\n{data['result']}"
    return json.dumps(data, ensure_ascii=False)


def _with_fallback_failure(result: str, other_side: str, other_error: str) -> str:
    suffix = f" (fallback via {other_side} also failed: {other_error})"
    data = _parsed(result)
    if isinstance(data, dict) and "error" in data:
        return _tool_error(f"{data['error']}{suffix}")
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        data["result"] += suffix
        return json.dumps(data, ensure_ascii=False)
    return result


def _ring_url(args: Mapping[str, Any]) -> str | None:
    from urllib.parse import urlsplit
    url = args.get("url") if isinstance(args, Mapping) else None
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    return url.strip() if parts.scheme in ("http", "https") and parts.hostname else None


def wrap_handler(server_name: str, tool_name: str, inner: Callable[..., Any]) -> Callable[..., Any]:
    """``inner`` for every tool but the searxng server's search and page read, which follow the mode."""
    if server_name != SERVER or tool_name not in (SEARCH_TOOL_NAME, READ_TOOL_NAME):
        return inner
    is_search = tool_name == SEARCH_TOOL_NAME
    what = "search" if is_search else "page read"

    def _private(args: dict, kwargs: dict) -> tuple[str, bool]:
        try:
            result = inner(args, **kwargs)
        except Exception as exc:  # fallback modes only; private mode never gets here
            logger.warning("searxng %s raised in %s: %s", tool_name, current_mode(), exc)
            return _tool_error(f"{type(exc).__name__}: {exc}"), True
        failed = search_failed(result) if is_search else fetch_failed(args, result)
        return result, failed

    def _ring(args: dict) -> tuple[str, bool]:
        try:
            return _ring_call(args)
        except Exception as exc:
            logger.warning("keyless web tier %s raised: %s", tool_name, exc)
            return _tool_error(f"keyless web tier error: {type(exc).__name__}: {exc}"), True

    def _ring_call(args: dict) -> tuple[str, bool]:
        if not _ring_enabled():
            return _tool_error(DISABLED), True
        if is_search:
            limit = min(max(_int(args.get("num_results"), DEFAULT_LIMIT) or DEFAULT_LIMIT, 1), 20)
            resp = _ring_search(str(args.get("query") or ""), limit)
            if not resp.get("success"):
                return _tool_error(str(resp.get("error") or "keyless search failed")), True
            resp.setdefault("data", {}).setdefault("served_by", RING_START)  # upstream omits it when unchanged
            return render_search(resp, args), False
        url = _ring_url(args) or ""
        refusal = _url_refusal(url)
        if refusal is not None:
            return refusal, True
        entry = _ring_extract(url)
        if entry.get("error"):
            return _tool_error(str(entry["error"])), True
        if not str(entry.get("content") or "").strip():
            return _tool_error("keyless web tier returned an empty page"), True
        return render_extract(entry, args), False

    def _handler(args: dict, **kwargs: Any) -> Any:
        mode = current_mode()
        if mode == "private" or (not is_search and _ring_url(args) is None):
            return inner(args, **kwargs)
        if mode == "keyless":
            result, failed = _ring(args)
            if failed:
                return _tool_error(f"{_failure_text(result)}. SearXNG is not used in this mode "
                                   "(HERMES_WEB_SEARCH_MODE=keyless).")
            return result
        if mode == "private-first":
            first, second, first_side, second_side = _private, _ring, SEARXNG_SIDE, RING_SIDE
        else:
            first, second, first_side, second_side = _ring, _private, RING_SIDE, SEARXNG_SIDE
        a = (args, kwargs) if first is _private else (args,)
        result, failed = first(*a)
        if not failed:
            return result
        b = (args, kwargs) if second is _private else (args,)
        other, other_failed = second(*b)
        reason = _reason(result) if first is _private else _failure_text(result)[:200]
        if not other_failed:
            label = first_side[0].upper() + first_side[1:]
            return _with_note(other, f"{label} was unavailable for this {what} ({reason}).")
        return _with_fallback_failure(result, second_side, _failure_text(other))

    return _handler


def render_extract(entry: dict, args: Mapping[str, Any]) -> str:
    """Handler JSON for a successful ring page read, sliced like mcp-searxng's startChar/maxLength."""
    content = str(entry.get("content") or "")
    start = max(_int(args.get("startChar"), 0) or 0, 0)
    length = _int(args.get("maxLength"), None)
    content = content[start:start + length] if length and length > 0 else content[start:]
    parts = ["Served by the keyless web tier."]
    if _set(args, READ_FILTERS):
        parts.append("The keyless web tier returned the whole page; section, paragraphRange and readHeadings "
                     "were not applied.")
    parts.append(content)
    return _handler_json("\n\n".join(parts))
