#!/usr/bin/env python3
"""Resolved compose config for the Bitwarden services: profiles, the broker's
sandbox, and what hermes and bitwarden-setup can and cannot reach.

Needs a .env that satisfies compose interpolation, as CI writes. Prints no
environment values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("bitwarden-broker", "bitwarden-secrets-sync", "bitwarden-setup")
ADDON_DATA = "addons/bitwarden"
SYNC_VOLUME = "bitwarden_sync"
failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def config(profiles: str, backend: str = "ssh") -> dict:
    env = os.environ.copy()
    env["COMPOSE_PROFILES"] = profiles
    env["HERMES_TERMINAL_BACKEND"] = backend
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"FAIL docker compose config ({profiles}): {proc.stderr.strip().splitlines()[-1:]}", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(proc.stdout)


def mounts(service: dict) -> list[dict]:
    return [v for v in service.get("volumes") or [] if isinstance(v, dict)]


def mount_at(service: dict, target: str) -> dict | None:
    return next((v for v in mounts(service) if v.get("target") == target), None)


def networks(service: dict) -> set[str]:
    return set(service.get("networks") or {})


def check_off() -> None:
    services = config("core").get("services", {})
    for name in SERVICES:
        if name in services:
            fail(f"{name} is in the core profile")


def check_broker(services: dict) -> None:
    broker = services.get("bitwarden-broker")
    if broker is None:
        fail("bitwarden-broker missing from core,bitwarden")
        return
    if broker.get("network_mode") != "service:hermes":
        fail(f"bitwarden-broker network_mode is {broker.get('network_mode')!r}, want 'service:hermes'")
    if broker.get("networks"):
        fail("bitwarden-broker joins networks of its own")
    if broker.get("ports"):
        fail("bitwarden-broker publishes a port")
    if broker.get("read_only") is not True:
        fail("bitwarden-broker needs read_only: true")
    if broker.get("cap_drop") != ["ALL"]:
        fail(f"bitwarden-broker cap_drop is {broker.get('cap_drop')!r}, want ['ALL']")
    if broker.get("user") != "10001:10001":
        fail("bitwarden-broker runs as 10001:10001")
    if "no-new-privileges:true" not in (broker.get("security_opt") or []):
        fail("bitwarden-broker needs no-new-privileges:true")
    if "/tmp" not in [t.split(":")[0] for t in broker.get("tmpfs") or []]:
        fail("bitwarden-broker needs a tmpfs on /tmp")
    test = " ".join((broker.get("healthcheck") or {}).get("test") or [])
    if "python3" not in test or "http://127.0.0.1:8099/health" not in test:
        fail("bitwarden-broker healthcheck GETs http://127.0.0.1:8099/health with python3")
    want = {
        "/accounts": (True, "compose/bitwarden/accounts"),
        "/secrets": (True, f"{ADDON_DATA}/secrets"),
        "/data/bw": (False, f"{ADDON_DATA}/bw"),
        "/data/broker": (False, f"{ADDON_DATA}/broker"),
        "/run/bitwarden-sync": (False, SYNC_VOLUME),
    }
    for target, (read_only, source) in want.items():
        vol = mount_at(broker, target)
        if vol is None:
            fail(f"bitwarden-broker does not mount {target}")
            continue
        if not str(vol.get("source", "")).endswith(source):
            fail(f"bitwarden-broker {target} comes from {vol.get('source')!r}, want .../{source}")
        if bool(vol.get("read_only")) != read_only:
            fail(f"bitwarden-broker {target} must be {'read-only' if read_only else 'writable'}")
    extra = {v.get("target") for v in mounts(broker)} - set(want)
    if extra:
        fail(f"bitwarden-broker mounts more than it needs: {sorted(extra)}")


def check_hermes(cfg: dict, backend: str) -> None:
    services = cfg.get("services", {})
    hermes = services.get("hermes") or {}
    for vol in mounts(hermes):
        source = str(vol.get("source", ""))
        if ADDON_DATA in source or source == SYNC_VOLUME or "compose/bitwarden/" in source:
            fail(f"hermes ({backend}) mounts {source}")
    if "vault" not in networks(hermes):
        fail(f"hermes ({backend}) is not on the vault network")
    vault = (cfg.get("networks") or {}).get("vault")
    if not vault:
        fail("network vault missing from compose config")
    elif not vault.get("internal"):
        fail("network vault must be internal")


def check_tools(cfg: dict) -> None:
    services = cfg.get("services", {})
    hermes_nets = networks(services.get("hermes") or {})
    setup = services.get("bitwarden-setup")
    sync = services.get("bitwarden-secrets-sync")
    if setup is None or sync is None:
        fail("bitwarden-setup and bitwarden-secrets-sync belong to bitwarden-tools")
        return
    broker_image = (services.get("bitwarden-broker") or {}).get("image")
    for name, service, entry in (("bitwarden-setup", setup, "/app/setup/setup.py"),
                                 ("bitwarden-secrets-sync", sync, "/app/secrets_sync.py")):
        if service.get("image") != broker_image:
            fail(f"{name} does not use the broker image")
        if service.get("entrypoint") != ["python3", entry]:
            fail(f"{name} entrypoint is {service.get('entrypoint')!r}, want ['python3', {entry!r}]")
    if networks(setup) != {"bitwarden-setup", "vault"}:
        fail(f"bitwarden-setup networks are {sorted(networks(setup))}, want ['bitwarden-setup', 'vault']")
    shared = (networks(setup) & hermes_nets) - {"vault"}
    if shared:
        fail(f"bitwarden-setup shares {sorted(shared)} with hermes")
    if setup.get("network_mode"):
        fail("bitwarden-setup must not use network_mode")
    setup_net = (cfg.get("networks") or {}).get("bitwarden-setup") or {}
    if setup_net.get("internal"):
        fail("network bitwarden-setup must reach Bitwarden, so it is not internal")
    for target, source in (("/secrets", f"{ADDON_DATA}/secrets"), ("/accounts", "compose/bitwarden/accounts")):
        vol = mount_at(setup, target)
        if vol is None or not str(vol.get("source", "")).endswith(source) or vol.get("read_only"):
            fail(f"bitwarden-setup mounts {source} read-write at {target}")
    for vol in mounts(setup):
        source = str(vol.get("source", ""))
        if "hermes" in source or source == SYNC_VOLUME:
            fail(f"bitwarden-setup mounts {source}")
    vol = mount_at(sync, "/run/bitwarden-sync")
    if vol is None or vol.get("source") != SYNC_VOLUME:
        fail("bitwarden-secrets-sync mounts bitwarden_sync at /run/bitwarden-sync")
    for name, service in services.items():
        if name in ("bitwarden-broker", "bitwarden-secrets-sync"):
            continue
        if any(v.get("source") == SYNC_VOLUME for v in mounts(service)):
            fail(f"{name} mounts {SYNC_VOLUME}")


def main() -> None:
    check_off()
    check_broker(config("core,bitwarden").get("services", {}))
    for backend in ("ssh", "local"):
        cfg = config("core,bitwarden,bitwarden-tools", backend)
        check_hermes(cfg, backend)
    check_tools(config("core,bitwarden,bitwarden-tools"))
    if failures:
        for msg in failures:
            print(f"FAIL {msg}", file=sys.stderr)
        raise SystemExit(1)
    print("ok")


if __name__ == "__main__":
    main()
