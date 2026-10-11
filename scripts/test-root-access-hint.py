#!/usr/bin/env python3
"""Assert the Hermes environment hint switches on the app-managed root flags.

Runs `docker compose config` with and without CONTEXT_ROOTS_APP_MANAGED,
CODING_ROOTS_APP_MANAGED and CODING_PATH_ALIASES, and checks the rendered
HERMES_ENVIRONMENT_HINT and opencode-mcp environment. Needs the same stub
files as the other compose checks (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAGS = ("CONTEXT_ROOTS_APP_MANAGED", "CODING_ROOTS_APP_MANAGED", "CODING_PATH_ALIASES")
ALIASES = "/opt/projects/foo=/Users/you/code/foo;/opt/projects/bar=/srv/bar"

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    if not cond:
        failures += 1
        print(f"FAIL {msg}", file=sys.stderr)


def services(extra: dict[str, str]) -> dict:
    env = os.environ.copy()
    for key in FLAGS:
        env.pop(key, None)
    env.update(extra)
    env["COMPOSE_PROFILES"] = "core,coding"
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(1)
    return json.loads(proc.stdout)["services"]


def env_of(service: dict) -> dict:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return raw
    return dict(item.split("=", 1) for item in raw if "=" in item)


def main() -> int:
    dev = services({})
    hint = env_of(dev["hermes"])["HERMES_ENVIRONMENT_HINT"]
    check("make context-root-add DIR=<absolute-path>" in hint, "dev: context-root command missing")
    check("APP_MANAGED" not in hint, "dev: managed text leaked into the hint")
    check("Project folders that are also host folders" not in hint, "dev: alias sentence without aliases")
    check("make coding-root-add" not in hint, "dev: coding access should defer to the needs_access hint")
    check("follow the hint in that result" in hint, "dev: needs_access deferral missing")
    mcp = env_of(dev["opencode-mcp"])
    check(not mcp.get("CODING_ROOTS_APP_MANAGED"), "dev: opencode-mcp flag should be empty")
    check(not mcp.get("CODING_PATH_ALIASES"), "dev: opencode-mcp aliases should be empty")

    managed = services({
        "CONTEXT_ROOTS_APP_MANAGED": "1",
        "CODING_ROOTS_APP_MANAGED": "1",
        "CODING_PATH_ALIASES": ALIASES,
    })
    hint = env_of(managed["hermes"])["HERMES_ENVIRONMENT_HINT"]
    check("make " not in hint, "managed: hint still names a make target")
    check("the app will ask the user to grant access to it" in hint, "managed: context sentence missing")
    check("(CONTEXT_ROOTS_APP_MANAGED=1)." in hint, "managed: flag wrapper did not render")
    check(f"(Hermes path=host path): {ALIASES}." in hint, "managed: alias sentence missing")
    mcp = env_of(managed["opencode-mcp"])
    check(mcp.get("CODING_ROOTS_APP_MANAGED") == "1", "managed: opencode-mcp flag not passed")
    check(mcp.get("CODING_PATH_ALIASES") == ALIASES, "managed: opencode-mcp aliases not passed")

    if failures:
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
