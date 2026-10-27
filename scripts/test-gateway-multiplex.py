#!/usr/bin/env python3
"""Checks for HERMES_GATEWAY_TOPOLOGY=multiplex in the pinned Hermes image.

With the image present (or --image):

- gateway-compat-proxy.py, against a fake upstream: prefixes the path with
  /p/<profile>, keeps the query, method, body and Authorization, streams a
  response chunk by chunk (SSE), bridges a WebSocket, and answers 502 when the
  gateway is down.
- 019-pin-profile-gateways.sh under multiplex, on a synthetic HERMES_HOME:
  writes no standalone pins, generates the default profile's API_SERVER_KEY
  (mode 0600) once, parks a profile that never ran a gateway and leaves a
  running one served, does not re-park a profile unparked later, and warns
  about a profile still pinned standalone.

With --boot, also boots the image under s6 with that home and
HERMES_GATEWAY_TOPOLOGY=multiplex and checks the real listener: one gateway
serves the profiles at :8642/p/<profile>/, each with its own key, and the
proxy answers on 8643 and 8644. About a minute.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, "/opt/hermes")

# --- gateway-compat-proxy ---
spec = importlib.util.spec_from_file_location("compat_proxy", "/bootstrap/gateway-compat-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
assert proxy.parse_routes("8643=api-server, 8644=browser") == [(8643, "api-server"), (8644, "browser")]
try:
    proxy.parse_routes("x=api-server")
    raise SystemExit("FAIL bad route accepted")
except ValueError:
    pass

from aiohttp import ClientSession, web

release = asyncio.Event()

async def echo(request):
    return web.json_response({"method": request.method, "path": request.path, "query": request.query_string,
                              "auth": request.headers.get("Authorization"), "body": await request.text()})

async def sse(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    await resp.write(b"data: one\n\n")
    await release.wait()
    await resp.write(b"data: two\n\n")
    await resp.write_eof()
    return resp

async def ws(request):
    sock = web.WebSocketResponse()
    await sock.prepare(request)
    async for msg in sock:
        await sock.send_str("echo:" + msg.data)
    return sock

async def main():
    up = web.Application()
    up.router.add_route("*", "/p/api-server/v1/echo", echo)
    up.router.add_get("/p/api-server/v1/runs/r1/events", sse)
    up.router.add_get("/p/api-server/v1/browser-control/ws", ws)
    runner = web.AppRunner(up)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18642).start()
    task = asyncio.create_task(proxy.serve([(18643, "api-server")], "http://127.0.0.1:18642", "127.0.0.1"))
    down = asyncio.create_task(proxy.serve([(18644, "browser")], "http://127.0.0.1:18699", "127.0.0.1"))
    await asyncio.sleep(0.5)
    async with ClientSession() as s:
        async with s.post("http://127.0.0.1:18643/v1/echo?a=1", data="hi",
                          headers={"Authorization": "Bearer k"}) as r:
            assert r.status == 200, r.status
            got = await r.json()
        assert got == {"method": "POST", "path": "/p/api-server/v1/echo", "query": "a=1",
                       "auth": "Bearer k", "body": "hi"}, got
        async with s.get("http://127.0.0.1:18643/v1/runs/r1/events") as r:
            first = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 5)
            assert first == b"data: one\n\n", first  # arrived before the upstream finished
            release.set()
            rest = await asyncio.wait_for(r.content.read(), 5)
            assert rest == b"data: two\n\n", rest
        async with s.ws_connect("http://127.0.0.1:18643/v1/browser-control/ws") as sock:
            await sock.send_str("ping")
            msg = await asyncio.wait_for(sock.receive(), 5)
            assert msg.data == "echo:ping", msg
        async with s.get("http://127.0.0.1:18644/health") as r:
            assert r.status == 502, r.status
            assert (await r.json())["error"]["code"] == "gateway_unreachable"
    task.cancel()
    down.cancel()
    await runner.cleanup()

asyncio.run(main())
print("proxy ok")

# --- 019-pin-profile-gateways under multiplex ---
home = Path("/tmp/multiplex-home")
for name, state, cfg in (("api-server", "running", "model:\n  default: m\n"),
                         ("browser", "running", "model:\n  default: m\n"),
                         ("software-engineer", "running", "model:\n  default: m\n"),
                         ("orchestrator", None, "model:\n  default: m\n"),
                         ("idle", "stopped", "model:\n  default: m\n"),
                         ("pinned", None, "gateway:\n  standalone: true\n")):
    p = home / "profiles" / name
    p.mkdir(parents=True)
    (p / "SOUL.md").write_text("synthetic\n")
    (p / "config.yaml").write_text(cfg)
    if state:
        (p / "gateway_state.json").write_text(json.dumps({"desired_state": state}))
(home / "config.yaml").write_text("model:\n  default: m\n")
(home / ".env").write_text("API_SERVER_HOST=0.0.0.0")  # no trailing newline on purpose

script = Path("/bootstrap/019-pin-profile-gateways.sh").read_text()
body = script.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY\n", 1)[0]
env = {**os.environ, "HERMES_HOME": str(home), "HERMES_GATEWAY_TOPOLOGY": "multiplex"}

def run019():
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout

first = run019()
assert "topology: multiplex" in first, first
assert "pinned keeps gateway.standalone: true" in first, first
parked = {p.parent.name for p in home.glob("profiles/*/gateway.parked")}
assert parked == {"orchestrator", "idle", "pinned"}, parked
lines = (home / ".env").read_text().splitlines()
assert lines[0] == "API_SERVER_HOST=0.0.0.0", lines
keys = [l for l in lines if l.startswith("API_SERVER_KEY=")]
assert len(keys) == 1 and len(keys[0]) > 40, lines
assert stat.S_IMODE((home / ".env").stat().st_mode) in (0o600, 0o644), oct((home / ".env").stat().st_mode)

from hermes_yaml import safe_load
for name in ("api-server", "browser", "orchestrator"):
    c = safe_load((home / "profiles" / name / "config.yaml").read_text())
    assert "gateway" not in c and c["sessions"]["auto_prune"] is False, (name, c)

# Unparked by hand (`hermes -p orchestrator gateway start` removes the marker): stays unparked.
(home / "profiles" / "orchestrator" / "gateway.parked").unlink()
second = run019()
assert "parked" not in second.replace("gateway.parked", ""), second
assert not (home / "profiles" / "orchestrator" / "gateway.parked").exists()
assert (home / ".env").read_text().count("API_SERVER_KEY=") == 1, "key generated once"
record = json.loads((home / "gateway_topology.json").read_text())
assert record["parked_once"] == sorted(["api-server", "browser", "software-engineer", "orchestrator",
                                        "idle", "pinned"]), record

# A key the operator set is kept; a missing .env gets one with mode 0600.
(home / ".env").unlink()
run019()
assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600, oct((home / ".env").stat().st_mode)

from hermes_cli.profiles import profiles_to_serve
os.environ["HERMES_HOME"] = str(home)
served = sorted(name for name, _ in profiles_to_serve(True))
assert "orchestrator" in served and "idle" not in served and "pinned" not in served, served
print("019 ok")
print("image ok")
'''

BOOT_SEED = r'''#!/command/with-contenv sh
# Test-only: copy the synthetic home into the container filesystem.
cp -a /seed/. /opt/data/
chown -R hermes:hermes /opt/data
'''

BOOT_CHECK = r'''
import json, os, sys, time, urllib.request, urllib.error
from pathlib import Path

def get(url, key=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None

deadline = time.time() + 120
while time.time() < deadline and get("http://127.0.0.1:8642/health") != 200:
    time.sleep(2)
assert get("http://127.0.0.1:8642/health") == 200, "default listener up"
env = dict(l.split("=", 1) for l in Path("/opt/data/.env").read_text().splitlines() if "=" in l)
default_key = env["API_SERVER_KEY"]
while time.time() < deadline and get("http://127.0.0.1:8643/health") != 200:
    time.sleep(2)
checks = {
    "8642 /p/api-server/v1/models, api-server key": (get("http://127.0.0.1:8642/p/api-server/v1/models", "key-api-0123456789abcdef"), 200),
    "8642 /p/api-server/v1/models, browser key": (get("http://127.0.0.1:8642/p/api-server/v1/models", "key-browser-0123456789abcdef"), 401),
    "8642 /p/browser/v1/models, browser key": (get("http://127.0.0.1:8642/p/browser/v1/models", "key-browser-0123456789abcdef"), 200),
    "8642 /v1/models, default key": (get("http://127.0.0.1:8642/v1/models", default_key), 200),
    "8642 /p/orchestrator/health (parked)": (get("http://127.0.0.1:8642/p/orchestrator/health"), 404),
    "8643 /health": (get("http://127.0.0.1:8643/health"), 200),
    "8643 /v1/models, api-server key": (get("http://127.0.0.1:8643/v1/models", "key-api-0123456789abcdef"), 200),
    "8643 /v1/models, browser key": (get("http://127.0.0.1:8643/v1/models", "key-browser-0123456789abcdef"), 401),
    "8644 /v1/models, browser key": (get("http://127.0.0.1:8644/v1/models", "key-browser-0123456789abcdef"), 200),
}
bad = {k: v for k, v in checks.items() if v[0] != v[1]}
for k, (got, want) in checks.items():
    print(f"{'ok ' if got == want else 'BAD'} {k}: {got} (want {want})")
state = json.loads(Path("/opt/data/gateway_state.json").read_text())
print("served_profiles:", state.get("served_profiles"), "standalone_reason:", state.get("multiplex_standalone_reason"))
sys.exit(1 if bad else 0)
'''


def seed_home(path: Path) -> None:
    for name, key, state in (("api-server", "key-api-0123456789abcdef", "running"), ("browser", "key-browser-0123456789abcdef", "running"),
                             ("orchestrator", None, None)):
        p = path / "profiles" / name
        p.mkdir(parents=True)
        (p / "SOUL.md").write_text("synthetic\n")
        (p / "config.yaml").write_text("model:\n  default: m\n")
        if key:
            (p / ".env").write_text(f"API_SERVER_KEY={key}\nAPI_SERVER_ENABLED=true\n")
        if state:
            (p / "gateway_state.json").write_text(json.dumps({"desired_state": state}))
    (path / "config.yaml").write_text("model:\n  default: m\n")
    (path / ".env").write_text("API_SERVER_HOST=0.0.0.0\n")
    (path / "gateway_state.json").write_text(json.dumps({"desired_state": "running"}))


def boot() -> None:
    image = f"nousresearch/hermes-agent:{hermes_image.image_tag()}"
    hermes = ROOT / "compose" / "hermes"
    name = f"gateway-multiplex-test-{int(time.time())}"
    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "seed"
        seed_home(seed)
        init = Path(tmp) / "0011-seed"
        init.write_text(BOOT_SEED)
        init.chmod(0o755)
        cmd = ["docker", "run", "-d", "--name", name,
               "-e", "HERMES_GATEWAY_TOPOLOGY=multiplex", "-e", "HERMES_CONTROL_SOCKET_IN_TMP=1",
               "-v", f"{seed}:/seed:ro",
               "-v", f"{init}:/etc/cont-init.d/0011-seed:ro",
               "-v", f"{hermes / '019-pin-profile-gateways.sh'}:/etc/cont-init.d/019-pin-profile-gateways:ro",
               "-v", f"{hermes / 'gateway-compat-proxy.py'}:/bootstrap/gateway-compat-proxy.py:ro",
               "-v", f"{hermes / 's6' / 'gateway-compat-proxy'}:/etc/s6-overlay/s6-rc.d/gateway-compat-proxy:ro",
               "-v", f"{hermes / 's6' / 'user-contents' / 'gateway-compat-proxy'}:"
                     "/etc/s6-overlay/s6-rc.d/user/contents.d/gateway-compat-proxy:ro",
               image, "gateway", "run"]
        subprocess.run(cmd, check=True, capture_output=True)
        try:
            out = subprocess.run(["docker", "exec", name, "/opt/hermes/.venv/bin/python", "-c", BOOT_CHECK],
                                 capture_output=True, text=True, timeout=180)
            print(out.stdout, end="")
            if out.returncode != 0:
                logs = subprocess.run(["docker", "logs", "--tail", "60", name], capture_output=True, text=True)
                print(f"FAIL boot checks\n{out.stderr}\n--- container log ---\n{logs.stdout}{logs.stderr}",
                      file=sys.stderr)
                raise SystemExit(1)
            print(f"boot ok ({image})")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)
    if "--boot" in sys.argv[1:]:
        boot()


if __name__ == "__main__":
    main()
