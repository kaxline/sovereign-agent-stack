#!/usr/bin/env python3
"""Checks the stale-replay loop guard (patch-tool-loop-replay.py).

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the overlay is applied inside
nousresearch/hermes-agent and the guard runs on a 9-call browser_exec rotation
like the 2026-11-05 Boundary loop, whose pages returned slightly different
text on some laps.
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

for script in ("/bootstrap/patch-tool-loop-notes.py", "/bootstrap/patch-tool-loop-replay.py"):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    assert "patched" in out.stdout and "not patched" not in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-tool-loop-replay.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

sys.path.insert(0, "/opt/hermes")
from run_agent import AIAgent
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
from hermes_cli.observability.shared_metrics_harness import _GUARD_DETECTORS

assert _GUARD_DETECTORS["stale_replay_halt"] == "stale_replay"

TOOL = "browser_exec"
SITES = ["facebook", "instagram", "youtube", "wordpress", "tumblr", "yelp", "chamber", "brownbook", "yellowpages"]


def make_agent(hard_stop=True):
    agent = AIAgent.__new__(AIAgent)
    config = ToolCallGuardrailConfig.from_mapping({"hard_stop_enabled": hard_stop}, platform="api_server")
    agent._tool_guardrails = ToolCallGuardrailController(config)
    agent._tool_guardrail_halt_decision = None
    agent._stall_guards = True
    return agent


def run(agent, calls):
    """Feed (args, result) pairs; return (call number of first notice, call number of halt)."""
    from agent.plugin_live_notes import attach_to_tool_message
    from agent.tool_dispatch_helpers import make_tool_result_message
    noticed = halted = None
    for i, (args, raw) in enumerate(calls, 1):
        result = agent._append_guardrail_observation(TOOL, args, raw, failed=False, tool_call_id=f"call_{i}")
        message = make_tool_result_message(TOOL, result, f"call_{i}")
        attach_to_tool_message(agent, message)
        if noticed is None and "got back a result it had already seen" in message["content"]:
            noticed = i
        if "Tool loop hard stop: stale_replay_halt" in message["content"]:
            halted = i
            break
    return noticed, halted


def page(site, lap):
    # yelp and brownbook drift on alternate laps, so whole laps never match byte for byte.
    drift = lap % 2 if site in ("yelp", "brownbook") else 0
    return f"synthetic {site} page text v{drift} " * 40


# A 9-call rotation, longer than the upstream cycle detector's period limit of 4.
rotation = [({"code": f"open('{s}')"}, page(s, lap)) for lap in range(8) for s in SITES]
noticed, halted = run(make_agent(), rotation)
assert noticed is not None, "no stale-replay notice on a 9-call rotation"
assert halted is not None, "no stale-replay halt on a 9-call rotation"
# Lap 1 is new, laps 2 and 3 repeat seen results (notice), lap 4 halts on its first call.
assert noticed == 2 * 9 + 1, noticed
assert halted == 3 * 9 + 1, halted

# Without hard stops the notice still fires and nothing halts.
noticed, halted = run(make_agent(hard_stop=False), rotation)
assert noticed == 2 * 9 + 1 and halted is None, (noticed, halted)

# Re-reading the same page while it changes is progress, never flagged.
changing = [({"code": "print(page_info())"}, f"synthetic page state {i} " * 40) for i in range(30)]
assert run(make_agent(), changing) == (None, None)

# Pollers stay exempt.
agent = make_agent()
for i in range(10):
    obs = agent._tool_guardrails.observe_call("process_manage", {"action": "poll"}, ("same " * 200) if i % 2 else ("other " * 200))
    assert obs.kind != "stale_replay", i
assert agent._tool_guardrails.halt_decision is None

print("image ok")
'''


def test_overlay_is_wired() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    notes = runner.index("run_patch /bootstrap/patch-tool-loop-notes.py")
    replay = runner.index("run_patch /bootstrap/patch-tool-loop-replay.py")
    assert notes < replay, "replay overlay must run after the notes overlay"
    assert "/bootstrap/patch-tool-loop-replay.py:ro" in (ROOT / "docker-compose.yml").read_text()


def main() -> None:
    test_overlay_is_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
