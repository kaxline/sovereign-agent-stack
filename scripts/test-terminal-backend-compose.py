#!/usr/bin/env python3
"""Assert ssh and local terminal backends mount the workspace on different services.

Runs `docker compose config` twice. It does not print secret values.
The override file is not loaded, so machine-specific binds stay out of the check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KNOWN_OPT = {"/opt/projects", "/opt/voice", "/opt/memory", "/opt/skills"}
KEY_TARGET = "/opt/hermes-ssh/id_ed25519"
KEY_INIT = "/etc/cont-init.d/09-install-worker-key"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)


def load_config(backend: str) -> dict:
    env = os.environ.copy()
    env["COMPOSE_PROFILES"] = "core"
    env["HERMES_TERMINAL_BACKEND"] = backend
    proc = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "docker compose config failed").strip()
        fail(f"{backend}: {err}")
        raise SystemExit(1)
    return json.loads(proc.stdout)


def binds(service: dict) -> list[dict]:
    found = []
    for vol in service.get("volumes") or []:
        if isinstance(vol, dict) and vol.get("target"):
            found.append(vol)
    return found


def by_target(service: dict, target: str) -> dict | None:
    for vol in binds(service):
        if vol.get("target") == target:
            return vol
    return None


def workspace_target(service: dict) -> str | None:
    matches = []
    for vol in binds(service):
        target = vol.get("target") or ""
        source = vol.get("source") or ""
        if target in KNOWN_OPT or target.startswith("/opt/") or target.startswith("/etc/"):
            continue
        if target.startswith("/") and source == target:
            matches.append(target)
    if len(matches) != 1:
        return None
    return matches[0]


def read_only(vol: dict | None) -> bool:
    return bool(vol and vol.get("read_only"))


def check_ssh(config: dict) -> list[str]:
    errors = []
    services = config.get("services") or {}
    worker = services.get("hermes-worker")
    hermes = services.get("hermes")
    if not worker:
        return ["ssh: service hermes-worker missing"]
    if not hermes:
        return ["ssh: service hermes missing"]
    dep = (hermes.get("depends_on") or {}).get("hermes-worker") or {}
    if dep.get("condition") != "service_healthy":
        errors.append("ssh: hermes does not wait for a healthy hermes-worker")
    projects = by_target(hermes, "/opt/projects")
    if not read_only(projects):
        errors.append("ssh: hermes /opt/projects must be read-only")
    for target in ("/opt/voice", "/opt/memory"):
        if by_target(hermes, target):
            errors.append(f"ssh: hermes must not mount {target}")
    if not read_only(by_target(hermes, KEY_TARGET)):
        errors.append("ssh: hermes is missing the worker private key mount")
    if by_target(hermes, KEY_INIT) is None:
        errors.append("ssh: hermes is missing the worker key install script")
    for target in ("/opt/projects", "/opt/voice", "/opt/memory"):
        vol = by_target(worker, target)
        if vol is None:
            errors.append(f"ssh: hermes-worker is missing {target}")
        elif read_only(vol):
            errors.append(f"ssh: hermes-worker {target} must be read-write")
    skills = by_target(worker, "/opt/skills")
    if not read_only(skills):
        errors.append("ssh: hermes-worker /opt/skills must be read-only")
    workspace = workspace_target(worker)
    if not workspace:
        errors.append("ssh: hermes-worker is missing the coding workspace mount")
    elif by_target(hermes, workspace):
        errors.append("ssh: hermes must not mount the coding workspace")
    if worker.get("ports"):
        errors.append("ssh: hermes-worker publishes a host port")
    return errors


def check_local(config: dict) -> list[str]:
    errors = []
    services = config.get("services") or {}
    if "hermes-worker" in services:
        errors.append("local: hermes-worker must not be defined")
    hermes = services.get("hermes")
    if not hermes:
        return errors + ["local: service hermes missing"]
    if "hermes-worker" in (hermes.get("depends_on") or {}):
        errors.append("local: hermes still depends on hermes-worker")
    projects = by_target(hermes, "/opt/projects")
    if projects is None:
        errors.append("local: hermes is missing /opt/projects")
    elif read_only(projects):
        errors.append("local: hermes /opt/projects must be read-write")
    for target in ("/opt/voice", "/opt/memory"):
        vol = by_target(hermes, target)
        if vol is None:
            errors.append(f"local: hermes is missing {target}")
        elif read_only(vol):
            errors.append(f"local: hermes {target} must be read-write")
    workspace = workspace_target(hermes)
    if not workspace:
        errors.append("local: hermes is missing the coding workspace mount")
    elif read_only(by_target(hermes, workspace)):
        errors.append("local: hermes coding workspace must be read-write")
    if by_target(hermes, KEY_TARGET):
        errors.append("local: hermes must not mount the worker private key")
    if by_target(hermes, KEY_INIT):
        errors.append("local: hermes must not install the worker key")
    return errors


def main() -> None:
    errors = check_ssh(load_config("ssh")) + check_local(load_config("local"))
    if errors:
        for err in errors:
            fail(err)
        raise SystemExit(1)
    print("OK terminal backend compose layouts (ssh and local)")


if __name__ == "__main__":
    main()
