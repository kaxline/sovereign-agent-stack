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
  about a profile still pinned standalone. It writes the resolved topology to
  HERMES_GATEWAY_TOPOLOGY_FILE; an invalid HERMES_GATEWAY_TOPOLOGY warns and
  writes standalone even with HERMES_PIN_STANDALONE_GATEWAYS=0.
- s6/gateway-compat-proxy run and finish read /run/hermes-gateway-topology:
  standalone, missing or empty leaves the slot down (run 0, finish 125);
  multiplex lets finish restart it (0).

With --boot, also boots the image under s6 with that home and
HERMES_GATEWAY_TOPOLOGY=multiplex and checks the real listener: one gateway
serves the profiles at :8642/p/<profile>/, each with its own key, and the
proxy answers on 8643 and 8644. Then boots it again with an invalid
HERMES_GATEWAY_TOPOLOGY and HERMES_PIN_STANDALONE_GATEWAYS=0 and checks that
the container runs standalone: 019 warns and writes standalone, the proxy slot
stays down, and the api-server profile's own gateway answers on 8643. About
two minutes.
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

async def slow(request):
    await asyncio.sleep(1.5)
    return web.json_response({"ok": True})

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
    up.router.add_get("/p/api-server/health/detailed", slow)
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
        # A client that gives up first (the WebUI's 2 s health probe) is not a proxy error.
        import logging
        errors = []
        class Grab(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    errors.append(record.getMessage())
        logging.getLogger("aiohttp").addHandler(Grab())
        try:
            await s.get("http://127.0.0.1:18643/health/detailed", timeout=__import__("aiohttp").ClientTimeout(total=0.3))
            raise SystemExit("FAIL slow request did not time out")
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1.8)
        assert not errors, errors
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
topology_file = Path("/tmp/multiplex-topology")
env = {**os.environ, "HERMES_HOME": str(home), "HERMES_GATEWAY_TOPOLOGY": "multiplex",
       "HERMES_GATEWAY_TOPOLOGY_FILE": str(topology_file)}

def run019(env=env):
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout

first = run019()
assert "topology: multiplex" in first, first
assert topology_file.read_text() == "multiplex\n", topology_file.read_text()
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

c = safe_load((home / "config.yaml").read_text())
assert c["dashboard"]["ws_ping_timeout"] == 120, c
assert "dashboard" not in safe_load((home / "profiles" / "browser" / "config.yaml").read_text())

# Unparked by hand (`hermes -p orchestrator gateway start` removes the marker): stays unparked.
(home / "profiles" / "orchestrator" / "gateway.parked").unlink()
second = run019()
assert "parked" not in second.replace("gateway.parked", ""), second
assert not (home / "profiles" / "orchestrator" / "gateway.parked").exists()
assert (home / ".env").read_text().count("API_SERVER_KEY=") == 1, "key generated once"
record = json.loads((home / "gateway_topology.json").read_text())
assert record["parked_once"] == sorted(["api-server", "browser", "software-engineer", "orchestrator",
                                        "idle", "pinned"]), record

# A ping timeout set by hand is kept.
(home / "config.yaml").write_text("dashboard:\n  ws_ping_timeout: 45\n")
assert "ws_ping_timeout" not in run019()
assert safe_load((home / "config.yaml").read_text())["dashboard"]["ws_ping_timeout"] == 45

# A key the operator set is kept; a missing .env gets one with mode 0600.
(home / ".env").unlink()
run019()
assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600, oct((home / ".env").stat().st_mode)

from hermes_cli.profiles import profiles_to_serve
os.environ["HERMES_HOME"] = str(home)
served = sorted(name for name, _ in profiles_to_serve(True))
assert "orchestrator" in served and "idle" not in served and "pinned" not in served, served

# An invalid topology warns and means standalone, whatever the older pin says.
bad_home = Path("/tmp/bad-topology-home")
bad_home.mkdir()
(bad_home / "config.yaml").write_text("model:\n  default: m\n")
out = run019({**os.environ, "HERMES_HOME": str(bad_home), "HERMES_GATEWAY_TOPOLOGY": "Multi-plex",
              "HERMES_PIN_STANDALONE_GATEWAYS": "0", "HERMES_GATEWAY_TOPOLOGY_FILE": str(topology_file)})
assert "warn: HERMES_GATEWAY_TOPOLOGY='multi-plex'" in out and "topology: standalone" in out, out
assert topology_file.read_text() == "standalone\n", topology_file.read_text()
assert not (bad_home / ".env").exists(), "standalone generates no default key"
print("019 ok")

