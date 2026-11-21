#!/usr/bin/env python3
"""Install tool-eval JSONL tracer into Hermes conversation_loop.

Copies tool_eval_trace.py into /opt/hermes/agent/ and inserts a record call
after text-mimicked tool-call recovery and after the tool/text phase (so
recovered calls and final content_len are visible). The anchors are upstream
lines, not the recovery block, but patch-text-tool-call-recovery.py must run
first: without its marker this patch exits. Also patches model_tools web
rewrite path to log rewrites.
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT, edit, install

HELPER_SRC = Path("/bootstrap/tool_eval_trace.py")
HELPER_DST = ROOT / "agent" / "tool_eval_trace.py"
ALIASES_SRC = Path("/bootstrap/stack_tool_aliases.py")
ALIASES_DST = ROOT / "tools" / "stack_tool_aliases.py"
LOOP_TARGET = ROOT / "agent" / "conversation_loop.py"
MODEL_TOOLS = ROOT / "model_tools.py"
MARKER = "# assistant-stack: tool-eval turn trace"
REWRITE_MARKER = "# assistant-stack: tool-eval web rewrite log"

RECOVERY_MARKER = "# assistant-stack: text-mimicked tool-call recovery"
TAG = "[patch-tool-eval-trace]"

# Upstream lines beside the recovery insertion. patch-text-tool-call-recovery.py
# inserts its block before each of these, so the trace lands after recovery.
# v2026.9+ phase loop: trace after the phase, so content_len is final.
ANCHOR_V9 = """            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )
"""
TRACE_V9 = """
            # assistant-stack: tool-eval turn trace
            try:
                from agent.tool_eval_trace import record_assistant_turn

                record_assistant_turn(
                    agent,
                    s.assistant_message,
                    messages=getattr(s, "messages", None)
                    or getattr(agent, "messages", None),
                )
            except Exception:
                logger.debug("tool-eval turn trace failed", exc_info=True)
"""

REWRITE_NEEDLE = """        if _rewrite_name:
            return handle_function_call(
"""

REWRITE_PATCH = """        if _rewrite_name:
            # assistant-stack: tool-eval web rewrite log
            try:
                from agent.tool_eval_trace import record_web_rewrite

                record_web_rewrite(from_name=function_name, to_name=_rewrite_name)
            except Exception:
                pass
            return handle_function_call(
"""


def main() -> None:
    install(HELPER_SRC, HELPER_DST, TAG)
    install(ALIASES_SRC, ALIASES_DST, TAG)
    # The trace goes after the recovery block, so patch-text-tool-call-recovery.py runs first.
    edit(LOOP_TARGET, [(ANCHOR_V9, ANCHOR_V9 + TRACE_V9)], MARKER, TAG, label=f"loop {LOOP_TARGET}",
         requires=[RECOVERY_MARKER])
    # REWRITE_NEEDLE is patch-block-native-web-tools.py's code, so that runs first too.
    edit(MODEL_TOOLS, [(REWRITE_NEEDLE, REWRITE_PATCH)], REWRITE_MARKER, TAG, label=f"web rewrite log in {MODEL_TOOLS}")


if __name__ == "__main__":
    main()
