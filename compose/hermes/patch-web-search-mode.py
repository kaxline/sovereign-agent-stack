#!/usr/bin/env python3
"""Let HERMES_WEB_SEARCH_MODE send the searxng MCP tools to the keyless ring.

Compatibility overlay (not upstream). Copies web_search_mode.py (and the
blocked_fetch_hint.py it imports) into the agent package, and makes
tools/mcp_tool_handlers.py's _make_tool_handler return
web_search_mode.wrap_handler(...) around its handler. wrap_handler leaves
every tool but searxng_web_search and web_url_read alone, and returns
upstream's handler untouched in the default `private` mode. See
web_search_mode.py.

Run scripts/test-web-search-mode.py on Hermes bumps.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, edit, install

AGENT = ROOT / "agent"
BOOTSTRAP = Path(os.environ.get("WEB_SEARCH_MODE_BOOTSTRAP", "/bootstrap"))
TAG = "[patch-web-search-mode]"
MARKER = "# assistant-stack: web search mode"

DEF_ANCHOR = (
    "def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float, *, "
    "native_images: bool = False):\n"
)
HELPER = (
    f"{MARKER}\n"
    "def _wrap_web_search_mode(server_name, tool_name, handler):\n"
    "    try:\n"
    "        from agent.web_search_mode import wrap_handler\n"
    "    except Exception:\n"
    "        logger.warning(\"web search mode overlay unavailable; searxng tools use upstream handlers\", exc_info=True)\n"
    "        return handler\n"
    "    return wrap_handler(server_name, tool_name, handler)\n"
    "\n\n"
)
RETURN_ANCHOR = (
    "        return _mcp_result_with_native_images(text, image_paths) if native_images and not "
    "_result_is_error(text) else text\n"
    "    return _handler\n"
)
RETURN_NEW = (
    "        return _mcp_result_with_native_images(text, image_paths) if native_images and not "
    "_result_is_error(text) else text\n"
    "    return _wrap_web_search_mode(server_name, tool_name, _handler)\n"
)
NEEDED = ("\nlogger = logging.getLogger(\"tools.mcp_tool\")\n",)


def main() -> None:
    install(BOOTSTRAP / "web_search_mode.py", AGENT / "web_search_mode.py", TAG)
    install(BOOTSTRAP / "blocked_fetch_hint.py", AGENT / "blocked_fetch_hint.py", TAG)
    edit(ROOT / "tools" / "mcp_tool_handlers.py", [(DEF_ANCHOR, HELPER + DEF_ANCHOR), (RETURN_ANCHOR, RETURN_NEW)],
         MARKER, TAG, requires=NEEDED)


if __name__ == "__main__":
    main()
