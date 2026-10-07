"""Run a Python driver inside the pinned Hermes image for overlay tests.

compose/hermes is mounted at /bootstrap, as in the real container, so a
driver can run the patch scripts and then import the patched Hermes code.
Tests call ``maybe_run`` after their host checks: it skips cleanly when
Docker or the image is missing, unless ``--image`` forces the check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERMES_DIR = ROOT / "compose" / "hermes"
DEFAULT_TAG = "v2026.9.14"


# Prelude for drivers that call finish_text_response with a bare AIAgent.
# Put it AFTER the driver runs the patch scripts: it imports the patched modules.
# Defines ``records`` (captured log lines), ``make_agent(holds)`` and
# ``drive(holds, reasoning, content="")`` -> (agent, msg, messages, verdict, err).
TURN_DRIVER_PRELUDE = r'''
import logging
import sys
from types import SimpleNamespace

sys.path.insert(0, "/opt/hermes")
from run_agent import AIAgent
from agent.turn_final_response import finish_text_response

records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.INFO)

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

def drive(holds, reasoning, content=""):
    agent = make_agent(holds)
    msg = SimpleNamespace(
        content=content, tool_calls=None, reasoning=reasoning, reasoning_content=reasoning,
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
            final_response=content, _turn_exit_reason=None, _preflight_compression_blocked=False,
            codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
            _pending_verification_response=None, _pending_verification_response_previewed=False,
        )
    except Exception as exc:  # later stop gates need more agent; promotion already ran
        err = exc
    return agent, msg, messages, verdict, err

'''


def image_tag() -> str:
    return os.environ.get("HERMES_AGENT_IMAGE_TAG", DEFAULT_TAG)


def image_available(tag: str) -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", f"nousresearch/hermes-agent:{tag}"],
        capture_output=True,
    )
    return probe.returncode == 0


def run_driver(driver: str, stdin: str = "", tag: str = "") -> None:
    """Run ``driver``; it must print "image ok" on success."""
    image = f"nousresearch/hermes-agent:{tag or image_tag()}"
    cmd = [
        "docker", "run", "--rm", "-i", "--entrypoint", "/opt/hermes/.venv/bin/python",
        "-v", f"{HERMES_DIR}:/bootstrap:ro", image, "-c", driver,
    ]
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if out.returncode != 0 or "image ok" not in out.stdout:
        print(f"FAIL image checks ({image}):\n{out.stdout}{out.stderr}", file=sys.stderr)
        raise SystemExit(1)
    print(f"image ok ({image})")


def maybe_run(driver: str, stdin: str = "", argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--host-only" in args:
        return
    tag = image_tag()
    if "--image" in args or image_available(tag):
        run_driver(driver, stdin, tag)
    else:
        print(f"skip image checks (nousresearch/hermes-agent:{tag} not present)")
