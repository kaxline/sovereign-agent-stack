#!/usr/bin/env python3
"""Resolved compose config for the Bitwarden services: profiles, the broker's
sandbox, what hermes and bitwarden-setup can and cannot reach, and the local
Vaultwarden with its TLS sidecar (profile vault).

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
LOCAL_SERVICES = ("vaultwarden", "vaultwarden-tls")
VAULTWARDEN_IMAGE = ("vaultwarden/server:1.37.4@sha256:"
                     "efb3cde962015fcc036b2ea625242248611943b27212ebf4392841fb30fad055")
LOCAL_NAME = "vaultwarden.vault.internal"
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
    for name in SERVICES + LOCAL_SERVICES:
        if name in services:
            fail(f"{name} is in the core profile")
    services = config("core,bitwarden,bitwarden-tools").get("services", {})
    for name in LOCAL_SERVICES:
        if name in services:
            fail(f"{name} starts without the vault profile")


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
    # Exactly the target folders, read-write; TWENTY_SECRETS_DIR unset here.
    want = {
        "/run/bitwarden-sync": SYNC_VOLUME,
        "/targets/twenty-workspaces": str(ROOT / "compose/twenty/workspaces"),
        "/targets/twenty-secrets": str(ROOT / "compose/twenty/no-secrets"),
        "/targets/postgres-services": str(ROOT / "compose/postgres/services"),
        "/targets/caldav-accounts": str(ROOT / "compose/caldav-mcp/accounts"),
    }
    got = {v.get("target"): str(v.get("source")) for v in mounts(sync)}
    if got != want or len(mounts(sync)) != len(want) or len(sync.get("volumes") or []) != len(want):
        fail(f"bitwarden-secrets-sync mounts {sorted(got.items())}, want {sorted(want.items())}")
    for vol in mounts(sync):
        if vol.get("read_only"):
            fail(f"bitwarden-secrets-sync mounts {vol.get('target')} read-only, want read-write")
    if sync.get("network_mode") != "none":
        fail("bitwarden-secrets-sync must use network_mode: none")
    for name, service in services.items():
        if name in ("bitwarden-broker", "bitwarden-secrets-sync"):
            continue
        if any(v.get("source") == SYNC_VOLUME for v in mounts(service)):
            fail(f"{name} mounts {SYNC_VOLUME}")


def env_value(service: dict, key: str):
    env = service.get("environment") or {}
    return env.get(key) if isinstance(env, dict) else None


def check_local(cfg: dict) -> None:
    services = cfg.get("services", {})
    vw, tls = services.get("vaultwarden"), services.get("vaultwarden-tls")
    if vw is None or tls is None:
        fail("vaultwarden and vaultwarden-tls belong to the vault profile")
        return
    if vw.get("image") != VAULTWARDEN_IMAGE:
        fail(f"vaultwarden image is {vw.get('image')!r}, want {VAULTWARDEN_IMAGE!r}")
    ports = vw.get("ports") or []
    want_port = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 80, "published": "8222", "protocol": "tcp"}]
    if ports != want_port:
        fail(f"vaultwarden ports are {ports!r}, want only 127.0.0.1:8222:80")
    for key, want in (("SIGNUPS_ALLOWED", "false"), ("INVITATIONS_ALLOWED", "true"),
                      ("DOMAIN", "http://localhost:8222")):
        if env_value(vw, key) != want:
            fail(f"vaultwarden {key} must be {want}")
    if env_value(vw, "ADMIN_TOKEN") is not None:
        fail("vaultwarden ADMIN_TOKEN must come from a file, not the environment")
    token_file = env_value(vw, "ADMIN_TOKEN_FILE")
    token_dir = str(Path(token_file or "/").parent)
    vol = mount_at(vw, token_dir)
    if not token_file or vol is None or not vol.get("read_only") \
            or not str(vol.get("source", "")).endswith(f"{ADDON_DATA}/secrets/local/vaultwarden"):
        fail("vaultwarden reads ADMIN_TOKEN_FILE from secrets/local/vaultwarden, mounted read-only")
    vol = mount_at(vw, "/data")
    if vol is None or not str(vol.get("source", "")).endswith(f"{ADDON_DATA}/vaultwarden") or vol.get("read_only"):
        fail("vaultwarden keeps /data in data/addons/bitwarden/vaultwarden")
    if {v.get("target") for v in mounts(vw)} != {"/data", token_dir}:
        fail(f"vaultwarden mounts {sorted(v.get('target') for v in mounts(vw))}, want /data and the token folder")
    if networks(vw) != {"default", "vault"}:
        fail(f"vaultwarden networks are {sorted(networks(vw))}, want ['default', 'vault']")
    if "/alive" not in " ".join((vw.get("healthcheck") or {}).get("test") or []):
        fail("vaultwarden healthcheck must GET /alive")

    if tls.get("image") != "nginx:1.30.6-alpine":
        fail(f"vaultwarden-tls image is {tls.get('image')!r}, want the pinned nginx")
    if tls.get("ports"):
        fail("vaultwarden-tls publishes a port")
    if set(tls.get("networks") or {}) != {"vault"}:
        fail(f"vaultwarden-tls networks are {sorted(networks(tls))}, want ['vault']")
    aliases = ((tls.get("networks") or {}).get("vault") or {}).get("aliases") or []
    if LOCAL_NAME not in aliases:
        fail(f"vaultwarden-tls needs the alias {LOCAL_NAME} on vault")
    want = {
        "/etc/nginx/templates/default.conf.template": str(ROOT / "compose/bitwarden-tls/default.conf.template"),
        "/etc/nginx/tls/server.crt": f"{ADDON_DATA}/secrets/local/tls/server.crt",
        "/etc/nginx/tls/server.key": f"{ADDON_DATA}/secrets/local/tls/server.key",
    }
    got = {v.get("target"): str(v.get("source")) for v in mounts(tls)}
    if set(got) != set(want) or any(not got[t].endswith(src) for t, src in want.items()):
        fail(f"vaultwarden-tls mounts {sorted(got)}, want the template, server.crt and server.key")
    for vol in mounts(tls):
        if not vol.get("read_only"):
            fail(f"vaultwarden-tls mounts {vol.get('target')} read-write")
    if "vaultwarden" not in (tls.get("depends_on") or {}):
        fail("vaultwarden-tls must depend on vaultwarden")
    template = (ROOT / "compose/bitwarden-tls/default.conf.template").read_text(encoding="utf-8")
    for needle in ("listen 443 ssl", "proxy_pass", "http://vaultwarden:80", "/notifications/hub",
                   "Upgrade", "/etc/nginx/tls/server.crt", "/etc/nginx/tls/server.key"):
        if needle not in template:
            fail(f"compose/bitwarden-tls/default.conf.template lacks {needle!r}")

    allowed = {"bitwarden-broker", "bitwarden-setup", *LOCAL_SERVICES}
    for name, service in services.items():
        if name not in allowed and any(f"{ADDON_DATA}/secrets" in str(v.get("source", "")) for v in mounts(service)):
            fail(f"{name} mounts the Bitwarden secrets")
    rows = (ROOT / "scripts/sync-profiles.sh").read_text(encoding="utf-8").splitlines()
    if "BITWARDEN_LOCAL_ENABLED vault 0" not in rows:
        fail("sync-profiles.sh must map BITWARDEN_LOCAL_ENABLED to vault, default off")


def main() -> None:
    check_off()
    check_broker(config("core,bitwarden").get("services", {}))
    for backend in ("ssh", "local"):
        cfg = config("core,bitwarden,bitwarden-tools", backend)
        check_hermes(cfg, backend)
    check_tools(config("core,bitwarden,bitwarden-tools"))
    check_local(config("core,bitwarden,bitwarden-tools,vault"))
    if failures:
        for msg in failures:
            print(f"FAIL {msg}", file=sys.stderr)
        raise SystemExit(1)
    print("ok")


if __name__ == "__main__":
    main()
