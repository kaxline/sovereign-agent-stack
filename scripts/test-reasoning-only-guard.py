#!/usr/bin/env python3
"""Checks for the reasoning-only tool-markup hold.

Host part is stdlib-only (no Hermes import). With --image (or when Docker and
the pinned Hermes image are present and --host-only is not given), the patch
is also applied inside nousresearch/hermes-agent and finish_text_response is
driven with the reply from session 20261007_030737_5f92c8.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(HERMES_DIR))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
import reasoning_only_guard as rg  # noqa: E402

# Reasoning from the LinkedIn login session (20261007_030737_5f92c8): the
# model planned a browser_exec call, and only the closing tags survived.
SESSION_REASONING = """Keith asked me to login to LinkedIn and read his home feed. I tried to use browser tools but ran into issues.

But I already called browser_vault_list() and it returned no items. So I need to navigate to LinkedIn's login page first, then save the login.

Let me try navigating to LinkedIn using the browser_exec tool properly.
</parameter>
</function>
</tool_call>
"""

PLAIN_ANSWER = (
    "Your LinkedIn feed has three new posts: a job change, a repost about "
    "hiring, and an article on remote work."
)


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_session_closing_tags() -> None:
    expect(rg.has_tool_call_markup(SESSION_REASONING), "session closing tags should match")


def test_full_call() -> None:
    text = (
        "Navigating now.\n<tool_call>\n<function=browser_exec>\n"
        "<parameter=code>\nnew_tab('https://www.linkedin.com')\n"
    )
    expect(rg.has_tool_call_markup(text), "unterminated opening tags should match")


def test_plain_answer() -> None:
    expect(not rg.has_tool_call_markup(PLAIN_ANSWER), "a plain answer should not match")


def test_prose_mentions() -> None:
    for text in (
        "The tool_call failed, so I used browser_exec instead.",
        "Call the function with one parameter, the URL.",
        "Use <b>bold</b> for the heading.",
    ):
        expect(not rg.has_tool_call_markup(text), f"prose should not match: {text!r}")


def test_empty() -> None:
    expect(not rg.has_tool_call_markup(""), "empty text should not match")
    expect(not rg.has_tool_call_markup("   "), "blank text should not match")
    expect(not rg.has_tool_call_markup(None), "None should not match")  # type: ignore[arg-type]


def test_hold_cap() -> None:
    expect(rg.should_hold_promotion(SESSION_REASONING, holds=0), "first hold")
    expect(rg.should_hold_promotion(SESSION_REASONING, holds=rg.MAX_HOLDS - 1), "last hold")
    expect(
        not rg.should_hold_promotion(SESSION_REASONING, holds=rg.MAX_HOLDS),
        "past the cap, upstream promotion runs",
    )
    expect(not rg.should_hold_promotion(PLAIN_ANSWER, holds=0), "no markup, no hold")


# Runs inside the Hermes image with compose/hermes mounted at /bootstrap.
IMAGE_DRIVER = r'''
import subprocess
import sys

def run(script):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
    return out.stdout

# Fabricated-result patches the same turn-end block; both must apply in boot order.
run("/bootstrap/patch-fabricated-result.py")
first = run("/bootstrap/patch-reasoning-only-tool-markup.py")
assert "patched /opt/hermes/agent/turn_final_response.py" in first, first
assert "patched /opt/hermes/agent/turn_tool_round.py" in first, first
again = run("/bootstrap/patch-reasoning-only-tool-markup.py")
assert "already applied" in again, again

import py_compile
for f in ("turn_final_response.py", "turn_tool_round.py"):
    py_compile.compile(f"/opt/hermes/agent/{f}", doraise=True)
src = open("/opt/hermes/agent/turn_final_response.py").read()
assert "assistant-stack: fabricated result without a tool call" in src
assert src.count("agent._reasoning_markup_holds = 0") == 1, "genuine-end reset"

'''

# Runs after TURN_DRIVER_PRELUDE, so it sees the patched modules.
IMAGE_CHECKS = r'''
REASONING = sys.stdin.read()

# 1. The session reply: held, content stays empty, ladder re-prompts.
agent, msg, messages, verdict, err = drive(0, REASONING)
assert err is None, f"held path raised {err!r}"
assert verdict.action == "continue", verdict.action
assert msg.content == "", "reasoning must not be promoted"
assert agent._reasoning_markup_holds == 1
assert any("re-prompting instead of promoting (1/2)" in r for r in records), records
assert not any("Reasoning-only clean stop" in r for r in records), records
assert messages[-1]["role"] == "user" and messages[-1].get("_empty_recovery_synthetic"), messages[-1]

# 2. Past the cap: upstream promotion runs.
agent, msg, *_ = drive(2, REASONING)
assert msg.content == REASONING, "past the cap the reasoning is promoted"
assert any("Reasoning-only clean stop" in r for r in records), records

# 3. No markup: upstream promotion unchanged.
plain = "Your feed has three new posts."
agent, msg, *_ = drive(0, plain)
assert msg.content == plain, "plain reasoning is still promoted"
assert agent._reasoning_markup_holds == 0
print("image ok")
'''


def main() -> None:
    test_session_closing_tags()
    test_full_call()
    test_plain_answer()
    test_prose_mentions()
    test_empty()
    test_hold_cap()
    print("ok")

    hermes_image.maybe_run(
        IMAGE_DRIVER + hermes_image.TURN_DRIVER_PRELUDE + IMAGE_CHECKS, stdin=SESSION_REASONING
    )

if __name__ == "__main__":
    main()
