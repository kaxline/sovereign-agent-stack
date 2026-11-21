#!/usr/bin/env python3
"""Install text-mimicked tool-call recovery into Hermes conversation_loop.

Compatibility overlay (not for Hermes upstream — see #29115 / #35129).
Copies the helper module into /opt/hermes/agent/ and inserts a call site
immediately before the structured tool_calls dispatch check.
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT, edit, install

HELPER_SRC = Path("/bootstrap/text_tool_call_recovery.py")
HELPER_DST = ROOT / "agent" / "text_tool_call_recovery.py"
TARGET = ROOT / "agent" / "conversation_loop.py"
TAG = "[patch-text-tool-call-recovery]"
MARKER = "# assistant-stack: text-mimicked tool-call recovery"

# v2026.9+ phase-based loop. Weak models (e.g. Llama 3.3 via OpenRouter) emit
# search_files(...) or <function/name=...> as plain text; the first valid mimic
# becomes structured tool_calls so the normal dispatch path runs.
OLD_V9 = """            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )
"""

NEW_V9 = """            # assistant-stack: text-mimicked tool-call recovery
            if not getattr(s.assistant_message, "tool_calls", None):
                try:
                    from agent.text_tool_call_recovery import (
                        try_recover_assistant_tool_calls,
                    )

                    try_recover_assistant_tool_calls(agent, s.assistant_message)
                except Exception:
                    logger.debug(
                        "text-mimicked tool-call recovery failed",
                        exc_info=True,
                    )

            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )
"""


def main() -> None:
    install(HELPER_SRC, HELPER_DST, TAG)
    edit(TARGET, [(OLD_V9, NEW_V9)], MARKER, TAG)


if __name__ == "__main__":
    main()
