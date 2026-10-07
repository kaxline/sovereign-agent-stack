#!/usr/bin/env python3
"""Reject stop turns that claim a tool result without calling a tool, or
that ask the user for a password in chat.

Compatibility overlay (not for Hermes upstream). Copies fabricated_result.py
into /opt/hermes/agent/ and:

- continues the turn from turn_final_response.py (max 2) with an ephemeral
  placeholder, so the fabricated claim or password request is not stored as
  the assistant reply
- skips those rows in session_persistence.py
- treats the nudge as synthetic in context_compressor.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

HELPER_SRC = Path("/bootstrap/fabricated_result.py")
HELPER_DST = Path("/opt/hermes/agent/fabricated_result.py")
TURN_FINAL = Path("/opt/hermes/agent/turn_final_response.py")
PERSIST = Path("/opt/hermes/agent/session_persistence.py")
COMPRESSOR = Path("/opt/hermes/agent/context_compressor.py")
MARKER = "# assistant-stack: fabricated result without a tool call"

OLD_FLAGS = """_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_thinking_prefill", "_empty_recovery_synthetic", "_empty_terminal_sentinel",
    "_dropped_toolcall_nudge",
)
"""

NEW_FLAGS = """_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_thinking_prefill", "_empty_recovery_synthetic", "_empty_terminal_sentinel",
    "_dropped_toolcall_nudge",
    "_fabricated_result_nudge",  # assistant-stack: claimed result with no tool call
)
"""

OLD_GENUINE = """    # Genuine turn end (no dropped-tool-call mismatch): clear stall budget.
    agent._dropped_toolcall_retries = 0
"""

NEW_GENUINE = """    # assistant-stack: fabricated result without a tool call
    # A stop turn that claims a write (or any <result>) with zero tool_calls,
    # or asks the user for a password in chat, must not become the durable
    # answer. Keep a placeholder plus a nudge in memory only, then continue.
    # Cap at 2 so a model that keeps doing it can still end the turn.
    if (
        not getattr(assistant_message, "tool_calls", None)
        and getattr(agent, "_fabricated_result_nudges", 0) < 2
    ):
        try:
            from agent.fabricated_result import turn_end_nudge

            _turn_end_nudge = turn_end_nudge(
                final_response or "",
                has_tool_calls=False,
            )
        except Exception:
            logger.debug("fabricated-result check failed", exc_info=True)
            _turn_end_nudge = None
        if _turn_end_nudge:
            _placeholder_content, _nudge_content = _turn_end_nudge
            agent._fabricated_result_nudges = (
                getattr(agent, "_fabricated_result_nudges", 0) + 1
            )
            logger.info(
                "%s — re-prompting (%d/2)",
                _placeholder_content.rstrip("."),
                agent._fabricated_result_nudges,
            )
            agent._emit_status(
                f"↻ {_placeholder_content.rstrip('.')} — "
                f"re-prompting ({agent._fabricated_result_nudges}/2)"
            )
            placeholder = dict(final_msg) if isinstance(final_msg, dict) else {
                "role": "assistant",
            }
            placeholder["role"] = "assistant"
            placeholder["content"] = _placeholder_content
            placeholder["_fabricated_result_nudge"] = True
            for _key in (
                "reasoning",
                "reasoning_content",
                "reasoning_details",
                "codex_reasoning_items",
                "codex_message_items",
                "anthropic_content_blocks",
                "bedrock_content_blocks",
                "tool_calls",
            ):
                placeholder.pop(_key, None)
            append_message(messages, placeholder)
            append_message(messages, {
                "role": "user",
                "content": _nudge_content,
                "_fabricated_result_nudge": True,
            })
            agent._session_messages = messages
            final_response = None
            return _verdict("continue")

    # Genuine turn end (no dropped-tool-call mismatch): clear stall budget.
    agent._dropped_toolcall_retries = 0
    agent._fabricated_result_nudges = 0
