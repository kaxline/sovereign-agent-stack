#!/usr/bin/env python3
"""Checks the intent-ack overlay (patch-intent-ack-continuation.py).

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the overlay is applied inside
nousresearch/hermes-agent and looks_like_codex_intermediate_ack is called with
require_workspace=False (the browser profile's setting):

- a reply that opens with an ack ("Let me search ...") is still an ack up to
  4000 characters, and not past that; with require_workspace=True the limit
  stays 1200;
- a tool result from an earlier turn does not block it, one from this turn does.
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

out = subprocess.run([sys.executable, "/bootstrap/patch-intent-ack-continuation.py"], capture_output=True, text=True)
assert out.returncode == 0 and "patched" in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-intent-ack-continuation.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

sys.path.insert(0, "/opt/hermes")
from agent.agent_runtime_helpers import looks_like_codex_intermediate_ack as ack


class Agent:
    @staticmethod
    def _strip_think_blocks(text):
        return text


ACK = "Let me search the docs for the release dates and check the changelog. "
SHORT = ACK + "One moment."
padded = lambda n: ACK + "x" * (n - len(ACK))
question = "when did it ship?"
earlier = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "", "tool_calls": []},
           {"role": "tool", "content": "result"}, {"role": "assistant", "content": "done"},
           {"role": "user", "content": question}]


def check(content, messages, require_workspace=False):
    return ack(Agent(), question, content, messages, require_workspace=require_workspace)


assert check(SHORT, earlier), "an earlier turn's tool result does not block the ack"
assert check(padded(3000), earlier), "an opening ack keeps up to 4000 characters"
assert not check(padded(4100), earlier), "past 4000 characters it is a final answer"
assert not check("Here is the answer. " + "y" * 1500, earlier), "no opening ack: 1200 still applies"
this_turn = earlier + [{"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "r"}]
assert not check(SHORT, this_turn), "a tool result in this turn blocks it"
assert not ack(Agent(), question, padded(3000), earlier, require_workspace=True), \
    "require_workspace=True keeps upstream's 1200"
print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
