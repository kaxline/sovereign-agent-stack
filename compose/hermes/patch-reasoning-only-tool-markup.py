#!/usr/bin/env python3
"""Don't promote a reasoning-only reply that holds a malformed tool call.

Compatibility overlay (not for Hermes upstream). Hermes v2026.9+ turns a
"stop" reply with reasoning, no content and no tool_calls into the final
answer before the empty-response ladder runs. v2026.8 sent that reply to the
ladder, which re-prompted. When a local model writes its tool call inside the
reasoning (only the closing tags survive), the turn now ends on its planning
text.

Copies reasoning_only_guard.py into /opt/hermes/agent/ and:

- skips the promotion in turn_final_response.py when the reasoning has
  tool-call markup (max 2 per turn), so the ladder re-prompts
- resets that budget at a genuine turn end and when a tool call lands
  (turn_tool_round.py)
"""
from __future__ import annotations

import shutil
from pathlib import Path

HELPER_SRC = Path("/bootstrap/reasoning_only_guard.py")
HELPER_DST = Path("/opt/hermes/agent/reasoning_only_guard.py")
TURN_FINAL = Path("/opt/hermes/agent/turn_final_response.py")
TOOL_ROUND = Path("/opt/hermes/agent/turn_tool_round.py")
MARKER = "# assistant-stack: hold reasoning-only promotion on tool-call markup"
MARKER_RESET = "# assistant-stack: reset reasoning-markup holds"

OLD_PROMOTE = """        _promoted = agent._extract_reasoning(assistant_message)
        if _promoted:
            logger.info(
                "Reasoning-only clean stop (%d chars) — using reasoning as the final response",
                len(_promoted),
            )
            assistant_message.content = _promoted
"""

NEW_PROMOTE = """        _promoted = agent._extract_reasoning(assistant_message)
        # assistant-stack: hold reasoning-only promotion on tool-call markup
        # A model that writes its tool call inside the reasoning (often only
        # the closing </parameter></function></tool_call> survive) would end
        # the turn on its planning text. Leave content empty so the
        # empty-response ladder re-prompts, as v2026.8 did. Capped per turn.
        _hold_promotion = False
        if _promoted:
            try:
                from agent.reasoning_only_guard import MAX_HOLDS, should_hold_promotion

                _hold_promotion = should_hold_promotion(
                    _promoted, holds=getattr(agent, "_reasoning_markup_holds", 0)
                )
            except Exception:
                logger.debug("reasoning-only markup check failed", exc_info=True)
        if _hold_promotion:
            agent._reasoning_markup_holds = getattr(agent, "_reasoning_markup_holds", 0) + 1
            logger.info(
                "Reasoning-only stop with tool-call markup (%d chars) — "
                "re-prompting instead of promoting (%d/%d)",
                len(_promoted), agent._reasoning_markup_holds, MAX_HOLDS,
            )
        elif _promoted:
            logger.info(
                "Reasoning-only clean stop (%d chars) — using reasoning as the final response",
                len(_promoted),
            )
            assistant_message.content = _promoted
"""

OLD_GENUINE = """    # Genuine turn end (no dropped-tool-call mismatch): clear stall budget.
    agent._dropped_toolcall_retries = 0
"""

NEW_GENUINE = """    # Genuine turn end (no dropped-tool-call mismatch): clear stall budget.
    agent._dropped_toolcall_retries = 0
    agent._reasoning_markup_holds = 0  # assistant-stack: reset reasoning-markup holds
"""

OLD_TOOL_ROUND = """    agent._post_tool_empty_retried = False
    agent._dropped_toolcall_retries = 0
"""

NEW_TOOL_ROUND = """    agent._post_tool_empty_retried = False
    agent._dropped_toolcall_retries = 0
    agent._reasoning_markup_holds = 0  # assistant-stack: reset reasoning-markup holds
"""


def main() -> None:
    if not HELPER_SRC.is_file():
        raise SystemExit(f"[patch-reasoning-only-tool-markup] missing {HELPER_SRC}")
    HELPER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"[patch-reasoning-only-tool-markup] installed {HELPER_DST}")

    turn = TURN_FINAL.read_text()
    tool_round = TOOL_ROUND.read_text()
    turn_done = MARKER in turn
    round_done = MARKER_RESET in tool_round
    if turn_done and round_done:
        print(f"[patch-reasoning-only-tool-markup] already applied ({TURN_FINAL})")
        return

    missing = []
    if not turn_done:
        if OLD_PROMOTE not in turn:
            missing.append(f"reasoning-only promotion in {TURN_FINAL}")
        if OLD_GENUINE not in turn:
            missing.append(f"genuine-turn-end in {TURN_FINAL}")
    if not round_done and OLD_TOOL_ROUND not in tool_round:
        missing.append(f"tool-round budget reset in {TOOL_ROUND}")
    if missing:
        raise SystemExit(
            "[patch-reasoning-only-tool-markup] expected block missing: "
            + "; ".join(missing)
            + ". Hermes image may have changed — update this patch."
        )

    if not turn_done:
        TURN_FINAL.write_text(
            turn.replace(OLD_PROMOTE, NEW_PROMOTE, 1).replace(OLD_GENUINE, NEW_GENUINE, 1)
        )
        print(f"[patch-reasoning-only-tool-markup] patched {TURN_FINAL}")
    if not round_done:
        TOOL_ROUND.write_text(tool_round.replace(OLD_TOOL_ROUND, NEW_TOOL_ROUND, 1))
        print(f"[patch-reasoning-only-tool-markup] patched {TOOL_ROUND}")


if __name__ == "__main__":
    main()
