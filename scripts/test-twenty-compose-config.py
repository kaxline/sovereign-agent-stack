#!/usr/bin/env python3
"""Resolved compose config for the crm profile: memory figures, healthchecks
and secret files on all four Twenty containers (CR 0020 R2, R5, R8).

Needs a .env that satisfies compose interpolation, as CI writes. Prints no
secret values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("twenty-server", "twenty-worker", "twenty-db", "twenty-redis")
MIB = 1024 * 1024
failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def config(extra_env: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    env["COMPOSE_PROFILES"] = "core,crm"
    env.update(extra_env or {})
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"FAIL docker compose config: {proc.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(proc.stdout)


def memory_bytes(value) -> int:
    # compose config normalises "1g" etc. to a byte count (int or string).
    return int(value)


def env_of(service: dict) -> dict:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {k: ("" if v is None else str(v)) for k, v in raw.items()}
    return dict(item.split("=", 1) if "=" in item else (item, "") for item in raw)


# Value mode, whatever the local .env says.
cfg = config({"TWENTY_ENCRYPTION_KEY_FILE": "", "TWENTY_DB_PASSWORD_FILE": "",
              "TWENTY_ENCRYPTION_KEY": "k", "TWENTY_DB_PASSWORD": "p"})
services = cfg.get("services", {})
for name in SERVICES:
    svc = services.get(name)
    if svc is None:
        fail(f"{name} missing from the crm profile")
        continue
    res = (svc.get("deploy") or {}).get("resources") or {}
    reserve = (res.get("reservations") or {}).get("memory")
    limit = (res.get("limits") or {}).get("memory")
    if reserve is None or limit is None:
        fail(f"{name} needs deploy.resources reservations.memory and limits.memory")
    elif memory_bytes(reserve) >= memory_bytes(limit):
        fail(f"{name}: reservation must be below the limit")
    test = (svc.get("healthcheck") or {}).get("test")
    if not test or svc["healthcheck"].get("disable"):
        fail(f"{name} needs a healthcheck")

worker_test = " ".join((services.get("twenty-worker", {}).get("healthcheck") or {}).get("test") or [])
# "[q]ueue-worker": a plain pattern would also match the healthcheck's own
# sh -c command line, so the check could never fail.
if "pgrep -f '[q]ueue-worker'" not in worker_test:
    fail("twenty-worker healthcheck runs pgrep -f '[q]ueue-worker'")

redis_cmd = " ".join(services.get("twenty-redis", {}).get("command") or [])
if "--maxmemory-policy noeviction" not in redis_cmd or "--maxmemory " not in redis_cmd:
    fail("twenty-redis runs with --maxmemory and --maxmemory-policy noeviction")
else:
    # Redis must refuse writes before the container limit kills it.
    words = redis_cmd.split()
    raw = words[words.index("--maxmemory") + 1].lower()
    units = {"kb": 1024, "mb": MIB, "gb": 1024 * MIB}
    maxmem = int(raw[:-2]) * units[raw[-2:]] if raw[-2:] in units else int(raw)
    limit = memory_bytes(services["twenty-redis"]["deploy"]["resources"]["limits"]["memory"])
    if not maxmem < limit * 0.8:
        fail("twenty-redis --maxmemory must stay under 80% of its container limit")

# Limits measured under load on v2.55 (docs/crm.md), about 1.25x the peak.
EXPECTED_LIMITS = {"twenty-server": 1152 * MIB, "twenty-worker": 1152 * MIB,
                   "twenty-db": 256 * MIB, "twenty-redis": 128 * MIB}
for name, want in EXPECTED_LIMITS.items():
    got = ((services.get(name, {}).get("deploy") or {}).get("resources") or {}).get("limits", {}).get("memory")
    if got is None or memory_bytes(got) != want:
        fail(f"{name}: memory limit is {want // MIB}m (measured, docs/crm.md)")

# Value mode (default): no *_FILE paths reach the containers.
for name in ("twenty-server", "twenty-worker"):
    env = env_of(services.get(name, {}))
    for key in ("TWENTY_ENCRYPTION_KEY_FILE", "TWENTY_DB_PASSWORD_FILE"):
        if env.get(key):
            fail(f"{name}: {key} must be empty when no file is configured")
    secrets = {s["source"] if isinstance(s, dict) else s for s in services.get(name, {}).get("secrets") or []}
    if secrets != {"twenty_encryption_key", "twenty_db_password"}:
        fail(f"{name} mounts both Twenty secrets")
if env_of(services.get("twenty-db", {})).get("POSTGRES_PASSWORD_FILE"):
    fail("twenty-db: POSTGRES_PASSWORD_FILE must be empty in value mode (Postgres refuses both)")

# File mode: paths point into /run/secrets and the secret sources are the files.
cfg = config({
    "TWENTY_ENCRYPTION_KEY": "",
    "TWENTY_DB_PASSWORD": "",
    "TWENTY_ENCRYPTION_KEY_FILE": str(ROOT / "compose/twenty/no-secret"),
    "TWENTY_DB_PASSWORD_FILE": str(ROOT / "compose/twenty/no-secret"),
})
services = cfg.get("services", {})
for name in ("twenty-server", "twenty-worker"):
    env = env_of(services.get(name, {}))
    if env.get("TWENTY_ENCRYPTION_KEY_FILE") != "/run/secrets/twenty_encryption_key":
        fail(f"{name}: TWENTY_ENCRYPTION_KEY_FILE points at /run/secrets/twenty_encryption_key")
    if env.get("TWENTY_DB_PASSWORD_FILE") != "/run/secrets/twenty_db_password":
        fail(f"{name}: TWENTY_DB_PASSWORD_FILE points at /run/secrets/twenty_db_password")
    if env.get("ENCRYPTION_KEY"):
        fail(f"{name}: ENCRYPTION_KEY must be empty in file mode")
    if "twenty:@twenty-db" not in env.get("PG_DATABASE_URL", ""):
        fail(f"{name}: PG_DATABASE_URL carries no password in file mode")
if env_of(services.get("twenty-db", {})).get("POSTGRES_PASSWORD_FILE") != "/run/secrets/twenty_db_password":
    fail("twenty-db reads POSTGRES_PASSWORD_FILE in file mode")

if failures:
    for msg in failures:
        print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)
print("OK Twenty compose config (memory, healthchecks, secret files)")
