#!/usr/bin/env python3
"""Checks patch-guardrail-halt-stream.py: the tool-guardrail stop note reaches
the dashboard gateway's stream callback.

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the halt branch of
agent/turn_tool_round.py is cut out of the installed file with ast and run
against a real AIAgent, set up as the gateway sets it up: its message.delta
emitter in _stream_callback and no stream_delta_callback. Unpatched, the note
must not reach the gateway (the bug, so a fixed upstream shows up here);
patched, it must, once, and not while a hold_stream plugin holds the turn.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import ast
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from run_agent import AIAgent

SOURCE = Path("/opt/hermes/agent/turn_tool_round.py")
NOTE = "Stopped browser_exec: this turn has called it 3 times with the same arguments."


def halt_branch() -> ast.Module:
    """The body of `if agent._tool_guardrail_halt_decision is not None:`."""
    for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.If) and "_tool_guardrail_halt_decision is not None" in ast.unparse(node.test):
            return ast.Module(body=node.body, type_ignores=[])
    raise AssertionError("no guardrail halt branch in turn_tool_round.py")


def run_halt(hold: bool, display: bool) -> tuple[list, list]:
    """Run the halt branch; return (what the gateway emitter got, what the display callback got)."""
    gateway, shown = [], []
    agent = AIAgent.__new__(AIAgent)
    agent.stream_delta_callback = shown.append if display else None
    agent._stream_callback = gateway.append
    agent._hold_stream = hold
    agent._tool_guardrail_halt_decision = type("D", (), {"tool_name": "browser_exec", "code": "identical_call_streak_halt"})()
    agent._toolguard_controlled_halt_response = lambda decision: NOTE
    agent._emit_diagnostic_status = lambda msg: None
    agent._safe_print = lambda *a, **k: None
    messages = []
    scope = {
        "agent": agent, "messages": messages, "suppress": suppress,
        "append_message": lambda msgs, msg: msgs.append(msg),
        "_verdict": lambda action, result=None: action,
        "_turn_exit_reason": None, "final_response": "",
    }
    body = halt_branch()
    # The branch ends with `return _verdict("break")`; run it as a function body.
    fn = ast.FunctionDef(name="branch", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                         body=body.body, decorator_list=[], type_params=[])
    mod = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(mod, str(SOURCE), "exec"), scope)
    assert scope["branch"]() == "break"
    assert messages == [{"role": "assistant", "content": NOTE}], messages
    return gateway, shown


# Unpatched: the gateway gets nothing. If this fails, upstream fixed it; drop the overlay.
gateway, _ = run_halt(hold=False, display=False)
assert gateway == [], f"unpatched Hermes already streams the halt to _stream_callback: {gateway!r}"

out = subprocess.run([sys.executable, "/bootstrap/patch-guardrail-halt-stream.py"], capture_output=True, text=True)
assert "patched" in out.stdout and "not patched" not in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-guardrail-halt-stream.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

gateway, _ = run_halt(hold=False, display=False)
assert gateway == [NOTE], f"gateway turn: the halt note reaches _stream_callback once ({gateway!r})"

gateway, shown = run_halt(hold=False, display=True)
assert gateway == [NOTE] and shown == [NOTE, None], f"CLI turn: display gets the note and the flush, TTS gets no None ({gateway!r}, {shown!r})"

gateway, shown = run_halt(hold=True, display=True)
assert gateway == [] and shown == [], f"a held turn streams nothing ({gateway!r}, {shown!r})"
print("image ok")
'''


def test_wired() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    assert "run_patch /bootstrap/patch-guardrail-halt-stream.py" in runner, "05-patch-tool-calling.sh runs the overlay"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "./compose/hermes/patch-guardrail-halt-stream.py:/bootstrap/patch-guardrail-halt-stream.py:ro" in compose, \
        "docker-compose.yml mounts the overlay"


def main() -> None:
    test_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
