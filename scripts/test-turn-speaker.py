#!/usr/bin/env python3
"""Checks for speaker names on assistant rows (Boundary CR 0011 R1).

Host part is stdlib-only and covers labelling. With the pinned Hermes image
present (or --image), the gateway overlays are applied in container order
(twice, for idempotency), a fake OpenAI-compatible model records every
request, and one session runs turns as Ada, as Bob and with no speaker, then
again after a cold resume. Checks: the contract and capability, the error
codes, ``display_metadata.speaker`` in ``session.history``, labels on earlier
replies in later requests (byte-stable, and after the resume), the per-turn
note, and stripping a label the model copied.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
import turn_speaker as ts  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_label() -> None:
    expect(ts.label({"id": "ada", "display_name": "Ada"}) == "[speaker: Ada (ada)]", "name and id")
    expect(ts.label({"id": "ada", "display_name": "ada"}) == "[speaker: ada]", "same name and id once")
    expect(ts.label({"id": "ada"}) == "[speaker: ada]", "id only")
    expect(ts.label({"display_name": "Ada"}) == "[speaker: Ada]", "name only")
    expect("You are answering as Ada (ada)." in ts.turn_note({"id": "ada", "display_name": "Ada"}), "note names")


def test_strip() -> None:
    expect(ts.strip_leading_label("[speaker: Bob (bob)]\nHi") == "Hi", "copied label is stripped")
    expect(ts.strip_leading_label("  [speaker: Bob] Hi") == "Hi", "inline label is stripped")
    expect(ts.strip_leading_label("Hi [speaker: Bob]") == "Hi [speaker: Bob]", "only a leading label")
    expect(ts.strip_leading_label("[speaker: A]\n[speaker: B]\nHi") == "[speaker: B]\nHi", "one label only")


def test_stamp() -> None:
    ada = {"id": "ada", "display_name": "Ada"}
    msg = {"role": "assistant", "content": "Hello"}
    ts.stamp_row(msg, ada)
    expect(msg["api_content"] == "[speaker: Ada (ada)]\nHello", "label prefixes the wire text")
    expect(msg["content"] == "Hello", "display content is unchanged")
    expect(msg["display_metadata"] == {"speaker": ada}, "speaker in display_metadata")
    again = dict(msg)
    ts.stamp_row(again, {"id": "bob"})
    expect(again == msg, "an already-labelled row is left alone")

    promoted = {"role": "assistant", "content": "", "api_content": "Answer"}
    ts.stamp_row(promoted, ada)
    expect(promoted["api_content"] == "[speaker: Ada (ada)]\nAnswer", "a replay-only sidecar is labelled")

    failed = {"role": "assistant", "content": "Partial", "display_metadata": {"failure": "x"}}
    ts.stamp_row(failed, ada)
    expect(failed["display_metadata"] == {"failure": "x", "speaker": ada}, "existing metadata is kept")

    for untouched in (
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "assistant", "content": "Old", "_row_id": 7},
        {"role": "assistant", "content": "Summary", "display_kind": "hidden"},
        {"role": "user", "content": "Hi"},
        {"role": "tool", "content": "{}"},
    ):
        before = dict(untouched)
        ts.stamp_row(untouched, ada)
        expect(untouched == before, f"left alone: {before}")

    copied = {"role": "assistant", "content": "[speaker: Ada (ada)]\nHello"}
    ts.stamp_row(copied, ada)
    expect(copied["content"] == "Hello", "a copied label leaves the stored content")
    expect(copied["api_content"] == "[speaker: Ada (ada)]\nHello", "and is not doubled on the wire")


IMAGE_DRIVER = r'''
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

home = "/tmp/hermes-home"
PORT = 18766
os.environ["HERMES_HOME"] = home
os.makedirs(home, exist_ok=True)

# Same order as 05-patch-tool-calling.sh.
for patch in ("patch-history-budget.py", "patch-client-context.py", "patch-gateway-liveness.py",
              "patch-turn-speaker.py") * 2:
    out = subprocess.run([sys.executable, "/bootstrap/" + patch], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {patch}: {out.stdout}{out.stderr}")
src = open("/opt/hermes/tui_gateway/server.py").read()
assert src.count("# assistant-stack: turn speaker") == 1, "one install block"

turns = []  # request bodies of main-loop calls (aux calls carry no tools)
REPLIES = [
    {"tool_calls": [{"id": "call_1", "type": "function", "function": {
        "name": "todo", "arguments": json.dumps({"todos": [{"id": "1", "content": "x", "status": "pending"}]})}}]},
    {"content": "ADA-REPLY"},
    {"content": "[speaker: Bob (bob)]\nBOB-REPLY"},  # the model copies the label
    {"content": "PLAIN-REPLY"},
    {"content": "AFTER-RESUME"},
]


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
        if body.get("tools"):
            turns.append(body)
            reply = REPLIES[min(len(turns), len(REPLIES)) - 1]
        else:
            reply = {"content": "Title"}
        message = {"role": "assistant", "content": reply.get("content")}
        if reply.get("tool_calls"):
            message["tool_calls"] = reply["tool_calls"]
        finish = "tool_calls" if reply.get("tool_calls") else "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not body.get("stream"):
            self._json({"id": "c1", "object": "chat.completion", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return
        delta = dict(message)
        if reply.get("tool_calls"):
            delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(reply["tool_calls"])]
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
open(home + "/config.yaml", "w").write(f"""model:
  default: fake-model
  provider: custom
  base_url: http://127.0.0.1:{PORT}/v1
  api_key: local-llm
