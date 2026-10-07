#!/usr/bin/env python3
"""Checks for the reasoning-only tool-markup hold.

Host part is stdlib-only (no Hermes import). With --image (or when Docker and
the pinned Hermes image are present and --host-only is not given), the patch
is also applied inside nousresearch/hermes-agent and finish_text_response is
driven with the reply from session 20261007_030737_5f92c8.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(HERMES_DIR))

import reasoning_only_guard as rg  # noqa: E402

DEFAULT_TAG = "v2026.9.14"

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
import logging
import subprocess
import sys
from types import SimpleNamespace

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

sys.path.insert(0, "/opt/hermes")
import py_compile
for f in ("turn_final_response.py", "turn_tool_round.py"):
    py_compile.compile(f"/opt/hermes/agent/{f}", doraise=True)
src = open("/opt/hermes/agent/turn_final_response.py").read()
assert "assistant-stack: fabricated result without a tool call" in src
assert src.count("agent._reasoning_markup_holds = 0") == 1, "genuine-end reset"

from run_agent import AIAgent
from agent.turn_final_response import finish_text_response

records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.INFO)

REASONING = sys.stdin.read()

def make_agent(holds):
    agent = object.__new__(AIAgent)
    defaults = dict(
        _reasoning_markup_holds=holds, _current_streamed_assistant_text="",
        _last_content_with_tools=None, _last_content_tools_all_housekeeping=False,
        _post_tool_empty_retried=False, _thinking_prefill_retries=0,
        _empty_content_retries=0, _mute_post_response=False, _fallback_chain=[],
        _stall_guards=True, valid_tool_names={"browser_exec"}, model="qwen/qwen3.6-35b-a3b",
        provider="custom", api_mode="chat_completions", _intent_ack_continuation="off",
        _session_messages=[], _status_buffer=[], verbose_logging=False, quiet_mode=True,
        reasoning_callback=None, status_callback=None, _fabricated_result_nudges=0,
        _dropped_toolcall_retries=0,
    )
    for k, v in defaults.items():
        setattr(agent, k, v)
    agent._buffer_status = lambda *a, **k: None
    agent._emit_status = lambda *a, **k: None
    return agent

def drive(holds, reasoning):
    agent = make_agent(holds)
    msg = SimpleNamespace(
        content="", tool_calls=None, reasoning=reasoning, reasoning_content=reasoning,
        reasoning_details=None, codex_reasoning_items=None, codex_message_items=None,
        finish_reason="stop",
    )
    messages = [
        {"role": "user", "content": "Can you login to LinkedIn for me and read my home feed?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
            "function": {"name": "browser_exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "{\"success\": true}"},
    ]
    records.clear()
    verdict = err = None
    try:
        verdict = finish_text_response(
            agent, assistant_message=msg, response=None, finish_reason="stop",
            messages=messages, api_messages=list(messages), conversation_history=[],
            api_call_count=6, user_message=messages[0]["content"], active_system_prompt="",
            final_response="", _turn_exit_reason=None, _preflight_compression_blocked=False,
            codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None, _pending_verification_response_previewed=False,
        )
    except Exception as exc:  # later stop gates need more agent; promotion already ran
        err = exc
    return agent, msg, messages, verdict, err

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


def run_image_checks(tag: str) -> None:
    image = f"nousresearch/hermes-agent:{tag}"
    cmd = [
        "docker", "run", "--rm", "-i", "--entrypoint", "/opt/hermes/.venv/bin/python",
        "-v", f"{HERMES_DIR}:/bootstrap:ro", image, "-c", IMAGE_DRIVER,
    ]
    out = subprocess.run(cmd, input=SESSION_REASONING, capture_output=True, text=True)
    if out.returncode != 0 or "image ok" not in out.stdout:
        fail(f"image checks ({image}):\n{out.stdout}{out.stderr}")
    print(f"image ok ({image})")


def image_available(tag: str) -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", f"nousresearch/hermes-agent:{tag}"],
        capture_output=True,
    )
    return probe.returncode == 0


def main() -> None:
    test_session_closing_tags()
    test_full_call()
    test_plain_answer()
    test_prose_mentions()
    test_empty()
    test_hold_cap()
    print("ok")

    args = sys.argv[1:]
    if "--host-only" in args:
        return
    tag = os.environ.get("HERMES_AGENT_IMAGE_TAG", DEFAULT_TAG)
    if "--image" in args or image_available(tag):
        run_image_checks(tag)
    else:
        print(f"skip image checks (nousresearch/hermes-agent:{tag} not present)")


if __name__ == "__main__":
    main()
