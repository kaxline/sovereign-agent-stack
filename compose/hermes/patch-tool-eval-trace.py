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

import shutil
from pathlib import Path

HELPER_SRC = Path("/bootstrap/tool_eval_trace.py")
HELPER_DST = Path("/opt/hermes/agent/tool_eval_trace.py")
LOOP_TARGET = Path("/opt/hermes/agent/conversation_loop.py")
MODEL_TOOLS = Path("/opt/hermes/model_tools.py")
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

# Pre-v2026.9 loop: trace before the dispatch check.
ANCHOR_LEGACY = """            # Check for tool calls
            if assistant_message.tool_calls:
"""
TRACE_LEGACY = """            # assistant-stack: tool-eval turn trace
            try:
                from agent.tool_eval_trace import record_assistant_turn

                record_assistant_turn(
                    agent,
                    assistant_message,
                    messages=getattr(agent, "messages", None),
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


def _patch_loop() -> None:
    text = LOOP_TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TAG} loop already applied ({LOOP_TARGET})")
        return
    if RECOVERY_MARKER not in text:
        raise SystemExit(
            f"{TAG} text-tool-call recovery not applied in {LOOP_TARGET}; run "
            "patch-text-tool-call-recovery.py first, or the Hermes image may have changed — update this patch."
        )
    if text.count(ANCHOR_V9) == 1:
        text = text.replace(ANCHOR_V9, ANCHOR_V9 + TRACE_V9, 1)
    elif text.count(ANCHOR_LEGACY) == 1:
        text = text.replace(ANCHOR_LEGACY, TRACE_LEGACY + ANCHOR_LEGACY, 1)
    else:
        raise SystemExit(
            f"{TAG} expected 1 x tool_calls dispatch in {LOOP_TARGET}, found "
            f"{text.count(ANCHOR_V9)} (v9) / {text.count(ANCHOR_LEGACY)} (legacy). "
            "Hermes image may have changed — update this patch."
        )
    LOOP_TARGET.write_text(text, encoding="utf-8")
    print(f"{TAG} patched loop {LOOP_TARGET}")


def _patch_web_rewrite() -> None:
    if not MODEL_TOOLS.is_file():
        print(f"[patch-tool-eval-trace] skip web rewrite log: missing {MODEL_TOOLS}")
        return
    text = MODEL_TOOLS.read_text(encoding="utf-8")
    if REWRITE_MARKER in text:
        print(f"[patch-tool-eval-trace] web rewrite log already applied")
        return
    if REWRITE_NEEDLE not in text:
        print(f"[patch-tool-eval-trace] warn: web rewrite needle missing (apply block-native first)")
        return
    MODEL_TOOLS.write_text(text.replace(REWRITE_NEEDLE, REWRITE_PATCH, 1), encoding="utf-8")
    print(f"[patch-tool-eval-trace] patched web rewrite log in {MODEL_TOOLS}")


def main() -> None:
    if not HELPER_SRC.is_file():
        raise SystemExit(f"[patch-tool-eval-trace] missing {HELPER_SRC}")
    HELPER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"[patch-tool-eval-trace] installed {HELPER_DST}")
    if not LOOP_TARGET.is_file():
        print(f"[patch-tool-eval-trace] skip missing {LOOP_TARGET}")
        return
    _patch_loop()
    _patch_web_rewrite()


if __name__ == "__main__":
    main()
