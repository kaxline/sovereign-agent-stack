#!/usr/bin/env python3
"""Relax intent-ack detection for interactive WebUI / LM Studio sessions.

Upstream looks_like_codex_intermediate_ack blocks continuation when (a) any prior
tool message exists in the transcript or (b) the assistant reply exceeds 1200
chars. Multi-turn research chats hit both: turn 3+ already has tool history, and
Qwen often narrates intent plus a partial answer in one long stop response.

Applied idempotently on hermes container start when browser bootstrap enables
agent.intent_ack_continuation=true (require_workspace=False path).
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/agent/agent_runtime_helpers.py")
MARKER = "# assistant-stack: intent-ack relaxed for require_workspace=False"

OLD = """    if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
        return False

    assistant_text = agent._strip_think_blocks(assistant_content or "").strip().lower()
    if not assistant_text:
        return False
    if len(assistant_text) > 1200:
        return False"""

NEW = """    assistant_text = agent._strip_think_blocks(assistant_content or "").strip().lower()
    if not assistant_text:
        return False

    # assistant-stack: intent-ack relaxed for require_workspace=False
    # Upstream skips any transcript that already contains tool results, which
    # makes intent-ack useless after turn 1 in tool-heavy WebUI sessions.
    # When the user opted in (require_workspace=False), only block if THIS turn
    # already executed tools since the latest user message.
    if require_workspace:
        if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
            return False
        max_ack_chars = 1200
    else:
        last_user_idx = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                last_user_idx = idx
        turn_messages = messages[last_user_idx + 1 :] if last_user_idx >= 0 else messages
        if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in turn_messages):
            return False
        # Qwen often bundles "Let me search…" with a partial answer in one reply.
        head = assistant_text[:500]
        has_early_ack = bool(
            re.search(
                r"\\b(i['']ll|i will|let me|i can do that|i can help with that)\\b",
                head,
            )
        )
        max_ack_chars = 4000 if has_early_ack else 1200

    if len(assistant_text) > max_ack_chars:
        return False"""


def main() -> None:
    text = TARGET.read_text()
    if MARKER in text:
        print(f"[patch-intent-ack] already applied ({TARGET})")
        return
    if OLD not in text:
        raise SystemExit(
            f"[patch-intent-ack] expected block missing in {TARGET}; "
            "Hermes image may have changed — update this patch."
        )
    TARGET.write_text(text.replace(OLD, NEW, 1))
    print(f"[patch-intent-ack] patched {TARGET}")


if __name__ == "__main__":
    main()
