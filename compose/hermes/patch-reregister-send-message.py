#!/usr/bin/env python3
"""Re-register send_message as an agent-callable tool for WebUI/CLI.

Upstream intentionally left the send engine unregistered so agents would not
fire cross-platform messages on their own. This stack's WebUI (browser profile,
hermes-cli toolset) needs that tool so users can ask the agent to deliver to
Signal/Telegram/etc. Cron and `hermes send` already import the helpers
directly; this only restores the model-facing registry entry + core toolset
membership.
"""
from __future__ import annotations

from pathlib import Path

TOOL_TARGET = Path("/opt/hermes/tools/send_message_tool.py")
TOOLSETS_TARGET = Path("/opt/hermes/toolsets.py")
TOOL_MARKER = "# assistant-stack: re-register send_message for agent"
TOOLSETS_MARKER = "# assistant-stack: include send_message in core tools"

TOOL_OLD = '''# --- Registry ---
from tools.registry import tool_error

# NOTE: ``send_message`` is intentionally NOT registered as an agent-callable
# model tool. The agent should not decide on its own to fire off cross-platform
# messages or reactions. The send engine in this module (``_send_to_platform``,
# ``_send_via_adapter``, ``_parse_target_ref``, the per-platform ``_send_*``
# helpers) remains the shared transport used by:
#   - cron delivery (cron/scheduler.py)
#   - the ``hermes send`` CLI command (hermes_cli/send_cmd.py)
#   - the gateway kanban notifier (dashboard-toggled, outside agent control)
#   - the standalone MCP server (mcp_serve.py), which is an opt-in surface
# Those callers import the helpers directly; none of them need the registry
# entry.
'''

TOOL_NEW = '''# --- Registry ---
from tools.registry import registry, tool_error

# assistant-stack: re-register send_message for agent
# Upstream leaves this unregistered; WebUI (hermes-cli) needs the model tool
# so users can ask to deliver to Signal and other platforms. Cron / hermes send
# still import the helpers directly.
registry.register(
    name="send_message",
    toolset="send_message",
    schema=SEND_MESSAGE_SCHEMA,
    handler=lambda args, **kw: send_message_tool(args, **kw),
    check_fn=lambda: True,  # always expose in WebUI/api_server tool lists
    emoji="📨",
)
'''

# Insert next to clarify in _HERMES_CORE_TOOLS only (not WEBHOOK_SAFE_TOOLS).
TOOLSETS_OLD = '''    # Clarifying questions
    "clarify",
    # Code execution + delegation
'''

TOOLSETS_NEW = '''    # Clarifying questions
    "clarify",
    "send_message",  # assistant-stack: include send_message in core tools
    # Code execution + delegation
'''


def _patch_file(path: Path, old: str, new: str, marker: str) -> str:
    if not path.is_file():
        return f"skip: missing {path}"
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return f"already patched: {path}"
    if old not in text:
        return f"skip: expected snippet not found in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"patched: {path}"


def main() -> int:
    print(_patch_file(TOOL_TARGET, TOOL_OLD, TOOL_NEW, TOOL_MARKER))
    print(_patch_file(TOOLSETS_TARGET, TOOLSETS_OLD, TOOLSETS_NEW, TOOLSETS_MARKER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
