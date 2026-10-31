#!/usr/bin/env python3
"""Checks that tool-loop notes stay outside the untrusted wrapper and that
bootstrap applies the hard stop to every profile.

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the overlay is applied inside
nousresearch/hermes-agent and the guardrail runs on an alternating MCP loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import subprocess
import sys

out = subprocess.run([sys.executable, "/bootstrap/patch-tool-loop-notes.py"], capture_output=True, text=True)
assert "patched /opt/hermes/run_agent.py" in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-tool-loop-notes.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

sys.path.insert(0, "/opt/hermes")
from run_agent import AIAgent
from agent.plugin_live_notes import attach_to_tool_message
from agent.tool_dispatch_helpers import make_tool_result_message
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

TOOL = "mcp__todos__tasks_list"
CLOSE = "</untrusted_tool_result>"

agent = AIAgent.__new__(AIAgent)
config = ToolCallGuardrailConfig.from_mapping({"hard_stop_enabled": True}, platform="api_server")
assert config.hard_stop_enabled
agent._tool_guardrails = ToolCallGuardrailController(config)
agent._tool_guardrail_halt_decision = None

noted = 0
halted_at = None
for i in range(1, 21):
    task = 7 if i % 2 else 15
    raw = f"synthetic task {task} " * 5
    result = agent._append_guardrail_observation(
        TOOL, {"filter": f"id = {task}"}, raw, failed=False, tool_call_id=f"call_{i}",
    )
    assert result == raw, f"note leaked into the raw result on call {i}"
    message = make_tool_result_message(TOOL, result, f"call_{i}")
    attach_to_tool_message(agent, message)
    head, tail = message["content"].rsplit(CLOSE, 1)
    assert "hermes note" not in head and "Tool loop" not in head, f"note inside wrapper on call {i}"
    if "hermes note" in tail or "Tool loop" in tail:
        noted += 1
    if "Tool loop hard stop" in tail:
        halted_at = i
        break

assert noted, "no loop note was attached"
assert halted_at == 10, f"expected the halt on call 10, got {halted_at}"
print("image ok")
'''


def test_bootstrap_covers_every_profile() -> None:
    script = (ROOT / "compose" / "hermes" / "bootstrap-api-profile.sh").read_text()
    assert 'hermes_config_set "$_ths_profile" tool_loop_guardrails.hard_stop_enabled' in script
    assert "\napply_tool_loop_hard_stop_all_profiles\n" in script
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("HERMES_TOOL_LOOP_HARD_STOP=${HERMES_TOOL_LOOP_HARD_STOP:-true}") == 2


def test_overlay_is_wired() -> None:
    assert "run_patch /bootstrap/patch-tool-loop-notes.py" in (
        ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh"
    ).read_text()
    assert "/bootstrap/patch-tool-loop-notes.py:ro" in (ROOT / "docker-compose.yml").read_text()


def main() -> None:
    test_bootstrap_covers_every_profile()
    test_overlay_is_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
