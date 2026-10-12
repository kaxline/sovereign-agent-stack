#!/usr/bin/env python3
"""Checks for the turn heartbeat and session.live_status overlay.

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the overlay is applied in the image (twice, for idempotency) and driven
against fake session records and a usage ticker whose counters never move.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import gateway_liveness as gl  # noqa: E402
import hermes_image  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_interval() -> None:
    saved = os.environ.pop(gl.HEARTBEAT_ENV, None)
    logging.disable(logging.WARNING)  # invalid values warn by design
    try:
        expect(gl.heartbeat_interval() == 30.0, "default is 30s")
        for raw, want in (("0", 0.0), ("12.5", 12.5), (" 45 ", 45.0), ("", 30.0),
                          ("abc", 30.0), ("-1", 30.0), ("nan", 30.0), ("inf", 30.0)):
            os.environ[gl.HEARTBEAT_ENV] = raw
            expect(gl.heartbeat_interval() == want, f"{raw!r} -> {want}")
    finally:
        logging.disable(logging.NOTSET)
        os.environ.pop(gl.HEARTBEAT_ENV, None)
        if saved is not None:
            os.environ[gl.HEARTBEAT_ENV] = saved


def test_idle_seconds() -> None:
    expect(gl.idle_seconds(None) is None, "no session")
    expect(gl.idle_seconds({"agent": object()}) is None, "agent without an activity clock")
    agent = SimpleNamespace(get_activity_summary=lambda: {"seconds_since_activity": 12.345,
                                                          "last_activity_desc": "secret"})
    expect(gl.idle_seconds({"agent": agent}) == 12.3, "rounded seconds")
    broken = SimpleNamespace(get_activity_summary=lambda: 1 / 0)
    expect(gl.idle_seconds({"agent": broken}) is None, "a failing summary is unknown")


IMAGE_DRIVER = r'''
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

os.environ["HERMES_HOME"] = "/tmp/hermes-home"
os.makedirs("/tmp/hermes-home/profiles/other", exist_ok=True)
os.environ["HERMES_TURN_HEARTBEAT_S"] = "0.2"

for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-gateway-liveness.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
src = open("/opt/hermes/tui_gateway/server.py").read()
assert src.count("assistant-stack: gateway liveness") == 1, "installed once"

sys.path.insert(0, "/opt/hermes")
from tui_gateway.contracts import registry
from tui_gateway import server
from agent import gateway_liveness as gl

registry.STRICT = True  # a result or payload off its contract raises
assert server._gateway_liveness_installed
assert "session.live_status" in registry.METHODS and "turn.heartbeat" in registry.EVENTS
assert server._invoke_agent.__globals__["_start_usage_ticker"] is server._start_usage_ticker, \
    "the turn resolves the wrapped ticker"
assert getattr(server._start_usage_ticker, "__wrapped__", None) is not None
gl.install(server)  # idempotent

def session(**extra):
    record = {"profile_home": None, "running": False, "history_lock": threading.Lock()}
    record.update(extra)
    return record

def call(method, params):
    return server.handle_request({"id": 1, "method": method, "params": params})

# ── heartbeat: frozen counters, turn running ──
events = []
real_emit = server._emit
server._emit = lambda event, sid, payload=None: events.append((event, sid, payload)) or True
try:
    agent = SimpleNamespace(model="m", get_activity_summary=lambda: {
        "seconds_since_activity": 4.0, "last_activity_desc": "waiting for stream response"})
    server._sessions["hb"] = session(agent=agent, running=True, inflight_turn={"started_at": 1000.0})
    stop, thread = server._start_usage_ticker("hb", agent)
    time.sleep(1.1)
    stop.set()
    thread.join()
    done = len(events)
    time.sleep(0.5)
    assert len(events) == done, "nothing after the ticker is stopped and joined"
    beats = [p for e, s, p in events if e == "turn.heartbeat"]
    assert [e for e, _, _ in events if e != "turn.heartbeat"] == [], "frozen counters: no session.usage"
    assert 3 <= len(beats) <= 6, len(beats)
    assert all(s == "hb" for e, s, _ in events)
    assert beats[0] == {"running": True, "status": "working", "turn_started_at": 1000.0,
                        "idle_s": 4.0, "interval_s": 0.2}, beats[0]
    for payload in beats:
        registry.EVENTS["turn.heartbeat"].payload.model_validate(payload)

    # Approval pending -> waiting.
    events.clear()
    real_pending = server._session_pending_kind
    server._session_pending_kind = lambda sid: "approval.request"
    stop, thread = server._start_usage_ticker("hb", agent)
    time.sleep(0.5)
    stop.set(); thread.join()
    server._session_pending_kind = real_pending
    assert events and all(p["status"] == "waiting" for _, _, p in events), events

    # Not running -> silent; interval 0 -> no heartbeat thread at all.
    events.clear()
    server._sessions["hb"]["running"] = False
    stop, thread = server._start_usage_ticker("hb", agent)
    time.sleep(0.5)
    stop.set(); thread.join()
    assert events == [], events
    os.environ["HERMES_TURN_HEARTBEAT_S"] = "0"
    stop, thread = server._start_usage_ticker("hb", agent)
    assert type(thread).__name__ != "_JoinBoth", "off: upstream ticker unchanged"
    stop.set(); thread.join()
    os.environ["HERMES_TURN_HEARTBEAT_S"] = "0.2"
finally:
    server._emit = real_emit

# ── session.live_status ──
FIELDS = {"session_id", "session_key", "status", "running", "turn_started_at", "idle_s"}
idle_agent = SimpleNamespace(session_id="stored-1", get_activity_summary=lambda: {"seconds_since_activity": 2})
server._sessions["s1"] = session(agent=idle_agent, session_key="stored-1")
r = call("session.live_status", {"session_id": "s1"})["result"]
assert set(r) == FIELDS, r
assert r == {"session_id": "s1", "session_key": "stored-1", "status": "idle", "running": False,
             "turn_started_at": None, "idle_s": None}, r

s1 = server._sessions["s1"]
s1.update(running=True, inflight_turn={"started_at": 2000.0})
r = call("session.live_status", {"session_key": "stored-1"})["result"]
assert (r["session_id"], r["status"], r["running"], r["turn_started_at"], r["idle_s"]) == \
    ("s1", "working", True, 2000.0, 2.0), r

real_pending = server._session_pending_kind
server._session_pending_kind = lambda sid: "clarify.request"
assert call("session.live_status", {"session_id": "s1"})["result"]["status"] == "waiting"
server._session_pending_kind = real_pending

r = call("session.live_status", {"session_id": "gone", "session_key": "nope"})["result"]
assert r == {"session_id": "gone", "session_key": "nope", "status": "idle", "running": False,
             "turn_started_at": None, "idle_s": None}, r
s1["_finalized"] = True
assert call("session.live_status", {"session_id": "s1"})["result"]["status"] == "idle", "finalized is not live"
s1.pop("_finalized")

# Profile: omitted matches any; named must match.
other = str(server._profile_home("other"))
server._sessions["s2"] = session(profile_home=other, session_key="stored-2", running=True)
assert call("session.live_status", {"session_id": "s2"})["result"]["running"] is True
assert call("session.live_status", {"session_id": "s2", "profile": "other"})["result"]["running"] is True
assert call("session.live_status", {"session_key": "stored-2", "profile": "other"})["result"]["running"] is True
assert call("session.live_status", {"session_id": "s2", "profile": "default"})["result"]["running"] is False
assert call("session.live_status", {"session_id": "s1", "profile": "other"})["result"]["running"] is False

def code(resp):
    return (resp.get("error") or {}).get("code")
assert code(call("session.live_status", {})) == 4006
assert code(call("session.live_status", {"session_id": "s1", "profile": "missing"})) == 4064
assert code(call("session.live_status", {"session_id": "s1", "title": "x"})) == 4000, "unknown keys refused"

# ── gateway.capabilities ──
caps = call("gateway.capabilities", {})["result"]
assert caps["turn_heartbeat"] is True and caps["turn_heartbeat_s"] == 0.2 and caps["session_live_status"] is True, caps
assert "per_session_exclusive_submit" in caps
registry.check_result(registry.METHODS["gateway.capabilities"], caps)
os.environ["HERMES_TURN_HEARTBEAT_S"] = "0"
assert call("gateway.capabilities", {})["result"]["turn_heartbeat"] is False
# Importing the gateway points sys.stdout at stderr (stdout is its JSON-RPC channel).
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_interval()
    test_idle_seconds()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
