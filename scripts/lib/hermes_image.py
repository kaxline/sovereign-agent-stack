"""Run a Python driver inside the pinned Hermes image for overlay tests.

compose/hermes is mounted at /bootstrap, as in the real container, so a
driver can run the patch scripts and then import the patched Hermes code.
Tests call ``maybe_run`` after their host checks: it skips when Docker or the
image is missing, unless ``--image`` or HERMES_IMAGE_REQUIRED=1 makes that a
failure. scripts/lib/hermes-image.sh is the same gate for shell tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERMES_DIR = ROOT / "compose" / "hermes"


def compose_default_tag() -> str:
    """The HERMES_AGENT_IMAGE_TAG default in docker-compose.yml, the one pin.

    Read on call, not at import: drivers import this module inside the image,
    where only scripts/ is mounted.
    """
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(r"hermes-agent:\$\{HERMES_AGENT_IMAGE_TAG:-([^}]+)\}", text)
    if not m:
        raise SystemExit("FAIL docker-compose.yml has no hermes-agent:${HERMES_AGENT_IMAGE_TAG:-...} pin")
    return m.group(1)



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

# Hermes v0.22 added required keyword-only arguments to finish_text_response.
import inspect as _inspect
_DRIVE_EXTRA_KWARGS = {}
if "effective_task_id" in _inspect.signature(finish_text_response).parameters:
    _DRIVE_EXTRA_KWARGS["effective_task_id"] = "turn-driver"

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
            **_DRIVE_EXTRA_KWARGS,
        )
    except Exception as exc:  # later stop gates need more agent; promotion already ran
        err = exc
    return agent, msg, messages, verdict, err

'''


def image_tag() -> str:
    return os.environ.get("HERMES_AGENT_IMAGE_TAG") or compose_default_tag()


def image_available(tag: str) -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", f"nousresearch/hermes-agent:{tag}"],
        capture_output=True,
    )
    return probe.returncode == 0


def run_driver(driver: str, stdin: str = "", tag: str = "", mounts=(), image: str = "",
               python: str = "/opt/hermes/.venv/bin/python", docker_args=()) -> None:
    """Run ``driver``; it must print "image ok" on success.

    ``mounts`` adds read-only ``(host_path, container_path)`` bind mounts.
    ``image`` names another image in place of the Hermes one, with ``python``
    as its interpreter. ``docker_args`` go to ``docker run`` before the image
    (for example ``--network none``).
    """
    image = image or f"nousresearch/hermes-agent:{tag or image_tag()}"
    extra = []
    for host, container in mounts:
        extra += ["-v", f"{host}:{container}:ro"]
    cmd = [
        "docker", "run", "--rm", "-i", "--entrypoint", python,
        "-v", f"{HERMES_DIR}:/bootstrap:ro", *extra, *docker_args, image, "-c", driver,
    ]
    out = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if out.returncode != 0 or "image ok" not in out.stdout:
        print(f"FAIL image checks ({image}):\n{out.stdout}{out.stderr}", file=sys.stderr)
        raise SystemExit(1)
    print(f"image ok ({image})")


def image_gate(argv: list[str] | None = None) -> str | None:
    """The tag to run image checks with, or None to skip them.

    --host-only skips. A missing image skips with a line saying so, unless
    --image is given (run_driver then fails on its own) or
    HERMES_IMAGE_REQUIRED=1 is set, which fails here so a CI job that is meant
    to run image checks cannot pass without them.
    """
    args = sys.argv[1:] if argv is None else argv
    if "--host-only" in args:
        return None
    tag = image_tag()
    if "--image" in args or image_available(tag):
        return tag
    if os.environ.get("HERMES_IMAGE_REQUIRED") == "1":
        print(f"FAIL image checks: nousresearch/hermes-agent:{tag} not present "
              "and HERMES_IMAGE_REQUIRED=1", file=sys.stderr)
        raise SystemExit(1)
    print(f"skip image checks (nousresearch/hermes-agent:{tag} not present)")
    return None


def maybe_run(driver: str, stdin: str = "", argv: list[str] | None = None, mounts=()) -> None:
    tag = image_gate(argv)
    if tag is not None:
        run_driver(driver, stdin, tag, mounts)
