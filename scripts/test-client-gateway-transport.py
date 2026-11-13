#!/usr/bin/env python3
"""Client gateway transport and auth (task 1.1): bearer token, Origin, identity
routing, startup refusal, Acp-Connection-Id, and the compose service publishing
on 127.0.0.1 only. The socket checks run inside the pinned Hermes image."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import client_gateway_testkit as kit  # noqa: E402
from testkit import Checks  # noqa: E402


async def socket_checks(c: Checks) -> None:
    from client_gateway.auth import SettingsError, load_settings
    from fake_backend import FakeBackend

    placeholder = {"CLIENT_API_TOKEN": "change-me-generate-with-openssl-rand-hex-32"}
    for env, why in (({}, "unset"), ({"CLIENT_API_TOKEN": "short"}, "short"), (placeholder, "placeholder")):
        try:
            load_settings(env)
            c.fail(f"load_settings accepted a {why} token")
        except SettingsError as exc:
            c.expect("CLIENT_API_TOKEN" in str(exc), f"{why} token error names the variable: {exc}")
    s = load_settings({"CLIENT_API_TOKEN": kit.TOKEN, "CLIENT_API_ALLOWED_ORIGINS": "https://a.example/, ,"})
    c.expect(s.allowed_origins == frozenset({"https://a.example"}), f"origins parsed: {s.allowed_origins}")

    async with kit.serve(FakeBackend(), allowed_origins=frozenset({"https://ok.example"})) as url:
        st = kit.upgrade_status
        c.expect(await st(url, token=None) == 401, "no token gives 401")
        c.expect(await st(url, token="wrong" * 10) == 401, "wrong token gives 401")
        c.expect(await st(url, origin="https://evil.example") == 403, "unlisted Origin gives 403")
        c.expect(await st(url, origin="https://ok.example") == 101, "listed Origin is accepted")
        c.expect(await st(url, path="/acp/agents/nope") == 404, "unknown slug gives 404")
        c.expect(await st(url, path="/acp/agents/alpha") == 101, "known slug is accepted")
        c.expect(await st(url) == 101, "/acp with the token is accepted")

        cl = await kit.client(url)
        c.expect(bool(cl.connection_id) and len(cl.connection_id) >= 32, f"Acp-Connection-Id on 101: {cl.connection_id}")
        await cl.ws.ping()
        await cl.close()


def startup_refusal(c: Checks) -> None:
    """The module entry point exits non-zero with a message when the token is short."""
    out = subprocess.run(
        [sys.executable, "-m", "client_gateway.server"],
        cwd=str(ROOT / "compose" / "client-gateway"),
        env={"CLIENT_API_TOKEN": "short", "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    c.expect(out.returncode == 2, f"short token exits 2, got {out.returncode}")
    c.expect("at least 32" in out.stderr, f"short token message: {out.stderr.strip()[-200:]}")


def compose_checks(c: Checks) -> None:
    if not shutil.which("docker"):
        print("skip compose checks (no docker)")
        return
    if not (ROOT / ".env").exists():
        # compose interpolates the whole file, which needs .env's required keys.
        # ci.yml writes a stub .env and runs these; hermes-image.yml has none.
        print("skip compose checks (no .env)")
        return
    import compose_config

    services = compose_config.render("core,client-gateway", {"CLIENT_API_TOKEN": kit.TOKEN})["services"]
    svc = services.get("client-gateway")
    if not c.expect(svc is not None, "compose defines client-gateway under its profile"):
        return
    ports = svc.get("ports") or []
    c.expect(len(ports) == 1, f"one published port: {ports}")
    for p in ports:
        c.expect(p.get("host_ip") == "127.0.0.1", f"published on 127.0.0.1 only: {p}")
        c.expect(str(p.get("target")) == "8650", f"container port 8650: {p}")
    c.expect(svc.get("profiles") == ["client-gateway"], f"profile client-gateway: {svc.get('profiles')}")
    absent = compose_config.render("core", {"CLIENT_API_TOKEN": kit.TOKEN})["services"]
    c.expect("client-gateway" not in absent, "client-gateway is off without its profile")
    # An install that never set the token must still render (compose interpolates
    # every service, active or not).
    compose_config.render("core", drop=("CLIENT_API_TOKEN",), env={"CLIENT_API_TOKEN": ""})


def main() -> None:
    c = Checks()
    if kit.IN_IMAGE:
        kit.in_image_paths()
        asyncio.run(socket_checks(c))
        startup_refusal(c)
        c.finish("ok client-gateway transport (image)")
        return
    compose_checks(c)
    c.finish("ok client-gateway transport (host)")
    kit.run_in_image(__file__)


if __name__ == "__main__":
    main()
