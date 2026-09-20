#!/usr/bin/env python3
"""Rewrite hallucinated native web_search / web_extract to MCP SearXNG.

disabling agent.disabled_toolsets: [web] removes those tools from the model
schema, but Hermes still dispatches handle_function_call for names the model
invents. Refusing them caused retry loops (WebUI sessions burned dozens of
turns on "disabled on this stack"). When args are mappable, rewrite to the
MCP tools this stack actually exposes; otherwise return a terminal error that
does not invite another web_search/web_extract attempt.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/model_tools.py")
MARKER = "# assistant-stack: rewrite native web tools to mcp"
OLD_MARKERS = (
    MARKER,
    "# assistant-stack: block native web tools (mcp-only)",
    "# assistant-stack: block native web tools when disabled",
    "# assistant-stack: debug-5de041 web tool log",
    "# assistant-stack: debug-53781c native web block",
)

NEEDLE = '''    # Coerce string arguments to their schema-declared types (e.g. "42"→42)
    function_args = coerce_tool_args(function_name, function_args)
    if not isinstance(function_args, dict):
        function_args = {}
    _tool_middleware_trace = list(tool_request_middleware_trace or [])
'''

PATCH = '''    # Coerce string arguments to their schema-declared types (e.g. "42"→42)
    function_args = coerce_tool_args(function_name, function_args)
    if not isinstance(function_args, dict):
        function_args = {}
    _tool_middleware_trace = list(tool_request_middleware_trace or [])

    # assistant-stack: rewrite native web tools to mcp
    # Hallucinated native web_* names are common with weaker tool-callers.
    # Rewrite to MCP when args allow; never execute native web on this stack.
    if function_name in {"web_search", "web_extract"}:
        _rewrite_name = None
        _rewrite_args = {}
        if function_name == "web_search":
            _q = function_args.get("query") or function_args.get("q")
            if isinstance(_q, str) and _q.strip():
                _rewrite_name = "mcp__searxng__searxng_web_search"
                _rewrite_args = {"query": _q.strip()}
                for _src in ("num_results", "limit", "max_results"):
                    if _src in function_args and "num_results" not in _rewrite_args:
                        _rewrite_args["num_results"] = function_args[_src]
        else:
            _url = function_args.get("url")
            if not _url:
                _maybe = function_args.get("query") or function_args.get("q")
                if isinstance(_maybe, str) and _maybe.strip().startswith(("http://", "https://")):
                    _url = _maybe.strip()
            if isinstance(_url, str) and _url.strip():
                _rewrite_name = "mcp__searxng__web_url_read"
                _rewrite_args = {"url": _url.strip()}
        if _rewrite_name:
            return handle_function_call(
                function_name=_rewrite_name,
                function_args=_rewrite_args,
                task_id=task_id,
                tool_call_id=tool_call_id,
                session_id=session_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                user_task=user_task,
                enabled_tools=enabled_tools,
                skip_pre_tool_call_hook=skip_pre_tool_call_hook,
                skip_tool_request_middleware=skip_tool_request_middleware,
                skip_tool_execution_middleware=skip_tool_execution_middleware,
                tool_request_middleware_trace=list(_tool_middleware_trace),
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
            )
        return tool_error(
            f"{function_name} is not available on this stack (native web "
            "toolset disabled). Call mcp__searxng__searxng_web_search with "
            "{query: ...} or mcp__searxng__web_url_read with {url: ...}. "
            "Do not call web_search or web_extract again this turn."
        )
'''


def _strip_marked_blocks(text: str) -> str:
    """Remove previously applied assistant-stack web-tool patches."""
    for marker in OLD_MARKERS:
        while marker in text:
            start = text.find(marker)
            if start < 0:
                break
            region_start = text.rfind("\n", 0, start)
            # Prefer folding an enclosing #region … #endregion if present
            region_open = text.rfind("# #region", 0, start)
            if region_open >= 0 and region_open > region_start:
                end = text.find("# #endregion", start)
                if end >= 0:
                    end = text.find("\n", end)
                    if end >= 0:
                        text = text[:region_open] + text[end + 1 :]
                        continue
            # Rewrite / block form: drop from marker through blank line after return
            end = text.find("\n\n", start)
            if end < 0:
                break
            text = text[: region_start + 1] + text[end + 1 :]
    return text


def _strip_debug_helper(text: str) -> str:
    """Remove orphaned session-53781c debug helper if present."""
    anchor = "\ndef _agent_debug_53781c("
    while anchor in text:
        start = text.find(anchor)
        # helper ends at next top-level def
        nxt = text.find("\ndef ", start + 1)
        if nxt < 0:
            break
        text = text[:start] + text[nxt:]
    return text


def main() -> None:
    if not TARGET.is_file():
        print(f"[patch-block-native-web-tools] skip missing {TARGET}")
        return
    text = TARGET.read_text(encoding="utf-8")
    if (
        MARKER in text
        and "Do not call web_search or web_extract again" in text
        and "_agent_debug_53781c(" not in text
    ):
        print(f"[patch-block-native-web-tools] already applied ({TARGET})")
        return
    text = _strip_marked_blocks(text)
    text = _strip_debug_helper(text)
    if NEEDLE not in text:
        if MARKER in text:
            print(f"[patch-block-native-web-tools] already applied ({TARGET})")
            return
        print(f"[patch-block-native-web-tools] needle missing in {TARGET}")
        return
    TARGET.write_text(text.replace(NEEDLE, PATCH, 1), encoding="utf-8")
    print(f"[patch-block-native-web-tools] patched {TARGET}")


if __name__ == "__main__":
    main()
