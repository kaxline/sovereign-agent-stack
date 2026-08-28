#!/usr/bin/env python3
"""Force-import send_message_tool after builtin discovery.

Discovery is cached under HERMES_HOME/cache/tool_discovery_cache.json. A stale
``registers=False`` entry (from before we re-registered the tool) can make the
gateway skip importing send_message_tool for the life of the process — WebUI
then has no send_message in its schema even though toolsets.py lists it.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/model_tools.py")
MARKER = "# assistant-stack: force-import send_message_tool"

OLD = """discover_builtin_tools()
"""

NEW = """discover_builtin_tools()
# assistant-stack: force-import send_message_tool
# Bypass stale tool_discovery_cache registers=False entries so WebUI always
# gets the re-registered send_message tool.
try:
    import tools.send_message_tool  # noqa: F401
except Exception:
    pass
"""


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
