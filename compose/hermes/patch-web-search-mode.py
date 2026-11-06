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
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
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
    for name in ("web_search_mode.py", "blocked_fetch_hint.py"):
        src = BOOTSTRAP / name
        if not src.is_file():
            raise SystemExit(f"{TAG} missing {src}")
        shutil.copyfile(src, AGENT / name)
    print(f"{TAG} installed {AGENT / 'web_search_mode.py'}")

    handlers = ROOT / "tools" / "mcp_tool_handlers.py"
    text = handlers.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TAG} already applied")
        return
    missing = [n.strip() for n in NEEDED if n not in text]
    if text.count(DEF_ANCHOR) != 1:
        missing.append("_make_tool_handler signature")
    if text.count(RETURN_ANCHOR) != 1:
        missing.append("_make_tool_handler return")
    if missing:
        raise SystemExit(f"{TAG} expected code missing in {handlers}: {missing}. "
                         "Upstream changed; update the anchors and scripts/test-web-search-mode.py.")
    text = text.replace(DEF_ANCHOR, HELPER + DEF_ANCHOR).replace(RETURN_ANCHOR, RETURN_NEW)
    handlers.write_text(text, encoding="utf-8")
    print(f"{TAG} patched {handlers}")


if __name__ == "__main__":
    main()
