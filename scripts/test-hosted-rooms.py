#!/usr/bin/env python3
"""Checks that upstream hosted rooms (``groups.*``) run each member in its own profile.

Boundary change request 0011 asks for speaker names on assistant rows and a
``session.append`` method so a channel member can run in its own profile.
Upstream's same-gateway Discussion driver already does that, with no peers or
grants, so this test pins the behaviour the stack relies on instead.

Image-only: with the pinned Hermes image present (or --image), the stack's
gateway and kanban overlays are applied, a fake OpenAI-compatible model runs
inside the container and records every request, and a two-member room
(``default`` and ``orchestrator``) runs two user messages. The checks:

- each member turn runs in its own profile home, with that profile's SOUL,
  toolsets (kanban only for the orchestrator) and memory file;
- a member's ``memory`` write lands in its own profile and nowhere else;
- each member's prompt holds only the messages it hasn't seen, with other
  members' replies labelled ``@handle: text``;
- ``groups.log`` attributes every reply to its member;
- member sessions are hidden ``Group: <room_id>`` sessions with source
  ``bot_room``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

home = "/tmp/hermes-home"
orch_home = home + "/profiles/orchestrator"
PORT = 18765
os.environ["HERMES_HOME"] = home

# Gateway overlays (05-patch-tool-calling.sh), then kanban (12-patch-kanban-guards.sh).
for patch in (
    "patch-history-budget.py", "patch-client-context.py", "patch-gateway-liveness.py",
    "patch-kanban-guards.py", "patch-kanban-edit.py", "patch-kanban-wake.py",
):
    out = subprocess.run([sys.executable, "/bootstrap/" + patch], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {patch}: {out.stdout}{out.stderr}")

# Fake model. A member turn is told apart from auxiliary calls (titles) by its tools.
turns = []  # (who, request body) for member turns only


def reply_for(who, messages):
    last = messages[-1]
    if who == "ORCH" and last.get("role") == "user" and "save a note" in str(last.get("content")):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_mem", "type": "function", "function": {"name": "memory", "arguments": json.dumps(
                {"action": "add", "target": "memory", "content": "ORCH-SAVED-IN-ROOM"})}},
            {"id": "call_kb", "type": "function", "function": {"name": "kanban_list", "arguments": "{}"}}]}
    return {"role": "assistant", "content": f"{who}-REPLY-{len(turns)}"}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        messages = body.get("messages") or []
        system = " ".join(str(m.get("content")) for m in messages if m.get("role") == "system")
        who = "ORCH" if "ORCH-SOUL" in system else "HELPER" if "HELPER-SOUL" in system else "AUX"
        if who != "AUX" and body.get("tools"):
            turns.append((who, body))
            message = reply_for(who, messages)
        else:
            message = {"role": "assistant", "content": "Spike title"}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not body.get("stream"):
            self._json({"id": "c1", "object": "chat.completion", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        delta = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(message["tool_calls"])]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}):
            chunk.update({"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "created": 0})
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


srv = ThreadingHTTPServer(("127.0.0.1", PORT), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()

CONFIG = """model:
  default: fake-model
  provider: custom
  base_url: http://127.0.0.1:%d/v1
  api_key: local-llm
platform_toolsets:
  cli: [%s]