"""

OLD_PERSIST = """    "_dropped_toolcall_nudge",  # internal retry instruction; must not replay as user context
)
"""

NEW_PERSIST = """    "_dropped_toolcall_nudge",  # internal retry instruction; must not replay as user context
    "_fabricated_result_nudge",  # assistant-stack: claimed result with no tool call
)
"""

OLD_COMPRESS = """        from agent.conversation_loop import (
            _CODEX_ACK_CONTINUATION_NUDGE, _CODEX_INCOMPLETE_NUDGE, _DROPPED_TOOLCALL_NUDGE_CONTENT,
            _EMPTY_TOOL_RESPONSE_NUDGE, _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX, _LENGTH_CONTINUATION_NETWORK_STUB,
            _LENGTH_CONTINUATION_OUTPUT_LIMIT,
        )
        return text in {
            COMPRESSION_CONTINUATION_USER_CONTENT, _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
            MAX_ITERATIONS_SUMMARY_REQUEST, _CODEX_INCOMPLETE_NUDGE, _CODEX_ACK_CONTINUATION_NUDGE,
            _DROPPED_TOOLCALL_NUDGE_CONTENT, _EMPTY_TOOL_RESPONSE_NUDGE, _LENGTH_CONTINUATION_NETWORK_STUB,
            _LENGTH_CONTINUATION_OUTPUT_LIMIT,
        } or text.startswith((
"""

NEW_COMPRESS = """        from agent.conversation_loop import (
            _CODEX_ACK_CONTINUATION_NUDGE, _CODEX_INCOMPLETE_NUDGE, _DROPPED_TOOLCALL_NUDGE_CONTENT,
            _EMPTY_TOOL_RESPONSE_NUDGE, _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX, _LENGTH_CONTINUATION_NETWORK_STUB,
            _LENGTH_CONTINUATION_OUTPUT_LIMIT,
        )
        from agent.fabricated_result import FABRICATED_RESULT_NUDGE, SECRET_REQUEST_NUDGE
        return text in {
            COMPRESSION_CONTINUATION_USER_CONTENT, _LEGACY_COMPRESSION_CONTINUATION_USER_CONTENT,
            MAX_ITERATIONS_SUMMARY_REQUEST, _CODEX_INCOMPLETE_NUDGE, _CODEX_ACK_CONTINUATION_NUDGE,
            _DROPPED_TOOLCALL_NUDGE_CONTENT, _EMPTY_TOOL_RESPONSE_NUDGE, _LENGTH_CONTINUATION_NETWORK_STUB,
            _LENGTH_CONTINUATION_OUTPUT_LIMIT,
            FABRICATED_RESULT_NUDGE, SECRET_REQUEST_NUDGE,
        } or text.startswith((
"""


def main() -> None:
    if not HELPER_SRC.is_file():
        raise SystemExit(f"[patch-fabricated-result] missing {HELPER_SRC}")
    HELPER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"[patch-fabricated-result] installed {HELPER_DST}")

    turn = TURN_FINAL.read_text()
    persist = PERSIST.read_text()
    compress = COMPRESSOR.read_text()
    turn_done = MARKER in turn
    persist_done = "_fabricated_result_nudge" in persist
    compress_done = "FABRICATED_RESULT_NUDGE" in compress
    if turn_done and persist_done and compress_done:
        print(f"[patch-fabricated-result] already applied ({TURN_FINAL})")
        return

    missing = []
    if not turn_done:
        if OLD_FLAGS not in turn:
            missing.append(f"ephemeral flags in {TURN_FINAL}")
        if OLD_GENUINE not in turn:
            missing.append(f"genuine-turn-end in {TURN_FINAL}")
    if not persist_done and OLD_PERSIST not in persist:
        missing.append(f"ephemeral flags in {PERSIST}")
    if not compress_done and OLD_COMPRESS not in compress:
        missing.append(f"synthetic-nudge set in {COMPRESSOR}")
    if missing:
        raise SystemExit(
            "[patch-fabricated-result] expected block missing: "
            + "; ".join(missing)
            + ". Hermes image may have changed — update this patch."
        )

    if not turn_done:
        TURN_FINAL.write_text(
            turn.replace(OLD_FLAGS, NEW_FLAGS, 1).replace(OLD_GENUINE, NEW_GENUINE, 1)
        )
        print(f"[patch-fabricated-result] patched {TURN_FINAL}")
    if not persist_done:
        PERSIST.write_text(persist.replace(OLD_PERSIST, NEW_PERSIST, 1))
        print(f"[patch-fabricated-result] patched {PERSIST}")
    if not compress_done:
        COMPRESSOR.write_text(compress.replace(OLD_COMPRESS, NEW_COMPRESS, 1))
        print(f"[patch-fabricated-result] patched {COMPRESSOR}")


if __name__ == "__main__":
    main()