# --- s6/gateway-compat-proxy run and finish read 019's file ---
run_file = Path("/run/hermes-gateway-topology")
s6_dir = "/bootstrap/s6/gateway-compat-proxy"
def slot(script):
    return subprocess.run(["sh", f"{s6_dir}/{script}"], capture_output=True, text=True).returncode
for content in (None, "", "standalone\n", "bogus\n"):
    run_file.unlink(missing_ok=True)
    if content is not None:
        run_file.write_text(content)
    assert (slot("run"), slot("finish")) == (0, 125), content
run_file.write_text("multiplex\n")
assert slot("finish") == 0
run_file.unlink()
print("s6 slot ok")
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

# Invalid HERMES_GATEWAY_TOPOLOGY with HERMES_PIN_STANDALONE_GATEWAYS=0: standalone.
BOOT_CHECK_STANDALONE = r'''
import subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None

deadline = time.time() + 120
while time.time() < deadline and get("http://127.0.0.1:8643/health") != 200:
    time.sleep(2)
up = subprocess.run(["/command/s6-svstat", "-o", "up", "/run/service/gateway-compat-proxy"],
                    capture_output=True, text=True)
checks = {
    "/run/hermes-gateway-topology": (Path("/run/hermes-gateway-topology").read_text(), "standalone\n"),
    "gateway-compat-proxy up": (up.stdout.strip(), "false"),
    "8643 /health (api-server's own gateway)": (get("http://127.0.0.1:8643/health"), 200),
}
bad = {k: v for k, v in checks.items() if v[0] != v[1]}
for k, (got, want) in checks.items():
    print(f"{'ok ' if got == want else 'BAD'} {k}: {got!r} (want {want!r})")
sys.exit(1 if bad else 0)
'''


def seed_home(path: Path, standalone: bool = False) -> None:
    for name, key, state, port in (("api-server", "key-api-0123456789abcdef", "running", 8643),
                                   ("browser", "key-browser-0123456789abcdef", "running", 8644),
                                   ("orchestrator", None, None, None)):
        p = path / "profiles" / name
        p.mkdir(parents=True)
        (p / "SOUL.md").write_text("synthetic\n")
        (p / "config.yaml").write_text("model:\n  default: m\n")
        if key:
            port_line = f"API_SERVER_PORT={port}\nAPI_SERVER_HOST=0.0.0.0\n" if standalone else ""
            (p / ".env").write_text(f"API_SERVER_KEY={key}\nAPI_SERVER_ENABLED=true\n{port_line}")
        if state:
            (p / "gateway_state.json").write_text(json.dumps({"desired_state": state}))
    (path / "config.yaml").write_text("model:\n  default: m\n")
    (path / ".env").write_text("API_SERVER_HOST=0.0.0.0\n")
    (path / "gateway_state.json").write_text(json.dumps({"desired_state": "running"}))


def boot(env: list[str], check: str, standalone: bool = False, log_has: str = "") -> None:
    image = f"nousresearch/hermes-agent:{hermes_image.image_tag()}"
    hermes = ROOT / "compose" / "hermes"
    name = f"gateway-multiplex-test-{int(time.time())}"
    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "seed"
        seed_home(seed, standalone)
        init = Path(tmp) / "0011-seed"
        init.write_text(BOOT_SEED)
        init.chmod(0o755)
        cmd = ["docker", "run", "-d", "--name", name,
               *[a for e in env for a in ("-e", e)], "-e", "HERMES_CONTROL_SOCKET_IN_TMP=1",
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
            out = subprocess.run(["docker", "exec", name, "/opt/hermes/.venv/bin/python", "-c", check],
                                 capture_output=True, text=True, timeout=180)
            print(out.stdout, end="")
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            if log_has and log_has not in logs.stdout + logs.stderr:
                print(f"BAD container log has no {log_has!r}")
                out.returncode = 1
            if out.returncode != 0:
                logs = subprocess.run(["docker", "logs", "--tail", "60", name], capture_output=True, text=True)
                print(f"FAIL boot checks\n{out.stderr}\n--- container log ---\n{logs.stdout}{logs.stderr}",
                      file=sys.stderr)
                raise SystemExit(1)
            print(f"boot ok ({image}, {' '.join(env)})")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)
    if "--boot" in sys.argv[1:]:
        boot(["HERMES_GATEWAY_TOPOLOGY=multiplex"], BOOT_CHECK)
        boot(["HERMES_GATEWAY_TOPOLOGY=bogus", "HERMES_PIN_STANDALONE_GATEWAYS=0"], BOOT_CHECK_STANDALONE,
             standalone=True, log_has="HERMES_GATEWAY_TOPOLOGY='bogus' is not standalone or multiplex")


if __name__ == "__main__":
    main()
