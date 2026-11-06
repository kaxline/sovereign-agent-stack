#!/usr/bin/env python3
"""Fail when a cont-init script reads a compose-set variable without with-contenv.

s6-overlay v3 runs /etc/cont-init.d scripts with an empty environment. Only a
script started through /command/with-contenv sees the container's variables;
any other script gets its own default for them, silently (CR 0021).

Runs `docker compose config` for each terminal backend, finds every bind mount
into /etc/cont-init.d, and checks each source script against the environment
of the service that mounts it. It prints variable names, never values.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONT_INIT = "/etc/cont-init.d/"
WITH_CONTENV = "#!/command/with-contenv"
VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def load_config(backend: str) -> dict:
    env = os.environ.copy()
    env.setdefault("COMPOSE_PROFILES", "core")
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
        print(f"FAIL {backend}: {err}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(proc.stdout)


def env_names(service: dict) -> set[str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return {item.split("=", 1)[0] for item in env}
    return set(env)


def variables_read(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        names.update(VAR_RE.findall(line))
    return names


def problems(name: str, text: str, service_env: set[str]) -> list[str]:
    """Return one message per script that reads service variables without with-contenv."""
    first = text.splitlines()[0] if text else ""
    if first.startswith(WITH_CONTENV):
        return []
    read = sorted(variables_read(text) & service_env)
    if not read:
        return []
    return [f"{name} reads {', '.join(read)} but starts with {first!r}, not {WITH_CONTENV}"]


def self_test() -> list[str]:
    env = {"HERMES_UID", "HERMES_GID"}
    plain = '#!/bin/sh\n# HERMES_UID in a comment is fine\nuid="${HERMES_UID:-1000}"\n'
    errors = []
    if not problems("plain", plain, env):
        errors.append("self-test: a #!/bin/sh script reading HERMES_UID was not flagged")
    if problems("contenv", WITH_CONTENV + " sh\n" + plain.split("\n", 1)[1], env):
        errors.append("self-test: a with-contenv script was flagged")
    if problems("unrelated", '#!/bin/sh\necho "$1 ${dest}"\n', env):
        errors.append("self-test: a script reading only its own variables was flagged")
    return errors


def check(config: dict, label: str) -> tuple[list[str], int]:
    errors: list[str] = []
    seen = 0
    for svc_name, service in (config.get("services") or {}).items():
        service_env = env_names(service)
        for vol in service.get("volumes") or []:
            target = vol.get("target", "")
            if vol.get("type") != "bind" or not target.startswith(CONT_INIT):
                continue
            source = Path(vol["source"])
            if not source.is_file():
                errors.append(f"{label}: {svc_name} {target}: source {source} is not a file")
                continue
            seen += 1
            name = f"{label}: {svc_name} {target}"
            errors += problems(name, source.read_text(), service_env)
    return errors, seen


def main() -> None:
    errors = self_test()
    total = 0
    for backend in ("ssh", "local"):
        found, seen = check(load_config(backend), backend)
        errors += found
        total += seen
    if total == 0:
        errors.append("no /etc/cont-init.d bind mounts found; the check is not looking at the right config")
    if errors:
        for err in errors:
            print(f"FAIL {err}", file=sys.stderr)
        raise SystemExit(1)
    print(f"OK cont-init scripts that read compose variables use with-contenv ({total} mounts checked)")


if __name__ == "__main__":
    main()
