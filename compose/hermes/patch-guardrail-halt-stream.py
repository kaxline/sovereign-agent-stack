#!/usr/bin/env python3
"""Stream the tool-guardrail stop note to gateway clients.

Compatibility overlay until upstream fixes it (draft:
docs/private/hermes-upstream/13-guardrail-halt-stream.md). When the tool-loop
guardrail halts a turn, agent/turn_tool_round.py streams the stop note only
through agent.stream_delta_callback. The dashboard gateway
(tui_gateway/prompt_turn.py) passes its message.delta emitter as
run_conversation's stream_callback, which becomes agent._stream_callback, and
never sets stream_delta_callback. So over the gateway the note arrived only as
message.complete.text, and a client that builds the reply from deltas showed an
empty turn (Boundary CR 0029, 2026-11-11).

The note now goes through agent._deliver_to_stream_callbacks, which every
other streamed text uses: it calls both callbacks and sends nothing while a
hold_stream plugin holds the turn. The end-of-segment flush (None) stays on
stream_delta_callback only, since _stream_callback (TTS) must not get None.

Identical on Hermes v0.22.7 and v0.23.1. Run scripts/test-guardrail-halt-stream.py
on Hermes bumps.
"""
from __future__ import annotations

from overlay_patch import ROOT, edit

TARGET = ROOT / "agent" / "turn_tool_round.py"
TAG = "[patch-guardrail-halt-stream]"
MARKER = "# assistant-stack: guardrail halt reaches every stream callback"

OLD = (
    "            if agent.stream_delta_callback and not getattr(agent, \"_hold_stream\", False):\n"
    "                with suppress(Exception):\n"
    "                    agent.stream_delta_callback(final_response)\n"
    "                    agent.stream_delta_callback(None)\n"
)
NEW = (
    f"            {MARKER}\n"
    "            # (the gateway's message.delta emitter is _stream_callback, not stream_delta_callback).\n"
    "            if not getattr(agent, \"_hold_stream\", False):\n"
    "                agent._deliver_to_stream_callbacks(final_response)\n"
    "                if agent.stream_delta_callback:\n"
    "                    with suppress(Exception):\n"
    "                        agent.stream_delta_callback(None)\n"
)


def main() -> None:
    edit(TARGET, [(OLD, NEW)], MARKER, TAG)


if __name__ == "__main__":
    main()
