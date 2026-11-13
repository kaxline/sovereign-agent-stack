#!/usr/bin/env python3
"""HERMES_API_SERVER_PORT moves the api-server gateway's host port only.

Inside the container the gateway stays on 8643: the hermes healthcheck, the
gateway-compat-proxy routes and in-network clients all use 8643. The rendered
config must publish <port>:8643 and hand hermes-api-bootstrap 8643, whatever
the variable says. Needs a .env that satisfies compose interpolation, as CI
writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402
from testkit import Checks  # noqa: E402

checks = Checks()


def env_list(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(e.split("=", 1) for e in env)
    return {k: "" if v is None else str(v) for k, v in env.items()}


for port in ("8643", "18643"):
    services = compose_config.render("core", {"HERMES_API_SERVER_PORT": port})["services"]
    boot = env_list(services["hermes-api-bootstrap"])
    checks.expect(boot.get("HERMES_API_SERVER_PORT") == "8643",
                  f"HERMES_API_SERVER_PORT={port}: hermes-api-bootstrap gets "
                  f"{boot.get('HERMES_API_SERVER_PORT')!r} as the in-container port, want '8643'")
    published = [(p.get("published"), p.get("target")) for p in services["hermes"].get("ports") or []]
    checks.expect((port, 8643) in published,
                  f"HERMES_API_SERVER_PORT={port}: hermes publishes {published}, want ({port!r}, 8643)")

checks.finish()
