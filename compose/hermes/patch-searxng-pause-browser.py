#!/usr/bin/env python3
"""Point the model at browser search while the searxng MCP server is paused.

Compatibility overlay. After 3 failed calls in a row, upstream's MCP circuit
breaker pauses that server for 60 seconds and answers every call with an
error that says to "use a different approach" without naming one. On
2026-11-05 a turn kept calling searxng_web_search through the pause until
same_tool_failure_halt ended it, instead of searching in the browser.

This wraps tools/mcp_tool_handlers.py's _check_circuit_breaker: for the
searxng server only, the pause error also says how to search with
browser_exec and not to call searxng tools until the pause ends. Other MCP
servers keep upstream's text.

Run scripts/test-searxng-pause-browser.py on Hermes bumps.
"""
from __future__ import annotations

from pathlib import Path

HANDLERS = Path("/opt/hermes/tools/mcp_tool_handlers.py")
MARKER = "# assistant-stack: searxng pause points at browser search"
ANCHOR = "def _check_circuit_breaker(server_name: str) -> Optional[str]:\n"

WRAPPER = f'''

{MARKER}
_SEARCH_PAUSE_SERVERS = frozenset({{"searxng"}})
_SEARCH_PAUSE_HINT = (
    " While searxng is paused, search in the browser if you have browser_exec: "
    "new_tab('https://www.bing.com/search?q=two+to+six+keywords') (keywords joined with +), "
    "wait_for_load(), then print(page_info()) to read the results, and goto_url(link) to open one. "
    "Don't call searxng tools again until the pause ends."
)
_upstream_check_circuit_breaker = _check_circuit_breaker


def _check_circuit_breaker(server_name: str) -> Optional[str]:
    error = _upstream_check_circuit_breaker(server_name)
    if error is None or server_name not in _SEARCH_PAUSE_SERVERS:
        return error
    try:
        message = json.loads(error)["error"]
    except (ValueError, KeyError, TypeError):
        return error
    return tool_error(message + _SEARCH_PAUSE_HINT)
'''


def main() -> None:
    if not HANDLERS.is_file():
        print(f"[patch-searxng-pause-browser] skip: {HANDLERS} missing")
        return
    text = HANDLERS.read_text(encoding="utf-8")
    if MARKER in text:
        print("[patch-searxng-pause-browser] already applied")
        return
    # The wrapper relies on these upstream names; check them before writing.
    for needed in (ANCHOR, "\nimport json\n", "import invalidate_check_fn_cache, tool_error\n"):
        if text.count(needed) != 1:
            print(f"[patch-searxng-pause-browser] anchor missing, not patched: {needed.strip()}")
            return
    HANDLERS.write_text(text.rstrip("\n") + "\n" + WRAPPER, encoding="utf-8")
    print(f"[patch-searxng-pause-browser] patched {HANDLERS}")


if __name__ == "__main__":
    main()
