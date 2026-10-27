#!/usr/bin/env python3
"""Checks for the v0.22 gateway topology pieces in the pinned Hermes image.

With the image present (or --image):

- 019-pin-profile-gateways.sh, run against a synthetic HERMES_HOME, writes
  gateway.standalone: true on named profiles and sessions.auto_prune: false on
  every profile, leaves values set by hand alone, and Hermes' own boot
  reconcile (dry run) then starts each standalone profile from its own intent
  and folds nothing into the root gateway.
- patch-control-socket-tmp.py applies twice (idempotent); with
  HERMES_CONTROL_SOCKET_IN_TMP=1 a real control server binds under /tmp even
  with a stale gateway.sock in the home, and both the legacy client and the
  runtime discovery client (gateway --replace) reach it through the pointer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, "/opt/hermes")

# --- 019-pin-profile-gateways ---
home = Path("/tmp/topology-home")
for name in ("api-server", "browser", "idle", "manual"):
    p = home / "profiles" / name
    p.mkdir(parents=True)
    (p / "SOUL.md").write_text("synthetic\n")
    (p / "config.yaml").write_text("model:\n  default: m\n")
(home / "config.yaml").write_text("model:\n  default: m\n")
(home / "profiles" / "manual" / "config.yaml").write_text("gateway:\n  standalone: false\nsessions:\n  auto_prune: true\n")
for name, state in (("api-server", "running"), ("browser", "running"), ("idle", "stopped")):
    (home / "profiles" / name / "gateway_state.json").write_text(json.dumps({"desired_state": state}))

script = Path("/bootstrap/019-pin-profile-gateways.sh").read_text()
body = script.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY\n", 1)[0]
env = {**os.environ, "HERMES_HOME": str(home)}
for _ in range(2):
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
assert out.stdout == "[019-pin-profile-gateways] topology: standalone\n", "second run changes nothing: " + out.stdout
assert not list(home.glob("profiles/*/gateway.parked")), "standalone parks nothing"
assert not (home / "gateway_topology.json").exists()

from hermes_yaml import safe_load
cfg = lambda p: safe_load((p / "config.yaml").read_text())
for name in ("api-server", "browser", "idle"):
    c = cfg(home / "profiles" / name)
    assert c["gateway"]["standalone"] is True and c["sessions"]["auto_prune"] is False, (name, c)
manual = cfg(home / "profiles" / "manual")
assert manual["gateway"]["standalone"] is False and manual["sessions"]["auto_prune"] is True, manual
root = cfg(home)
assert root["sessions"]["auto_prune"] is False and "gateway" not in root, root

from hermes_cli.container_boot import reconcile_profile_gateways
actions = {a.profile: a for a in reconcile_profile_gateways(
    hermes_home=home, scandir=Path("/tmp/scandir"), dry_run=True, container_argv=[])}
assert actions["api-server"].action == "started" and actions["browser"].action == "started", actions
assert actions["idle"].action == "registered", actions
assert not any(a.folded_into_root for a in actions.values()), actions

# --- patch-control-socket-tmp ---
for want in ("patched", "already applied"):
    out = subprocess.run([sys.executable, "/bootstrap/patch-control-socket-tmp.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    assert out.stdout.count(want) == 2, out.stdout

os.environ["HERMES_CONTROL_SOCKET_IN_TMP"] = "1"
gw_home = Path("/tmp/gw-home")
gw_home.mkdir(mode=0o700)
(gw_home / "gateway.sock").write_text("")  # stale leftover; on virtiofs it cannot even be unlinked

from gateway import control_socket as cs
from hermes_cli.gateway_runtime_discovery import query_identify

loop = asyncio.new_event_loop()
server = cs.GatewayControlServer(gw_home)
assert loop.run_until_complete(server.start()), "control server started"
threading.Thread(target=loop.run_forever, daemon=True).start()

bind = Path(f"/tmp/hermes-gw-{cs._home_hash(gw_home.resolve())}/control.sock")
assert stat.S_ISSOCK(bind.lstat().st_mode), bind
assert stat.S_IMODE(bind.parent.lstat().st_mode) == 0o700, oct(bind.parent.lstat().st_mode)
pointer = gw_home / "gateway.sock.path"
assert pointer.read_text() == str(bind), pointer.read_text()
assert stat.S_IMODE(pointer.lstat().st_mode) == 0o600, oct(pointer.lstat().st_mode)
assert cs.resolve_client_socket_path(gw_home) == bind
assert cs.identify_gateway(gw_home, timeout=5), "legacy client reaches the gateway"
assert query_identify(gw_home, timeout=5), "runtime discovery reaches the gateway"

asyncio.run_coroutine_threadsafe(server.stop(), loop).result(5)
os.write(1, b"image ok\n")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
