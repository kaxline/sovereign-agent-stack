#!/usr/bin/env python3
"""Checks for gated cron fires and read-only cron sessions (Boundary CR 0033).

Host part is stdlib-only: the prompt.submit refusal, the fire match and the
settle step against fakes. With the pinned Hermes image present (or --image),
the gateway overlays are applied in container order (twice, for idempotency)
and the real ``gateway.session_cron.execute`` runs fires for a monitor_url job
against a local server and a fake model:

- three unchanged fires leave no row in ``session.list``; each is titled,
  hidden and ended ``cron_skipped`` (``include_hidden`` lists them);
- a changed fire runs the agent and lists one titled session;
- a typed message into a cron session (the gateway's own refusal) leaves the
  session alone;
- ``session.resume`` on a cron session, then ``prompt.submit``, answers 4087
  with the read-only text and ``error_surface`` and stores no message; an
  ordinary resumed session is not marked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

import hermes_image  # noqa: E402
import cron_gated_sessions as cgs  # noqa: E402


class FakeServer:
    def __init__(self):
        self._sessions = {}
        self.calls = []

    def _err(self, rid, code, msg, data=None):
        return {"id": rid, "error": {"code": code, "message": msg, **({"data": data} if data else {})}}


def test_submit_refusal() -> None:
    server = FakeServer()

    def handler(rid, params):
        server.calls.append(params)
        return {"id": rid, "result": {"status": "streaming"}}

    submit = cgs.wrap_prompt_submit(server, handler)
    server._sessions["s1"] = {}
    expect("result" in submit(1, {"session_id": "s1", "text": "hi"}), "an ordinary session runs")
    expect("result" in submit(1, {"session_id": "gone", "text": "hi"}), "unknown ids reach upstream's 4001")
    server._sessions["s1"][cgs.SESSION_KEY] = True
    sys.modules.setdefault("agent", SimpleNamespace())
    sys.modules["agent.i18n"] = SimpleNamespace(t=lambda key: f"<{key}>")
    resp = submit(2, {"session_id": "s1", "text": "hi"})
    expect(resp["error"]["code"] == cgs.READ_ONLY_CODE, f"refused: {resp}")
    expect(resp["error"]["message"] == "<gateway.errors.cron_session_read_only>", "upstream's text")
    expect(resp["error"]["data"]["error_surface"]["code"] == "cron_session_read_only", "upstream's surface")
    expect(len(server.calls) == 2, "the refusal never reaches upstream")
    expect(submit.__wrapped__ is handler, "wrapped handler is reachable")


def test_own_fire() -> None:
    policy = SimpleNamespace(request_json=json.dumps({"request_id": "cron:j:1", "cron_job": {"id": "j"}}))
    expect(cgs._own_fire({"request_id": "cron:j:1"}, policy) == {"id": "j"}, "the frozen fire")
    expect(cgs._own_fire({"request_id": "typed"}, policy) is None, "a typed message is not the fire")
    expect(cgs._own_fire({"request_id": "cron:j:1"}, SimpleNamespace(request_json="{")) is None, "bad policy")


class FakeDB:
    def __init__(self, row):
        self.row = row
        self.hidden = None
        self.ended = None

    def get_session(self, sid):
        return self.row

    def set_session_hidden(self, sid, hidden):
        self.hidden = hidden

    def end_session(self, sid, reason):
        self.ended = reason


def test_settle() -> None:
    db = FakeDB({"message_count": 2, "title": None})
    expect(cgs.settle_skipped(db, "s", {"id": "j"}) is False, "a session with messages is left alone")
    expect(db.hidden is None and db.ended is None, "and untouched")
    db = FakeDB({"message_count": 0, "title": "Already"})
    expect(cgs.settle_skipped(db, "s", {"id": "j"}) is True, "an empty session settles")
    expect(db.hidden is True and db.ended == cgs.SKIPPED_END_REASON, "hidden and ended")
    expect(cgs.settle_skipped(FakeDB(None), "s", {"id": "j"}) is False, "a missing row is skipped")


IMAGE_DRIVER = r'''
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

home = "/tmp/hermes-home"
MODEL_PORT, MONITOR_PORT = 18767, 18768
os.environ["HERMES_HOME"] = home
os.makedirs(home, exist_ok=True)

# Same order as 05-patch-tool-calling.sh.
for patch in ("patch-history-budget.py", "patch-client-context.py", "patch-gateway-liveness.py",
              "patch-turn-speaker.py", "patch-cron-gated-sessions.py") * 2:
    out = subprocess.run([sys.executable, "/bootstrap/" + patch], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {patch}: {out.stdout}{out.stderr}")
for path in ("/opt/hermes/gateway/session_cron.py", "/opt/hermes/tui_gateway/server.py"):
    assert open(path).read().count("# assistant-stack: cron gated sessions") == 1, f"one install block in {path}"

monitor_body = ["STATUS: steady\n"]
model_calls = []


class Monitor(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = monitor_body[0].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        model_calls.append(body)
        message = {"role": "assistant", "content": "CHANGED-REPORT"}
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not body.get("stream"):
            self._json({"id": "c1", "object": "chat.completion", "created": 0, "model": "fake-model",
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ({"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}):
            chunk.update({"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "created": 0})
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


for port, handler in ((MONITOR_PORT, Monitor), (MODEL_PORT, Model)):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
open(home + "/config.yaml", "w").write(f"""model:
  default: fake-model
  provider: custom
  base_url: http://127.0.0.1:{MODEL_PORT}/v1
  api_key: local-llm
