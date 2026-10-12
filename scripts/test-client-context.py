#!/usr/bin/env python3
"""Checks for client context and the prompt.submit profile check.

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the gateway overlays are applied in the image in container order (twice, for
idempotency), the wire contract must accept ``context``, capabilities from
every overlay must validate together, and the wrapped handlers are driven
against fake session records.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import client_context as cc  # noqa: E402
import hermes_image  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_coerce() -> None:
    expect(cc.coerce_context(None) is None, "null clears")
    expect(cc.coerce_context("   ") is None, "blank clears")
    expect(cc.coerce_context("  Room: ops \n") == "Room: ops", "text is stripped")
    for bad in (1, True, ["x"], {"a": 1}, "x" * (cc.MAX_CONTEXT_CHARS + 1)):
        try:
            cc.coerce_context(bad)
        except ValueError:
            continue
        fail(f"expected ValueError for {type(bad).__name__}")


def test_blocks() -> None:
    expect(cc.session_block(None) == "", "no session block without text")
    expect(cc.turn_block("") == "", "no turn block without text")
    block = cc.turn_block("Answering: Ada")
    expect(block.startswith(cc.TURN_HEADER), "turn block has its header")
    expect(cc.CAUTION in block and block.endswith("Answering: Ada"), "turn block carries caution then text")


def test_compose_ephemeral() -> None:
    agent = SimpleNamespace(ephemeral_system_prompt="Base prompt.")
    cc.compose_ephemeral(agent, "BLOCK")
    expect(agent.ephemeral_system_prompt == "Base prompt.\n\nBLOCK", "block is appended to the base")
    cc.compose_ephemeral(agent, "BLOCK")
    expect(agent.ephemeral_system_prompt == "Base prompt.\n\nBLOCK", "reapplying is byte-stable")
    cc.compose_ephemeral(agent, "OTHER")
    expect(agent.ephemeral_system_prompt == "Base prompt.\n\nOTHER", "a new block replaces the old one")
    agent.ephemeral_system_prompt = "Personality."  # upstream rewrote it (personality switch)
    cc.compose_ephemeral(agent, "OTHER")
    expect(agent.ephemeral_system_prompt == "Personality.\n\nOTHER", "an upstream change becomes the base")
    cc.compose_ephemeral(agent, "")
    expect(agent.ephemeral_system_prompt == "Personality.", "clearing restores the base")
    bare = SimpleNamespace(ephemeral_system_prompt=None)
    cc.compose_ephemeral(bare, "")
    expect(bare.ephemeral_system_prompt is None, "no base and no block stays None")
    cc.compose_ephemeral(bare, "BLOCK")
    expect(bare.ephemeral_system_prompt == "BLOCK", "block alone needs no base")


IMAGE_DRIVER = r'''
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

home = "/tmp/hermes-home"
os.makedirs(home + "/profiles/other", exist_ok=True)
os.environ["HERMES_HOME"] = home

# Same order as 05-patch-tool-calling.sh.
for patch in ("patch-history-budget.py", "patch-client-context.py", "patch-gateway-liveness.py") * 2:
    out = subprocess.run([sys.executable, "/bootstrap/" + patch], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {patch}: {out.stdout}{out.stderr}")
for name in ("sessions.py", "prompt_voice.py"):
    src = open("/opt/hermes/tui_gateway/contracts/" + name).read()
    assert "client context" not in src, "contract files stay untouched"
src = open("/opt/hermes/tui_gateway/server.py").read()
assert src.count("# assistant-stack: client context") == 1, "one install block"

sys.path.insert(0, "/opt/hermes")
from tui_gateway.contracts import registry
from tui_gateway import server
from agent import client_context as cc

for name, params in (
    ("session.create", {"context": "Room: ops"}),
    ("session.resume", {"session_id": "x", "context": None}),
    ("prompt.submit", {"session_id": "x", "text": "hi", "context": "Answering: Ada"}),
):
    _, problem = registry.validate_params(registry.METHODS[name], params)
    assert problem is None, (name, problem)
_, problem = registry.validate_params(registry.METHODS["prompt.submit"], {"session_id": "x", "contxt": "a"})
assert problem is not None, "unknown keys are still refused"
_, problem = registry.validate_params(registry.METHODS["session.create"], {"history_budget": 1000, "context": "a"})
assert problem is None, "history_budget survives the redeclared create contract"

caps = server.handle_request({"id": 0, "method": "gateway.capabilities", "params": {}})["result"]
assert caps["client_context"] is True and caps["prompt_profile_check"] is True, caps
assert "turn_heartbeat" in caps and "session_live_status" in caps, "gateway liveness fields kept"
fields = registry.METHODS["gateway.capabilities"].result.model_fields
assert {"client_context", "prompt_profile_check", "turn_heartbeat", "session_live_status"} <= set(fields)
registry.METHODS["gateway.capabilities"].result.model_validate(caps)  # raises; check_result may only log

assert server._client_context_installed
assert server._run_prompt_submit.__globals__["_prepare_turn_input"] is server._prepare_turn_input
assert getattr(server._prepare_turn_input, "__wrapped__", None) is not None

def session(**extra):
    record = {"profile_home": None, "running": False, "history_lock": threading.Lock()}
    record.update(extra)
    return record

def code(response):
    return (response.get("error") or {}).get("code")

# Profile check through the installed chain: it answers before the upstream handler runs.
server._sessions["s1"] = session()
resp = server.handle_request({"id": 1, "method": "prompt.submit",
                              "params": {"session_id": "s1", "text": "hi", "profile": "other"}})
assert code(resp) == cc.PROFILE_MISMATCH, resp
assert resp["error"]["data"]["session_profile"], resp
resp = server.handle_request({"id": 2, "method": "prompt.submit",
                              "params": {"session_id": "s1", "text": "hi", "profile": "missing"}})
assert code(resp) == 4064, resp

# The rest against a fake upstream handler.
calls = []
def fake_submit(rid, params):
    calls.append(params)
    return server._ok(rid, {"status": fake_submit.status})
fake_submit.status = "streaming"
submit = cc.wrap_prompt_submit(server, fake_submit)

other_home = server._profile_home("other")
server._sessions["s2"] = session(profile_home=str(other_home))
assert "result" in submit(3, {"session_id": "s2", "text": "hi", "profile": "other"}), "matching profile passes"
assert code(submit(4, {"session_id": "s2", "text": "hi", "profile": "default"})) == cc.PROFILE_MISMATCH
assert "result" in submit(5, {"session_id": "s1", "text": "hi", "profile": "default"}), "launch profile matches"
assert "result" in submit(6, {"session_id": "s2", "text": "hi", "profile": "default", "_hosted_task": {}}), \
    "internal hosted submits skip the profile check"
assert "result" in submit(7, {"session_id": "gone", "text": "hi", "profile": "other"}), "unknown sid -> upstream"

s1 = server._sessions["s1"]
assert code(submit(8, {"session_id": "s1", "text": "hi", "context": 5})) == cc.CONTEXT_INVALID
assert code(submit(9, {"session_id": "s1", "text": "hi", "context": "x" * 20000})) == cc.CONTEXT_INVALID
s1["running"] = True
assert code(submit(10, {"session_id": "s1", "text": "hi", "context": "Answering: Ada"})) == cc.CONTEXT_BUSY
assert "result" in submit(11, {"session_id": "s1", "text": "hi"}), "busy without context goes upstream"
s1["running"] = False
assert "result" in submit(12, {"session_id": "s1", "text": "hi", "context": "Answering: Ada"})
assert s1.get(cc.TURN_KEY) == "Answering: Ada", "a streaming turn keeps the context for the runner"
s1.pop(cc.TURN_KEY)
fake_submit.status = "queued"
submit(13, {"session_id": "s1", "text": "hi", "context": "Answering: Ada"})
assert cc.TURN_KEY not in s1, "a reply that did not start a turn drops the context"
fake_submit.status = "streaming"

real_isolation = server._session_uses_compute_host
server._session_uses_compute_host = lambda *a, **k: True
try:
    assert code(submit(14, {"session_id": "s1", "text": "hi", "context": "a"})) == cc.CONTEXT_ISOLATED
    s1[cc.SESSION_KEY] = "Room: ops"
    assert code(submit(15, {"session_id": "s1", "text": "hi"})) == cc.CONTEXT_ISOLATED
    s1.pop(cc.SESSION_KEY)
    assert "result" in submit(16, {"session_id": "s1", "text": "hi"}), "no context runs isolated"
finally:
    server._session_uses_compute_host = real_isolation

# session.create / session.resume stamping.
def fake_open(rid, params):
    return server._ok(rid, {"session_id": "s3"})
open_ = cc.wrap_session_open(server, fake_open)
server._sessions["s3"] = session()
assert code(open_(20, {"context": []})) == cc.CONTEXT_INVALID
open_(21, {"context": "Room: ops"})
assert server._sessions["s3"][cc.SESSION_KEY] == "Room: ops"
open_(22, {})
assert server._sessions["s3"][cc.SESSION_KEY] == "Room: ops", "omitted keeps the value"
open_(23, {"context": None})
assert cc.SESSION_KEY not in server._sessions["s3"], "null clears"

# Turn input: session context in the system prompt, turn context as a one-shot note.
agent = SimpleNamespace(ephemeral_system_prompt="Base.")
def fake_prepare(sid, sess, st, text, images):
    st.agent = agent
    return ("p", "m", 80, None)
prepare = cc.wrap_prepare_turn_input(fake_prepare)
s3 = server._sessions["s3"]
s3[cc.SESSION_KEY] = "Room: ops"
s3[cc.TURN_KEY] = "Answering: Ada"
prepare("s3", s3, SimpleNamespace(agent=None), "hi", [])
assert cc.TURN_KEY not in s3, "turn context is consumed"
assert agent.ephemeral_system_prompt == "Base.\n\n" + cc.session_block("Room: ops")
assert agent._gateway_turn_context_notes == cc.turn_block("Answering: Ada")

from agent.turn_context import _merge_gateway_notes
merged = _merge_gateway_notes(agent, [{"role": "user", "content": "hi"}], 0, "")
assert merged == cc.turn_block("Answering: Ada"), "upstream picks the note up for the user message"
assert agent._gateway_turn_context_notes == "", "upstream consumes it once"

prepare("s3", s3, SimpleNamespace(agent=None), "again", [])
assert agent._gateway_turn_context_notes == "", "no turn context on the next turn"
assert agent.ephemeral_system_prompt == "Base.\n\n" + cc.session_block("Room: ops"), "system prompt is stable"
s3.pop(cc.SESSION_KEY)
prepare("s3", s3, SimpleNamespace(agent=None), "again", [])
assert agent.ephemeral_system_prompt == "Base.", "cleared session context restores the base"
# Importing the gateway points sys.stdout at stderr (stdout is its JSON-RPC channel).
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_coerce()
    test_blocks()
    test_compose_ephemeral()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