""")

sys.path.insert(0, "/opt/hermes")
report = os.fdopen(os.dup(1), "w")  # the gateway import points sys.stdout at stderr
from tui_gateway.contracts import registry  # noqa: E402
from tui_gateway import server  # noqa: E402
from agent import turn_speaker as ts  # noqa: E402
sys.stdout = sys.stderr


def say(*a):
    print(*a, file=report, flush=True)


def rpc(method, params):
    return server.handle_request({"id": 1, "method": method, "params": params})


def call(method, params):
    resp = rpc(method, params)
    assert "result" in resp, (method, resp)
    return resp["result"]


def code(resp):
    return (resp.get("error") or {}).get("code")


# Contract and capability.
_, problem = registry.validate_params(
    registry.METHODS["prompt.submit"], {"session_id": "x", "text": "hi", "speaker": {"id": "ada"}})
assert problem is None, problem
_, problem = registry.validate_params(
    registry.METHODS["prompt.submit"], {"session_id": "x", "text": "hi", "context": "c", "speaker": None})
assert problem is None, "client context survives the redeclared contract"
caps = call("gateway.capabilities", {})
assert caps["turn_speaker"] is True and caps["client_context"] is True and "turn_heartbeat" in caps, caps
registry.METHODS["gateway.capabilities"].result.model_validate(caps)

sid = call("session.create", {})["session_id"]
session = server._sessions[sid]

# Refusals.
for bad in ("ada", {"id": 5}, {"name": "Ada"}, {}, {"id": "  ", "display_name": "\n"}):
    resp = rpc("prompt.submit", {"session_id": sid, "text": "hi", "speaker": bad})
    assert code(resp) == ts.SPEAKER_INVALID, (bad, resp)
session["running"] = True
resp = rpc("prompt.submit", {"session_id": sid, "text": "hi", "speaker": {"id": "ada"}})
assert code(resp) == ts.SPEAKER_BUSY, resp
session["running"] = False
assert ts.TURN_KEY not in session, "a refusal leaves no speaker behind"


def run_turn(text, speaker=None, sid=sid, expect_calls=1):
    start = len(turns)
    params = {"session_id": sid, "text": text}
    if speaker is not None:
        params["speaker"] = speaker
    result = call("prompt.submit", params)
    assert result.get("status") == "streaming", result
    deadline = time.time() + 60
    while time.time() < deadline:
        record = server._sessions.get(sid) or {}
        if len(turns) >= start + expect_calls and not record.get("running"):
            time.sleep(0.3)
            return turns[start:]
        time.sleep(0.1)
    raise AssertionError(f"turn {text!r} did not finish")


ADA = {"id": "ada", "display_name": "Ada"}
BOB = {"id": "bob", "display_name": "Bob"}
ada_calls = run_turn("first", ADA, expect_calls=2)  # tool call, then text
bob_calls = run_turn("second", BOB)
plain_calls = run_turn("third")


def user_text(body):
    return next(str(m.get("content")) for m in reversed(body["messages"]) if m.get("role") == "user")


def assistants(body):
    return [m for m in body["messages"] if m.get("role") == "assistant"]


assert "You are answering as Ada (ada)." in user_text(ada_calls[0]), "turn note for Ada"
assert "You are answering as Bob (bob)." in user_text(bob_calls[0]), "turn note for Bob"
assert ts.TURN_HEADER not in user_text(plain_calls[0]), "no note on a turn without a speaker"

# Third turn's model input labels both earlier replies.
texts = [m.get("content") or "" for m in assistants(plain_calls[0])]
assert "[speaker: Ada (ada)]\nADA-REPLY" in texts, texts
assert "[speaker: Bob (bob)]\nBOB-REPLY" in texts, "copied label is not doubled"
tool_turns = [m for m in assistants(plain_calls[0]) if m.get("tool_calls")]
assert tool_turns and not str(tool_turns[0].get("content") or "").startswith("[speaker"), \
    "a tool-call message with no text is not labelled"
# Byte-stable: Ada's reply went out identically on turns two and three.
ada_wire = [m for m in assistants(bob_calls[0]) if "ADA-REPLY" in str(m.get("content"))]
assert ada_wire and ada_wire[0]["content"] == "[speaker: Ada (ada)]\nADA-REPLY", ada_wire

# session.history: speaker on assistant rows, clean text.
rows = call("session.history", {"session_id": sid})["messages"]
replies = [r for r in rows if r["role"] == "assistant" and r.get("text")]
by_text = {r["text"]: (r.get("display_metadata") or {}).get("speaker") for r in replies}
assert by_text.get("ADA-REPLY") == ADA, by_text
assert by_text.get("BOB-REPLY") == BOB, f"stored text is stripped and labelled: {by_text}"
assert by_text.get("PLAIN-REPLY") is None, by_text

# Cold resume: labels come back from the DB sidecar.
stored = session.get("session_key") or session.get("stored_session_id")
info = call("session.info", {"session_id": sid}) if stored is None else {}
stored = stored or info.get("stored_session_id")
call("session.close", {"session_id": sid})
resumed = call("session.resume", {"session_id": stored})["session_id"]
after = run_turn("fourth", {"id": "ada"}, sid=resumed)
texts = [m.get("content") or "" for m in assistants(after[0])]
assert "[speaker: Ada (ada)]\nADA-REPLY" in texts and "[speaker: Bob (bob)]\nBOB-REPLY" in texts, texts
assert "PLAIN-REPLY" in texts, texts
assert "You are answering as ada." in user_text(after[0])

say("labels, history, note, strip and resume ok")
say("image ok")
report.flush()
os._exit(0)
'''


def main() -> None:
    test_label()
    test_strip()
    test_stamp()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
