#!/usr/bin/env python3
"""Checks for waking a parked MCP server (patch-mcp-wake.py / mcp_wake.py).

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the patch is applied in the image twice (idempotent) and then, end to end:
an MCP server that is down parks, comes up, and is revived by a wake request
well before its parked probe interval, without a second teardown.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

import mcp_wake  # noqa: E402
import hermes_image  # noqa: E402


def test_parked_retry_interval() -> None:
    interval = mcp_wake.parked_retry_interval
    expect(interval({}, 300) == 300.0, "unset keeps the Hermes default")
    expect(interval(None, 300) == 300.0, "a missing config keeps the default")
    expect(interval({"parked_retry_interval": 15}, 300) == 15.0, "a per-server value wins")
    expect(interval({"parked_retry_interval": "20"}, 300) == 20.0, "a numeric string is accepted")
    expect(interval({"parked_retry_interval": 1}, 300) == 10.0, "the floor is 10 s")
    expect(interval({"parked_retry_interval": "soon"}, 300) == 300.0, "junk falls back to the default")
    expect(interval({"parked_retry_interval": True}, 300) == 300.0, "a bool is not a number here")


def test_peer_parked() -> None:
    url = {"url": "http://host.docker.internal:62441/mcp", "timeout": 180}
    expect(not mcp_wake.peer_parked("boundary", url), "nothing parked yet")
    mcp_wake.note_initial_park("boundary", url)
    expect(mcp_wake.peer_parked("boundary", dict(url, timeout=60)), "same name and URL share the park")
    expect(not mcp_wake.peer_parked("boundary", {"url": "http://other:1/mcp"}), "another URL does not")
    expect(not mcp_wake.peer_parked("other", url), "another name does not")
    cmd = {"command": "npx", "args": ["srv"]}
    mcp_wake.note_initial_park("stdio", cmd)
    expect(mcp_wake.peer_parked("stdio", dict(cmd)), "same command and args share the park")
    expect(not mcp_wake.peer_parked("stdio", {"command": "npx", "args": ["other"]}), "other args do not")
    mcp_wake._peer_parks[("boundary", repr(url["url"]))] -= mcp_wake.PEER_PARK_WINDOW_SECONDS + 1
    expect(not mcp_wake.peer_parked("boundary", url), "an old park has expired")


def test_valid_name() -> None:
    for name in ("boundary", "caldav-work", "a.b_c", "x"):
        expect(mcp_wake.valid_name(name), f"{name!r} is a valid server name")
    for name in ("", ".hidden", "../etc", "a/b", "a b", "x" * 129):
        expect(not mcp_wake.valid_name(name), f"{name!r} is refused")


IMAGE_DRIVER = r'''
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

def run_patch():
    out = subprocess.run([sys.executable, "/bootstrap/patch-mcp-wake.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    return out.stdout

first = run_patch()
for label in ("parked retry interval", "wake watcher", "reconnect endpoint"):
    assert f"patched {label}" in first, first
second = run_patch()
for label in ("parked retry interval", "wake watcher", "reconnect endpoint"):
    assert f"already applied ({label})" in second, second

root = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(root)
sys.path.insert(0, "/opt/hermes")

records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.INFO)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
# keepalive 5 s notices the server quitting; parked probe 120 s, so only a wake revives in time.
server_cfg = {"url": f"http://127.0.0.1:{port}/mcp", "connect_timeout": 30, "timeout": 5,
              "keepalive_interval": 5, "parked_retry_interval": 120}
try:  # Hermes v0.22 has ruamel behind hermes_yaml and no PyYAML
    from hermes_yaml import safe_dump as yaml_dump
except ImportError:
    from yaml import safe_dump as yaml_dump
(root / "config.yaml").write_text(yaml_dump({"mcp_servers": {"wake-test": server_cfg}}))
for profile, servers in (("api-server", {"wake-test": server_cfg}), ("browser", {})):
    (root / "profiles" / profile).mkdir(parents=True)
    (root / "profiles" / profile / "config.yaml").write_text(yaml_dump({"mcp_servers": servers}))

from tools import mcp_wake
assert mcp_wake.hermes_root() == root, mcp_wake.hermes_root()
assert mcp_wake.handle_reconnect("../x")[1] == 400
assert mcp_wake.handle_reconnect("nope")[1] == 404

# Endpoint: route registered, auth enforced, 202 names the configuring profiles.
from gateway.platforms import api_server
cls = next(c for c in vars(api_server).values()
           if isinstance(c, type) and "_handle_mcp_reconnect" in vars(c))
import inspect
assert '"/v1/mcp/servers/{name}/reconnect"' in inspect.getsource(cls._http_route_table)
request = SimpleNamespace(match_info={"name": "wake-test"}, headers={})
denied = api_server.web.json_response({"error": "no"}, status=401)
resp = asyncio.run(cls._handle_mcp_reconnect(SimpleNamespace(_check_auth=lambda r: denied), request))
assert resp.status == 401, resp.status
assert not (root / "mcp-wake").exists(), "an unauthenticated call must not request a wake"

# End to end: server down -> parked; up -> woken by request, not by the 120 s probe.
from tools.mcp_tool_common import _core
from tools.mcp_tool_discovery import register_mcp_servers, get_registered_mcp_server_names
register_mcp_servers({"wake-test": server_cfg})

def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise SystemExit(f"FAIL timed out waiting for {what}\n" + "\n".join(records[-30:]))

def task():
    with _core._lock:
        return next((s for s in _core._servers.values() if s.name == "wake-test"), None)

def parked():
    t = task()
    return t is not None and getattr(t, "_assistant_parked", False)

from mcp.server.mcpserver import MCPServer
import uvicorn
app = MCPServer("wake-test")
@app.tool()
def echo(text: str) -> str:
    return text

def serve():
    srv = uvicorn.Server(uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port,
                                        log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    wait_for(lambda: srv.started, 10, "test MCP server")
    return srv

def wake_and_wait(phase):
    started = time.monotonic()
    resp = asyncio.run(cls._handle_mcp_reconnect(SimpleNamespace(_check_auth=lambda r: None), request))
    assert resp.status == 202, resp.status
    body = json.loads(resp.text)
    assert body["profiles"] == ["default", "api-server"], body
    # This process holds the parked connection, so the endpoint wakes it itself.
    assert body["woken"] == 1, body
    wait_for(lambda: "wake-test" in get_registered_mcp_server_names(), 15, f"tools after wake ({phase})")
    elapsed = time.monotonic() - started
    assert elapsed < 10, elapsed
    # The same request must not tear the fresh session down again.
    session = task().session
    time.sleep(3)
    assert task().session is session, f"a woken connection was signalled twice ({phase})"
    print(f"{phase}: woken in {elapsed:.1f}s")

# A: server down when Hermes starts -> parked; up -> woken by request.
wait_for(parked, 60, "park (down at start)")
assert "wake-test" not in get_registered_mcp_server_names()
srv = serve()
wake_and_wait("down at start")

# B: connected server quits -> parked again; back -> woken by a new request.
srv.should_exit = True
wait_for(parked, 120, "park (server quit)")
assert "wake-test" not in get_registered_mcp_server_names()
assert any("every 120s" in r for r in records), "park log names the per-server interval"
time.sleep(0.05)  # a new request needs a newer mtime than the first
srv = serve()
wake_and_wait("server quit")
assert sum("woken from parked state by request" in r for r in records) == 2

# C: a server down for two profiles runs the initial ladder once; the second profile parks
# after one attempt.
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    down_port = s.getsockname()[1]
peer_cfg = {"url": f"http://127.0.0.1:{down_port}/mcp", "connect_timeout": 30, "timeout": 5}
took = {}
for profile in ("api-server", "browser"):
    token = set_hermes_home_override(str(root / "profiles" / profile))
    try:
        started = time.monotonic()
        register_mcp_servers({"peer-test": peer_cfg})
        took[profile] = time.monotonic() - started
    finally:
        reset_hermes_home_override(token)
assert sum("'peer-test' failed initial connection after" in r for r in records) == 1, records[-10:]
assert sum("'peer-test' failed initial connection and another profile" in r for r in records) == 1, records[-10:]
assert took["browser"] < 2 < took["api-server"], took
print(f"peer park: {took['api-server']:.1f}s then {took['browser']:.1f}s")
print("image ok")
sys.stdout.flush()
os._exit(0)  # skip MCP loop / uvicorn thread teardown
'''


def main() -> None:
    test_parked_retry_interval()
    test_peer_parked()
    test_valid_name()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
