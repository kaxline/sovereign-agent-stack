#!/usr/bin/env python3
"""Relax intent-ack detection for interactive WebUI / LM Studio sessions.

Upstream looks_like_codex_intermediate_ack blocks continuation when the assistant
reply exceeds 1200 chars, and Qwen often narrates intent plus a partial answer in
one long stop response. (Before v0.23 it also blocked on any earlier tool message
in the transcript; v0.23 limits that check to this turn, #69778.)

Applied idempotently on hermes container start when browser bootstrap enables
agent.intent_ack_continuation=true (require_workspace=False path).
"""
from __future__ import annotations

from overlay_patch import ROOT, edit

TARGET = ROOT / "agent" / "agent_runtime_helpers.py"
TAG = "[patch-intent-ack]"
MARKER = "# assistant-stack: intent-ack relaxed for require_workspace=False"

# The longer limit for a reply that opens with an ack.
OLD_V23 = """    assistant_text = agent._strip_think_blocks(assistant_content or "").strip().lower()
    if not assistant_text or len(assistant_text) > 1200:
        return False"""

NEW_V23 = """    assistant_text = agent._strip_think_blocks(assistant_content or "").strip().lower()
    if not assistant_text:
        return False
    # assistant-stack: intent-ack relaxed for require_workspace=False
    # Qwen often bundles "Let me search…" with a partial answer in one reply.
    max_ack_chars = 1200
    if not require_workspace and _ACK_FUTURE_RE.search(assistant_text[:500]):
        max_ack_chars = 4000
    if len(assistant_text) > max_ack_chars:
        return False"""


def main() -> None:
    edit(TARGET, [(OLD_V23, NEW_V23)], MARKER, TAG, requires=["reanchor_current_turn_user_idx", "_ACK_FUTURE_RE = "])


if __name__ == "__main__":
    main()
