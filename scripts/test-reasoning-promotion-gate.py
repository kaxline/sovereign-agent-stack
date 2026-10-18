#!/usr/bin/env python3
"""Check that Hermes still keeps a reasoning-only reply out of the answer on this stack's route.

v2026.9 promoted a reasoning-only stop to the final answer, so a reply whose
reasoning ended in a malformed tool call (`</parameter></function></tool_call>`)
was shown to the user. The stack patched that until v0.22, which promotes only
when answer_in_reasoning_capability() trusts the route. This stack's route
(custom provider behind llm-proxy, no answer_in_reasoning) is not trusted, so the
empty-response ladder re-prompts instead. Run with --image on every Hermes bump.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(HERMES_DIR))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

# Reasoning from the LinkedIn login session (20261007_030737_5f92c8): the
# model planned a browser_exec call, and only the closing tags survived.
SESSION_REASONING = """Keith asked me to login to LinkedIn and read his home feed. I tried to use browser tools but ran into issues.

But I already called browser_vault_list() and it returned no items. So I need to navigate to LinkedIn's login page first, then save the login.

Let me try navigating to LinkedIn using the browser_exec tool properly.
</parameter>
</function>
</tool_call>
"""

# Fails at import if upstream drops the route gate.
IMAGE_DRIVER = r'''
import subprocess
import sys

# Fabricated-result patches the same turn-end block; the gate must survive it.
out = subprocess.run([sys.executable, "/bootstrap/patch-fabricated-result.py"], capture_output=True, text=True)
if out.returncode != 0:
    raise SystemExit(f"FAIL patch-fabricated-result: {out.stdout}{out.stderr}")
sys.path.insert(0, "/opt/hermes")
from agent.reasoning_promotion import answer_in_reasoning_capability  # noqa: F401
'''

# Runs after TURN_DRIVER_PRELUDE, so it sees the patched modules.
IMAGE_CHECKS = r'''
REASONING = sys.stdin.read()

# This stack's route: custom provider behind llm-proxy, no answer_in_reasoning.
_bare_agent = make_agent
def make_agent(holds, model="qwen/qwen3.6-35b-a3b"):
    agent = _bare_agent(holds)
    agent.base_url = "http://llm-proxy:4000/v1"
    agent.requested_provider = "custom"
    agent._custom_providers = None
    agent.model = model
    return agent

# 1. The session reply: not promoted, the empty-response ladder re-prompts.
agent, msg, messages, verdict, err = drive(0, REASONING)
assert err is None, f"ladder path raised {err!r}"
assert verdict.action == "continue", verdict.action
assert verdict.final_response in (None, ""), verdict.final_response
assert not any("Reasoning-only clean stop" in r for r in records), records
assert messages[-1]["role"] == "user" and messages[-1].get("_empty_recovery_synthetic"), messages[-1]

# 2. Plain reasoning is not promoted on this route either.
agent, msg, messages, verdict, err = drive(0, "Your feed has three new posts.")
assert err is None, repr(err)
assert verdict.action == "continue", verdict.action
assert not any("Reasoning-only clean stop" in r for r in records), records

# 3. Control: a trusted route still promotes, so 1 and 2 show the gate at work.
_qwen_agent = make_agent
make_agent = lambda holds: _qwen_agent(holds, model="nvidia/nemotron-3.5-lightning")
drive(0, "Your feed has three new posts.")
assert any("Reasoning-only clean stop" in r for r in records), records
print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(
        IMAGE_DRIVER + hermes_image.TURN_DRIVER_PRELUDE + IMAGE_CHECKS, stdin=SESSION_REASONING
    )

if __name__ == "__main__":
    main()
