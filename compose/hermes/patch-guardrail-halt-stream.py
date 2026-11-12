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

from pathlib import Path

TARGET = Path("/opt/hermes/agent/turn_tool_round.py")
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
    if not TARGET.is_file():
        print(f"[patch-guardrail-halt-stream] skip: {TARGET} missing")
        return
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print("[patch-guardrail-halt-stream] already applied")
        return
    if text.count(OLD) != 1:
        print("[patch-guardrail-halt-stream] anchor missing in turn_tool_round.py, not patched")
        return
    TARGET.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print(f"[patch-guardrail-halt-stream] patched {TARGET}")


if __name__ == "__main__":
    main()
