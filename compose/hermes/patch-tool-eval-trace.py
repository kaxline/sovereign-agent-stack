#!/usr/bin/env python3
"""Install tool-eval JSONL tracer into Hermes conversation_loop.

Copies tool_eval_trace.py into /opt/hermes/agent/ and inserts a record call
after text-mimicked tool-call recovery and after the tool/text phase (so
recovered calls and final content_len are visible). Also patches model_tools
web rewrite path to log rewrites.
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

# Prefer hooking after the text-recovery block (already applied by 05-patch).
AFTER_RECOVERY_V9 = """            # assistant-stack: text-mimicked tool-call recovery
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

WITH_TRACE_V9 = """            # assistant-stack: text-mimicked tool-call recovery
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

AFTER_RECOVERY_LEGACY = """            # assistant-stack: text-mimicked tool-call recovery
            # Weak models (e.g. Llama 3.3 via OpenRouter) often emit
            # search_files(...) or <function/name=...> as plain text under
            # Hermes' large WebUI prompt. Promote the first valid mimic into
            # structured tool_calls so the normal dispatch path runs.
            # Also reads _current_streamed_assistant_text when content is empty
            # so streamed XML mimic is not classified as an empty response.
            if not getattr(assistant_message, "tool_calls", None):
                try:
                    from agent.text_tool_call_recovery import (
                        try_recover_assistant_tool_calls,
                    )

                    try_recover_assistant_tool_calls(agent, assistant_message)
                except Exception:
                    logger.debug(
                        "text-mimicked tool-call recovery failed",
                        exc_info=True,
                    )

            # Check for tool calls
            if assistant_message.tool_calls:
"""

WITH_TRACE_LEGACY = """            # assistant-stack: text-mimicked tool-call recovery
            # Weak models (e.g. Llama 3.3 via OpenRouter) often emit
            # search_files(...) or <function/name=...> as plain text under
            # Hermes' large WebUI prompt. Promote the first valid mimic into
            # structured tool_calls so the normal dispatch path runs.
            # Also reads _current_streamed_assistant_text when content is empty
            # so streamed XML mimic is not classified as an empty response.
            if not getattr(assistant_message, "tool_calls", None):
                try:
                    from agent.text_tool_call_recovery import (
                        try_recover_assistant_tool_calls,
                    )

                    try_recover_assistant_tool_calls(agent, assistant_message)
                except Exception:
                    logger.debug(
                        "text-mimicked tool-call recovery failed",
                        exc_info=True,
                    )

            # assistant-stack: tool-eval turn trace
            try:
                from agent.tool_eval_trace import record_assistant_turn

                record_assistant_turn(
                    agent,
                    assistant_message,
                    messages=getattr(agent, "messages", None),
                )
            except Exception:
                logger.debug("tool-eval turn trace failed", exc_info=True)

            # Check for tool calls
            if assistant_message.tool_calls:
"""

# Fallback when recovery patch is absent (should not happen if 05-patch order is correct).
FALLBACK_V9 = """            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )
"""

FALLBACK_WITH_TRACE_V9 = """            _v = _run_phase(
                run_tool_round if s.assistant_message.tool_calls else finish_text_response, agent, s
            )

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


def _patch_loop() -> None:
    text = LOOP_TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[patch-tool-eval-trace] loop already applied ({LOOP_TARGET})")
        return
    if AFTER_RECOVERY_V9 in text:
        LOOP_TARGET.write_text(text.replace(AFTER_RECOVERY_V9, WITH_TRACE_V9, 1), encoding="utf-8")
        print(f"[patch-tool-eval-trace] patched loop (v9+recovery) {LOOP_TARGET}")
        return
    if AFTER_RECOVERY_LEGACY in text:
        LOOP_TARGET.write_text(
            text.replace(AFTER_RECOVERY_LEGACY, WITH_TRACE_LEGACY, 1), encoding="utf-8"
        )
        print(f"[patch-tool-eval-trace] patched loop (legacy+recovery) {LOOP_TARGET}")
        return
    if FALLBACK_V9 in text:
        LOOP_TARGET.write_text(
            text.replace(FALLBACK_V9, FALLBACK_WITH_TRACE_V9, 1), encoding="utf-8"
        )
        print(f"[patch-tool-eval-trace] patched loop (v9 fallback) {LOOP_TARGET}")
        return
    print(f"[patch-tool-eval-trace] warn: conversation_loop needle missing in {LOOP_TARGET}")


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
