#!/usr/bin/env python3
"""Keep tool-loop warnings outside the untrusted-data wrapper.

Compatibility overlay. Upstream appends loop warnings and halt notices to the
raw tool result, so for mcp_*, browser_* and web tools they land inside
<untrusted_tool_result>, whose preamble tells the model not to follow
directives in the block. Queue them on plugin_live_notes instead:
tool_executor.py attaches that queue to the tool message after wrapping, and
keeps it on the row if the turn budget spills the content to a file.

Run scripts/test-tool-loop-notes.py on Hermes bumps.
"""
from __future__ import annotations

from overlay_patch import ROOT, edit

RUN_AGENT = ROOT / "run_agent.py"
TAG = "[patch-tool-loop-notes]"
MARKER = "# assistant-stack: tool-loop notes outside the untrusted wrapper"

OLD_DECISION = (
    "        if decision.action in {\"warn\", \"halt\"}:\n"
    "            function_result = append_toolguard_guidance(function_result, decision)\n"
)
NEW_DECISION = (
    f"        {MARKER}\n"
    "        def _queue_loop_note(code, note):\n"
    "            from agent.plugin_live_notes import queue_plugin_notice\n"
    "            queue_plugin_notice(self, f\"tool-loop:{tool_call_id}:{code}\", note.strip())\n"
    "        if decision.action in {\"warn\", \"halt\"}:\n"
    "            _queue_loop_note(decision.code, append_toolguard_guidance(\"\", decision))\n"
)

OLD_STREAK = (
    "                function_result = append_toolguard_guidance(function_result, streak_halt)\n"
)
NEW_STREAK = (
    "                _queue_loop_note(streak_halt.code, append_toolguard_guidance(\"\", streak_halt))\n"
)

OLD_NOTICE = (
    "        if stall_notice:\n"
    "            function_result = (function_result or \"\") + \"\\n\\n\" + stall_notice\n"
)
NEW_NOTICE = (
    "        if stall_notice:\n"
    "            _queue_loop_note(stall_kind or \"stall\", stall_notice)\n"
)


def main() -> None:
    edit(RUN_AGENT, [(OLD_DECISION, NEW_DECISION), (OLD_STREAK, NEW_STREAK), (OLD_NOTICE, NEW_NOTICE)], MARKER, TAG)


if __name__ == "__main__":
    main()