"""
for path, soul, mem, toolsets in (
    (home, "HELPER-SOUL", "HELPER-MEMORY-NOTE", "hermes-cli"),
    (orch_home, "ORCH-SOUL", "ORCH-MEMORY-NOTE", "hermes-cli, kanban"),
):
    os.makedirs(path + "/memories", exist_ok=True)
    open(path + "/config.yaml", "w").write(CONFIG % (PORT, toolsets))
    open(path + "/SOUL.md", "w").write(f"You are {soul}.\n")
    open(path + "/memories/MEMORY.md", "w").write(mem)

sys.path.insert(0, "/opt/hermes")
report = os.fdopen(os.dup(1), "w")  # the gateway import points sys.stdout at stderr
from tui_gateway import server  # noqa: E402
from tui_gateway import methods_groups  # noqa: E402
from gateway import hosted_room_discussion as discussion  # noqa: E402
sys.stdout = sys.stderr  # session events go to stderr, not the report


def say(*a):
    print(*a, file=report, flush=True)


def call(method, params):
    resp = server.handle_request({"id": 1, "method": method, "params": params})
    assert "result" in resp, (method, resp)
    return resp["result"]


def wait_settled(room_id, settled, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = call("groups.state", {"room_id": room_id})["driver_status"]
        if not st["working"] and st["counts"].get("settled", 0) >= settled:
            return st
        time.sleep(0.5)
    raise AssertionError(f"room did not settle {settled} tasks: {st}")


def tools_of(body):
    return {t["function"]["name"] for t in body.get("tools") or []}


def system_of(body):
    return " ".join(str(m.get("content")) for m in body["messages"] if m.get("role") == "system")


def last_user(body):
    return next(str(m.get("content")) for m in reversed(body["messages"]) if m.get("role") == "user")


service = methods_groups.start_hosted_room_service()
assert service is not None, "the dashboard process starts the hosted room service"
assert set(service.local_profiles()) == {"default", "orchestrator"}, service.local_profiles()
caps = call("groups.capabilities", {})
assert caps["driver"] is True, caps

# Control: a plain dashboard session on the orchestrator profile.
sid = call("session.create", {"profile": "orchestrator"})["session_id"]
call("prompt.submit", {"session_id": sid, "text": "hello"})
deadline = time.time() + 60
while not turns and time.time() < deadline:
    time.sleep(0.2)
assert turns and turns[0][0] == "ORCH", "control turn ran in the orchestrator profile"
control = turns.pop(0)[1]
control_memory = "ORCH-MEMORY-NOTE" in system_of(control)
time.sleep(1)

call("groups.create", {"room_id": "room1", "name": "Spike", "members": [
    {"member_id": "m-helper", "profile": "default", "handle": "helper", "display_name": "Helper"},
    {"member_id": "m-orch", "profile": "orchestrator", "handle": "orch", "display_name": "Orchestrator"},
]})
call("groups.send", {"room_id": "room1", "event_id": "e1",
                     "payload": {"thread_id": "t1", "text": "@helper @orch save a note"}})
wait_settled("room1", 2)
call("groups.send", {"room_id": "room1", "event_id": "e2",
                     "payload": {"thread_id": "t1", "text": "Anything else?"}})
wait_settled("room1", 4)

by = {"HELPER": [b for w, b in turns if w == "HELPER"], "ORCH": [b for w, b in turns if w == "ORCH"]}
assert len(by["HELPER"]) == 2, f"helper ran twice, got {len(by['HELPER'])}"
assert len(by["ORCH"]) == 3, f"orch ran twice, one tool round trip, got {len(by['ORCH'])}"
helper1, helper2 = by["HELPER"]
orch1, orch_after_tools, orch2 = by["ORCH"]

# Profile: SOUL and toolsets.
assert "ORCH-SOUL" not in system_of(helper1) and "HELPER-SOUL" not in system_of(orch1)
assert not any(t.startswith("kanban") for t in tools_of(helper1)), "helper has no kanban tools"
kanban = sorted(t for t in tools_of(orch1) if t.startswith("kanban"))
assert "kanban_create" in kanban and "kanban_update" in kanban, kanban
assert "bot_room" not in str(tools_of(orch1)), "no room-only tool names leak into the tool list"

# Tool calls run in the member's profile.
tool_rows = [m for m in orch_after_tools["messages"] if m.get("role") == "tool"]
assert len(tool_rows) == 2, tool_rows
kanban_result = next(str(m["content"]) for m in tool_rows if m.get("tool_call_id") == "call_kb")
assert "error" not in kanban_result.lower()[:200], kanban_result[:300]
orch_mem = open(orch_home + "/memories/MEMORY.md").read()
default_mem = open(home + "/memories/MEMORY.md").read()
assert "ORCH-SAVED-IN-ROOM" in orch_mem, "a member's memory write lands in its own profile"
assert "ORCH-SAVED-IN-ROOM" not in default_mem, "and not in the default profile"
room_memory = "ORCH-MEMORY-NOTE" in system_of(orch1)
assert room_memory == control_memory, "a room turn loads memory the same way a plain session does"

# Prompts: deltas with attributed replies.
assert "User (user): @helper @orch save a note" in last_user(helper1)
assert "@helper: HELPER-REPLY" in last_user(orch1), "orch sees helper's reply, attributed"
assert "@orch: ORCH-REPLY" in last_user(helper2), "helper sees orch's reply in its next turn"
assert "save a note" not in last_user(helper2), "already-seen messages are not repeated"
assert "Anything else?" in last_user(orch2) and "HELPER-REPLY" in last_user(orch2)
assert "@helper: HELPER-REPLY-0" not in last_user(orch2), "orch's second delta skips what it saw"
history_roles = [m.get("role") for m in orch2["messages"] if m.get("role") in ("user", "assistant")]
assert history_roles[:2] == ["user", "assistant"], "a member keeps its own room transcript"

# The room log.
events = call("groups.log", {"room_id": "room1", "limit": 100})["events"]
replies = [e for e in events if e["kind"] == "message.member"] or \
    [e for e in events if "member_id" in e["payload"] and "text" in e["payload"]]
speakers = [(e["payload"]["member_id"], e["payload"]["text"].split("-REPLY")[0]) for e in replies]
assert ("m-helper", "HELPER") in speakers and ("m-orch", "ORCH") in speakers, speakers

# Member sessions.
rows = call("session.list", {"profile": "orchestrator", "include_hidden": True})["sessions"]
room_rows = [r for r in rows if r.get("title") == "Group: room1"]
assert len(room_rows) == 1 and room_rows[0]["source"] == "bot_room", rows
visible = call("session.list", {"profile": "orchestrator"})["sessions"]
assert not any(r.get("title") == "Group: room1" for r in visible), "member sessions are hidden"

say("members run in their own profile: SOUL, toolsets, tool calls, memory writes")
say(f"orchestrator kanban tools in room: {len(kanban)} ({', '.join(kanban[:4])}, ...)")
say(f"seeded MEMORY.md in system prompt: room={room_memory} plain session={control_memory}")
say(f"log event kinds: {sorted({e['kind'] for e in events})}")
say(f"limits: members {discussion.MIN_DISCUSSION_MEMBERS}-{discussion.MAX_DISCUSSION_MEMBERS}, "
    f"rounds {discussion.MAX_DISCUSSION_ROUNDS}, member messages per user message "
    f"{discussion.MAX_DISCUSSION_MESSAGES}, delta lines {discussion.MAX_DISCUSSION_DELTA_LINES}")
say("image ok")
report.flush()
os._exit(0)  # the room worker and fake model are daemon threads
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
