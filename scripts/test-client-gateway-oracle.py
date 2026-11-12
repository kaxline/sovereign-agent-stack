#!/usr/bin/env python3
"""Conformance oracle (task 1.8): one ACP scenario against Hermes' own `hermes acp`
and against the client gateway in front of a real `hermes dashboard`, both in
the pinned image with a stub model, no network and throwaway Hermes homes on
the container's filesystem (a macOS bind mount breaks gateway.sock).

The scenario: initialize, session/new, a prompt with a fixed stub reply,
session/list, a prompt cancelled mid-stream, then session/load from a second
connection. Ids and timings are dropped; the shared subset must match, except
for the differences listed in compose/client-gateway/oracle-known-differences.md.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import client_gateway_testkit as kit  # noqa: E402
from testkit import Checks  # noqa: E402

KNOWN = ROOT / "compose" / "client-gateway" / "oracle-known-differences.md"
STUB_PORT = 9000
DASH_PORT = 9119
DASH_USER, DASH_PASSWORD = "oracle", "oracle-password-" + "p" * 16
REPLY = "STUB-REPLY: hello from the stub model."


def known_differences() -> set[str]:
    """Keys in backticks at the start of each list item: `- `key` — why (link)`."""
    return set(re.findall(r"^- `([a-z0-9_.]+)`", KNOWN.read_text(), re.M))


def hermes_home(path: str) -> dict:
    env = {**os.environ, "HERMES_HOME": path, "HOME": path}
    os.makedirs(path, exist_ok=True)
    code = (
        "import sys; sys.path.insert(0, '/opt/hermes')\n"
        "from hermes_cli.config import set_config_value\n"
        f"for k, v in [('model.provider', 'custom'), ('model.default', 'stub-model'),\n"
        f"             ('model.base_url', 'http://127.0.0.1:{STUB_PORT}/v1'), ('model.api_key', 'sk-stub')]:\n"
        "    set_config_value(k, v)\n"
    )
    subprocess.run([sys.executable, "-c", code], env=env, check=True, stdout=subprocess.DEVNULL)
    return env


def text(cl, sid: str, kind: str) -> str:
    return "".join(
        u["update"]["content"].get("text", "")
        for u in cl.updates
        if u["sessionId"] == sid and u["update"]["sessionUpdate"] == kind
    )


async def scenario(connect) -> dict:
    """Run the scenario; ``connect()`` opens a new ACP connection. Returns the
    normalised observations, keyed by name."""
    obs: dict = {}
    a = await connect()
    init = await a.initialize()
    caps = init.get("agentCapabilities", {})
    obs["initialize.protocol_version"] = init.get("protocolVersion")
    obs["initialize.load_session"] = caps.get("loadSession")
    obs["initialize.session_list"] = "list" in (caps.get("sessionCapabilities") or {})
    obs["initialize.session_resume"] = "resume" in (caps.get("sessionCapabilities") or {})
    obs["initialize.session_close"] = "close" in (caps.get("sessionCapabilities") or {})

    sid = (await a.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=120))["sessionId"]
    obs["session_new.has_id"] = bool(sid)

    r = await a.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "Say hi"}]}, timeout=120)
    obs["prompt.stop_reason"] = r.get("stopReason")
    obs["prompt.reply"] = text(a, sid, "agent_message_chunk").strip()
    obs["prompt.usage_update"] = any(
        u["update"]["sessionUpdate"] == "usage_update" for u in a.updates if u["sessionId"] == sid
    )

    lst = await a.request("session/list", {}, timeout=60)
    obs["session_list.contains_new"] = sid in [s["sessionId"] for s in lst.get("sessions", [])]

    _, slow = a.start_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "SLOW reply"}]})
    await asyncio.sleep(5)
    await a.notify("session/cancel", {"sessionId": sid})
    try:
        r = await asyncio.wait_for(slow, 60)
        obs["cancel.stop_reason"] = r.get("stopReason")
    except Exception as exc:  # noqa: BLE001
        obs["cancel.stop_reason"] = f"error {exc}"

    b = await connect()
    await b.initialize()
    try:
        await b.request("session/load", {"sessionId": sid, "cwd": "/tmp", "mcpServers": []}, timeout=120)
        obs["load.replays_user"] = "Say hi" in text(b, sid, "user_message_chunk")
        obs["load.replays_agent"] = REPLY in text(b, sid, "agent_message_chunk")
    except Exception as exc:  # noqa: BLE001
        obs["load.replays_user"] = obs["load.replays_agent"] = f"error {exc}"
    try:
        await b.request("session/close", {"sessionId": sid}, timeout=30)
        obs["session_close.ok"] = True
    except Exception as exc:  # noqa: BLE001
        obs["session_close.ok"] = f"error {getattr(exc, 'code', exc)}"
    obs["frames.valid"] = not (a.frame_errors or b.frame_errors)
    if a.frame_errors or b.frame_errors:
        print("frame errors:", (a.frame_errors + b.frame_errors)[:3])
    for cl in (a, b):
        await cl.close()
    return obs


async def run_oracle() -> dict:
    from acp_client import AcpClient, StdioTransport
    from client_gateway.validate import Validator

    env = hermes_home("/tmp/hh-oracle")
    stderr = open("/tmp/hermes-acp.log", "w")
    conns: list = []

    async def connect():
        # hermes acp is one connection per process; both share the same home.
        t = await StdioTransport.spawn("hermes", "acp", env=env, stderr=stderr)
        cl = AcpClient(t, validator=Validator())
        cl.start()
        conns.append(t)
        return cl

    try:
        return await scenario(connect)
    finally:
        for t in conns:
            await t.close()


async def run_gateway() -> dict:
    from client_gateway.auth import Settings
    from client_gateway.hermes.backend import HermesBackend

    env = hermes_home("/tmp/hh-gw")
    env.update({
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": DASH_USER,
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": DASH_PASSWORD,
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "s" * 64,
    })
    log = open("/tmp/hermes-dashboard.log", "w")
    dash = subprocess.Popen(
        ["hermes", "dashboard", "--host", "0.0.0.0", "--port", str(DASH_PORT), "--no-open"],
        env=env, cwd="/tmp/hh-gw", stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        settings = Settings(
            token=kit.TOKEN, hermes_url=f"http://127.0.0.1:{DASH_PORT}",
            dashboard_user=DASH_USER, dashboard_password=DASH_PASSWORD,
        )
        backend = HermesBackend(settings)
        async with kit.serve(backend) as url:
            deadline = time.monotonic() + 120
            while not backend.upstream.connected.is_set():
                if dash.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the dashboard did not come up; see /tmp/hermes-dashboard.log")
                await asyncio.sleep(0.5)
            return await scenario(lambda: kit.client(url))
    finally:
        dash.terminate()
        try:
            dash.wait(15)
        except subprocess.TimeoutExpired:
            dash.kill()


async def checks(c: Checks) -> None:
    stub = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "lib" / "stub_llm.py"), str(STUB_PORT)],
        stderr=open("/tmp/stub.log", "w"),
    )
    try:
        oracle = await run_oracle()
        try:
            gateway = await run_gateway()
        except Exception:
            print(Path("/tmp/hermes-dashboard.log").read_text()[-3000:])
            raise
    finally:
        stub.terminate()
    known = known_differences()
    print("observation                    hermes acp        gateway")
    for key in sorted(set(oracle) | set(gateway)):
        o, g = oracle.get(key), gateway.get(key)
        mark = "" if o == g else ("  (known)" if key in known else "  DIFFERS")
        print(f"{key:30} {str(o)[:16]:17} {str(g)[:16]}{mark}")
        if o != g and key not in known:
            c.fail(f"{key}: hermes acp {o!r}, gateway {g!r}")
    for key in known:
        if oracle.get(key) == gateway.get(key) and key in oracle:
            print(f"note: known difference {key} no longer differs; drop it from {KNOWN.name}")
    # The gateway's own answers must be right, whatever the oracle says.
    c.expect(gateway.get("prompt.stop_reason") == "end_turn", f"gateway prompt: {gateway.get('prompt.stop_reason')}")
    c.expect(gateway.get("prompt.reply") == REPLY, f"gateway reply: {gateway.get('prompt.reply')!r}")
    c.expect(gateway.get("cancel.stop_reason") == "cancelled", f"gateway cancel: {gateway.get('cancel.stop_reason')}")
    c.expect(gateway.get("load.replays_agent") is True, "gateway load replays")
    c.expect(gateway.get("frames.valid") is True, "gateway frames validate")


def main() -> None:
    c = Checks()
    if kit.IN_IMAGE:
        kit.in_image_paths()
        asyncio.run(checks(c))
        c.finish("ok client-gateway oracle (image)")
        return
    if not KNOWN.is_file():
        c.fail(f"{KNOWN} is missing")
    c.finish("ok client-gateway oracle (host)")
    # Hermes' own user, and no network: the stub model is the only endpoint.
    kit.run_in_image(__file__, docker_args=("--network", "none", "--user", "1000:1000"))


if __name__ == "__main__":
    main()