security:
  allow_private_urls: true
""")

sys.path.insert(0, "/opt/hermes")
report = os.fdopen(os.dup(1), "w")  # the gateway import points sys.stdout at stderr
from tui_gateway import server  # noqa: E402
from gateway import session_cron  # noqa: E402
from gateway.session_contract import SessionRef  # noqa: E402
from hermes_state_keys import CRON_PRINCIPAL_ID  # noqa: E402
from hermes_state_registry import acquire  # noqa: E402
from cron.monitor import hash_monitor_output  # noqa: E402
from agent import cron_gated_sessions as cgs  # noqa: E402
sys.stdout = sys.stderr


def say(*a):
    print(*a, file=report, flush=True)


assert hasattr(session_cron.execute, "__wrapped__"), "execute is wrapped"
db = acquire(Path(home) / "state.db")
authority = SimpleNamespace(db=db, sessions={}, pending_results={}, profile_id="default")
JOB = {"id": "watch1", "name": "Watch status page", "prompt": "Report what changed.",
       "monitor_url": f"http://127.0.0.1:{MONITOR_PORT}/status", "schedule_display": "every 15m"}


def fire(n, job, *, request_id=None, typed=False):
    sid = f"cron_watch1_fire{n}"
    rid = f"cron:{job['id']}:exec{n}"
    db.create_session(sid, source="cron")
    authority.sessions[sid] = SimpleNamespace(source=SimpleNamespace(user_id=CRON_PRINCIPAL_ID))
    policy = SimpleNamespace(source="cron", request_json=json.dumps(
        {"cron_job": job, "extra_prompt": None, "request_id": rid}))
    row = {"payload": {"text": "typed" if typed else ""}, "request_id": request_id or rid,
           "principal_id": CRON_PRINCIPAL_ID, "admission_id": f"adm{n}", "owner_epoch": 1, "generation": 1}
    try:
        out = asyncio.run(session_cron.execute(authority, SessionRef("default", sid), row, policy))
    except RuntimeError as exc:
        out = f"raised: {exc}"
    return sid, out


def listed(include_hidden=False):
    resp = server.handle_request({"id": 1, "method": "session.list",
                                  "params": {"include_hidden": include_hidden, "limit": 500}})
    assert "result" in resp, resp
    return {row["id"]: row for row in resp["result"]["sessions"]}


# R1: unchanged output, three fires.
steady = dict(JOB, monitor_state={"last_output_hash": hash_monitor_output(monitor_body[0])})
gated = [fire(n, steady) for n in (1, 2, 3)]
visible = listed()
for sid, out in gated:
    assert sid not in visible, f"gated fire {sid} is listed: {out!r}"
    stored = db.get_session(sid)
    assert stored["hidden"] in (1, True), stored
    assert stored["end_reason"] == "cron_skipped" and stored["ended_at"], stored
    assert stored["title"] and stored["title"].startswith("Watch status page · "), stored["title"]
hidden = listed(include_hidden=True)
assert all(sid in hidden for sid, _ in gated), "include_hidden still lists them"
assert not model_calls, "no model call on a gated fire"
say("unchanged fires: no listed row, titled, hidden, ended")

# R1: changed output runs the agent and lists one titled session.
monitor_body[0] = "STATUS: degraded\n"
sid, out = fire(4, steady)
assert model_calls, f"changed fire reached the model: {out!r}"
row = listed().get(sid)
assert row is not None, f"changed fire is listed: {out!r}"
stored = db.get_session(sid)
assert stored["title"] and stored["title"].startswith("Watch status page · "), stored
assert stored["end_reason"] != "cron_skipped" and not stored["hidden"], stored
say("changed fire: listed and titled by upstream")

# A typed message reaching the gateway's cron execute is refused upstream; the session stays as it was.
sid, out = fire(5, steady, request_id="typed-1", typed=True)
stored = db.get_session(sid)
assert not stored["hidden"] and stored["ended_at"] is None, stored
say("typed admission: session untouched")

# R2: resume a cron session that ran, then type into it.
ran = gated and "cron_watch1_fire4"
before = db.get_session(ran)["message_count"]
resumed = server.handle_request({"id": 2, "method": "session.resume", "params": {"session_id": ran}})
assert "result" in resumed, resumed
runtime = resumed["result"]["session_id"]
assert server._sessions[runtime].get(cgs.SESSION_KEY) is True, "resumed cron session is marked"
resp = server.handle_request({"id": 3, "method": "prompt.submit", "params": {"session_id": runtime, "text": "hi"}})
err = resp.get("error") or {}
assert err.get("code") == cgs.READ_ONLY_CODE, resp
from agent.i18n import t  # noqa: E402
assert err.get("message") == t("gateway.errors.cron_session_read_only"), err
assert err["data"]["error_surface"] == cgs.READ_ONLY_SURFACE, err
time.sleep(0.5)
assert db.get_session(ran)["message_count"] == before, "no message stored"
say("resumed cron session refuses prompt.submit")

db.create_session("plain1", source="tui")
db.append_message("plain1", "user", "hello")
resumed = server.handle_request({"id": 4, "method": "session.resume", "params": {"session_id": "plain1"}})
assert "result" in resumed, resumed
assert not server._sessions[resumed["result"]["session_id"]].get(cgs.SESSION_KEY), "ordinary session not marked"
say("ordinary resumed session not marked")
say("image ok")
report.flush()
os._exit(0)
'''


def main() -> None:
    test_submit_refusal()
    test_own_fire()
    test_settle()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
