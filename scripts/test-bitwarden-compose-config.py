#!/usr/bin/env python3
"""Resolved compose config for the Bitwarden services: profiles, the broker's
sandbox, what hermes and bitwarden-setup can and cannot reach, and the local
Vaultwarden with its TLS sidecar (profile vault).

Needs a .env that satisfies compose interpolation, as CI writes. Prints no
environment values.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("bitwarden-broker", "bitwarden-setup")
LOCAL_SERVICES = ("vaultwarden", "vaultwarden-tls")
VAULTWARDEN_IMAGE = ("vaultwarden/server:1.37.5@sha256:"
                     "4fe17f415f90087377d37c755e0ea3cf189dcc6794e49cdd2a9adbb4937d9e1f")
LOCAL_NAME = "vaultwarden.vault.internal"
ADDON_DATA = "addons/bitwarden"
SYNC_DIR = "/run/bitwarden-sync"
HOST_USER = "1000:1000"  # HERMES_UID:HERMES_GID's defaults
checks = Checks()


def config(profiles: str, backend: str = "ssh") -> dict:
    return compose_config.render(profiles, {"HERMES_TERMINAL_BACKEND": backend})


def mounts(service: dict) -> list[dict]:
    return [v for v in service.get("volumes") or [] if isinstance(v, dict)]


def mount_at(service: dict, target: str) -> dict | None:
    return next((v for v in mounts(service) if v.get("target") == target), None)


def networks(service: dict) -> set[str]:
    return set(service.get("networks") or {})


def helper_profiles(env_profiles: str | None, caller: str | None = None) -> list[str]:
    """The --profile values `compose_with_profile bitwarden` passes, with .env
    holding COMPOSE_PROFILES=env_profiles (None: no such line) and the caller's
    environment holding COMPOSE_PROFILES=caller (None: unset)."""
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("" if env_profiles is None else f"COMPOSE_PROFILES={env_profiles}\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
        env["ENV_FILE"] = str(env_file)
        if caller is not None:
            env["COMPOSE_PROFILES"] = caller
        script = ('source scripts/lib/env.sh; docker() { printf "%s\\n" "$@"; }; '
                  "compose_with_profile bitwarden ps -q bitwarden-broker")
        proc = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)
    args = [a for a in proc.stdout.split("\n") if a]
    if proc.returncode != 0 or args[:1] != ["compose"] or args[-3:] != ["ps", "-q", "bitwarden-broker"]:
        checks.fail(f"compose_with_profile ran docker {args} (exit {proc.returncode})")
        return []
    flags = args[1:-3]
    if len(flags) % 2 or any(f != "--profile" for f in flags[::2]):
        checks.fail(f"compose_with_profile passed {flags}, not --profile pairs")
        return []
    return flags[1::2]


def check_profile_flags() -> None:
    """--profile bitwarden on its own replaces COMPOSE_PROFILES and drops core,
    so compose rejects the project: bitwarden-broker depends on hermes. The
    scripts go through compose_with_profile, which keeps the enabled profiles."""
    pattern = re.compile(r"docker compose --profile bitwarden(\s|$)")
    for rel in ("scripts/vault.sh", "scripts/doctor.sh"):
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and pattern.search(line):
                checks.fail(f"{rel}:{n} runs docker compose --profile bitwarden alone; use compose_with_profile")
    cases = (
        (None, None, ["core", "bitwarden"]),
        ("core,budget,bitwarden", None, ["core", "budget", "bitwarden"]),
        ("core, rag", None, ["core", "rag", "bitwarden"]),
        ("core,budget", "core,mail", ["core", "mail", "bitwarden"]),
    )
    for env_profiles, caller, want in cases:
        got = helper_profiles(env_profiles, caller)
        checks.expect(got == want, f"compose_with_profile with .env {env_profiles!r} and caller {caller!r}: {got}, want {want}")
    services = config(",".join(helper_profiles(None))).get("services", {})
    checks.expect({"bitwarden-broker", "hermes"} <= set(services),
                  "the default profiles plus bitwarden render bitwarden-broker and hermes")
    # vault.sh passes these alone (setup_run, init).
    for alone in ("bitwarden-tools", "vault"):
        config(alone)


def check_off() -> None:
    services = config("core").get("services", {})
    for name in SERVICES + LOCAL_SERVICES:
        if name in services:
            checks.fail(f"{name} is in the core profile")
    services = config("core,bitwarden,bitwarden-tools").get("services", {})
    for name in LOCAL_SERVICES:
        if name in services:
            checks.fail(f"{name} starts without the vault profile")


def check_broker(services: dict) -> None:
    broker = services.get("bitwarden-broker")
    if broker is None:
        checks.fail("bitwarden-broker missing from core,bitwarden")
        return
    if broker.get("network_mode") != "service:hermes":
        checks.fail(f"bitwarden-broker network_mode is {broker.get('network_mode')!r}, want 'service:hermes'")
    dep = (broker.get("depends_on") or {}).get("hermes") or {}
    if dep.get("condition") != "service_started" or dep.get("restart") is not True:
        checks.fail(f"bitwarden-broker depends_on.hermes is {dep!r}, want condition service_started and restart true")
    if broker.get("networks"):
        checks.fail("bitwarden-broker joins networks of its own")
    if broker.get("ports"):
        checks.fail("bitwarden-broker publishes a port")
    if broker.get("read_only") is not True:
        checks.fail("bitwarden-broker needs read_only: true")
    if broker.get("cap_drop") != ["ALL"]:
        checks.fail(f"bitwarden-broker cap_drop is {broker.get('cap_drop')!r}, want ['ALL']")
    if broker.get("user") != HOST_USER:
        checks.fail(f"bitwarden-broker runs as HERMES_UID:HERMES_GID ({HOST_USER}), not {broker.get('user')!r}")
    # The manual-unlock socket's folder: a tmpfs owned by that user, so no
    # volume keeps an image owner that differs from it.
    sync = next((t for t in broker.get("tmpfs") or [] if t.split(":")[0] == SYNC_DIR), "")
    if sync != f"{SYNC_DIR}:mode=0700,uid=1000,gid=1000":
        checks.fail(f"bitwarden-broker needs a mode 0700 tmpfs at {SYNC_DIR} owned by HERMES_UID, not {sync!r}")
    if "no-new-privileges:true" not in (broker.get("security_opt") or []):
        checks.fail("bitwarden-broker needs no-new-privileges:true")
    if "/tmp" not in [t.split(":")[0] for t in broker.get("tmpfs") or []]:
        checks.fail("bitwarden-broker needs a tmpfs on /tmp")
    test = " ".join((broker.get("healthcheck") or {}).get("test") or [])
    if "python3" not in test or "http://127.0.0.1:8099/health" not in test:
        checks.fail("bitwarden-broker healthcheck GETs http://127.0.0.1:8099/health with python3")
    want = {
        "/store/instances": (True, f"{ADDON_DATA}/instances"),
        "/secrets": (True, f"{ADDON_DATA}/secrets"),
        "/data/bw": (False, f"{ADDON_DATA}/bw"),
        "/data/broker": (False, f"{ADDON_DATA}/broker"),
    }
    for target, (read_only, source) in want.items():
        vol = mount_at(broker, target)
        if vol is None:
            checks.fail(f"bitwarden-broker does not mount {target}")
            continue
        if not str(vol.get("source", "")).endswith(source):
            checks.fail(f"bitwarden-broker {target} comes from {vol.get('source')!r}, want .../{source}")
        if bool(vol.get("read_only")) != read_only:
            checks.fail(f"bitwarden-broker {target} must be {'read-only' if read_only else 'writable'}")
    extra = {v.get("target") for v in mounts(broker)} - set(want)
    if extra:
        checks.fail(f"bitwarden-broker mounts more than it needs: {sorted(extra)}")
    if mount_at(broker, "/accounts") is not None or "compose/bitwarden/" in str(broker.get("volumes")):
        checks.fail("bitwarden-broker still mounts the account files")
    if env_value(broker, "BITWARDEN_INSTANCES_DIR") != "/store/instances":
        checks.fail("bitwarden-broker needs BITWARDEN_INSTANCES_DIR=/store/instances")
    if env_value(broker, "BITWARDEN_ACCOUNTS_DIR") is not None:
        checks.fail("bitwarden-broker still sets BITWARDEN_ACCOUNTS_DIR")


def check_store_follows_data_root() -> None:
    """ASSISTANT_DATA_ROOT moves the instance store, BITWARDEN_HOST_DATA_DIR the rest."""
    with tempfile.TemporaryDirectory() as tmp:
        store, bw = Path(tmp) / "store-root", Path(tmp) / "bw-data"
        services = compose_config.render("core,bitwarden,bitwarden-tools", {
            "ASSISTANT_DATA_ROOT": str(store), "BITWARDEN_HOST_DATA_DIR": str(bw)}).get("services", {})
    for name in SERVICES:
        service = services.get(name) or {}
        vol = mount_at(service, "/store/instances")
        if vol is None or vol.get("source") != str(store / ADDON_DATA / "instances"):
            checks.fail(f"{name} /store/instances comes from {vol and vol.get('source')!r}, "
                        "want ASSISTANT_DATA_ROOT/addons/bitwarden/instances")
        vol = mount_at(service, "/secrets")
        if vol is None or vol.get("source") != str(bw / "secrets"):
            checks.fail(f"{name} /secrets does not follow BITWARDEN_HOST_DATA_DIR")


def check_hermes(cfg: dict, backend: str) -> None:
    services = cfg.get("services", {})
    hermes = services.get("hermes") or {}
    for vol in mounts(hermes):
        source = str(vol.get("source", ""))
        if ADDON_DATA in source or "compose/bitwarden/" in source:
            checks.fail(f"hermes ({backend}) mounts {source}")
    if "vault" not in networks(hermes):
        checks.fail(f"hermes ({backend}) is not on the vault network")
    vault = (cfg.get("networks") or {}).get("vault")
    if not vault:
        checks.fail("network vault missing from compose config")
    elif not vault.get("internal"):
        checks.fail("network vault must be internal")


def check_tools(cfg: dict) -> None:
    services = cfg.get("services", {})
    hermes_nets = networks(services.get("hermes") or {})
    setup = services.get("bitwarden-setup")
    if setup is None:
        checks.fail("bitwarden-setup belongs to bitwarden-tools")
        return
    if "bitwarden-secrets-sync" in services:
        checks.fail("bitwarden-secrets-sync was removed and must not render")
    broker_image = (services.get("bitwarden-broker") or {}).get("image")
    for name, service, entry in (("bitwarden-setup", setup, "/app/setup/setup.py"),):
        if service.get("image") != broker_image:
            checks.fail(f"{name} does not use the broker image")
        if service.get("entrypoint") != ["python3", entry]:
            checks.fail(f"{name} entrypoint is {service.get('entrypoint')!r}, want ['python3', {entry!r}]")
    if networks(setup) != {"bitwarden-setup", "vault"}:
        checks.fail(f"bitwarden-setup networks are {sorted(networks(setup))}, want ['bitwarden-setup', 'vault']")
    shared = (networks(setup) & hermes_nets) - {"vault"}
    if shared:
        checks.fail(f"bitwarden-setup shares {sorted(shared)} with hermes")
    if setup.get("network_mode"):
        checks.fail("bitwarden-setup must not use network_mode")
    setup_net = (cfg.get("networks") or {}).get("bitwarden-setup") or {}
    if setup_net.get("internal"):
        checks.fail("network bitwarden-setup must reach Bitwarden, so it is not internal")
    for target, source in (("/secrets", f"{ADDON_DATA}/secrets"), ("/store/instances", f"{ADDON_DATA}/instances"),
                           ("/broker", f"{ADDON_DATA}/broker")):
        vol = mount_at(setup, target)
        if vol is None or not str(vol.get("source", "")).endswith(source) or vol.get("read_only"):
            checks.fail(f"bitwarden-setup mounts {source} read-write at {target}")
    if mount_at(setup, "/accounts") is not None:
        checks.fail("bitwarden-setup still mounts the account files")
    for key, want in (("BITWARDEN_INSTANCES_DIR", "/store/instances"), ("BITWARDEN_BROKER_DIR", "/broker")):
        if env_value(setup, key) != want:
            checks.fail(f"bitwarden-setup needs {key}={want}")
    for vol in mounts(setup):
        source = str(vol.get("source", ""))
        # Hermes' own data (data/hermes and below), not any path that happens
        # to contain the word, such as a worktree's directory name.
        if re.search(r"(^|/)data/hermes(/|$)", source):
            checks.fail(f"bitwarden-setup mounts {source}")
    if setup.get("user") != HOST_USER:
        checks.fail(f"bitwarden-setup runs as HERMES_UID:HERMES_GID ({HOST_USER}), not {setup.get('user')!r}")
    if "bitwarden_sync" in (cfg.get("volumes") or {}):
        checks.fail("the bitwarden_sync volume is gone: the socket folder is a tmpfs")
    for name, service in services.items():
        if name != "bitwarden-broker" and any(SYNC_DIR in str(t) for t in service.get("tmpfs") or []):
            checks.fail(f"{name} mounts {SYNC_DIR}")


def env_value(service: dict, key: str):
    env = service.get("environment") or {}
    return env.get(key) if isinstance(env, dict) else None


def check_local(cfg: dict) -> None:
    services = cfg.get("services", {})
    nets = cfg.get("networks") or {}
    vw, tls = services.get("vaultwarden"), services.get("vaultwarden-tls")
    if vw is None or tls is None:
        checks.fail("vaultwarden and vaultwarden-tls belong to the vault profile")
        return
    local = f"{ADDON_DATA}/local"
    if vw.get("image") != VAULTWARDEN_IMAGE:
        checks.fail(f"vaultwarden image is {vw.get('image')!r}, want {VAULTWARDEN_IMAGE!r}")
    ports = vw.get("ports") or []
    want_port = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 80, "published": "8222", "protocol": "tcp"}]
    if ports != want_port:
        checks.fail(f"vaultwarden ports are {ports!r}, want only 127.0.0.1:8222:80")
    for key, want in (("SIGNUPS_ALLOWED", "false"), ("INVITATIONS_ALLOWED", "true"),
                      ("DOMAIN", "http://localhost:8222"), ("DISABLE_ICON_DOWNLOAD", "true")):
        if env_value(vw, key) != want:
            checks.fail(f"vaultwarden {key} must be {want}")
    if env_value(vw, "ADMIN_TOKEN") is not None:
        checks.fail("vaultwarden ADMIN_TOKEN must come from a file, not the environment")
    token_file = env_value(vw, "ADMIN_TOKEN_FILE")
    token_dir = str(Path(token_file or "/").parent)
    vol = mount_at(vw, token_dir)
    if not token_file or vol is None or not vol.get("read_only") \
            or not str(vol.get("source", "")).endswith(f"{local}/vaultwarden"):
        checks.fail("vaultwarden reads ADMIN_TOKEN_FILE from local/vaultwarden, mounted read-only")
    vol = mount_at(vw, "/data")
    if vol is None or not str(vol.get("source", "")).endswith(f"{ADDON_DATA}/vaultwarden") or vol.get("read_only"):
        checks.fail("vaultwarden keeps /data in data/addons/bitwarden/vaultwarden")
    if {v.get("target") for v in mounts(vw)} != {"/data", token_dir}:
        checks.fail(f"vaultwarden mounts {sorted(v.get('target') for v in mounts(vw))}, want /data and the token folder")
    for name, service in (("vaultwarden", vw), ("vaultwarden-tls", tls)):
        if "no-new-privileges:true" not in (service.get("security_opt") or []):
            checks.fail(f"{name} needs no-new-privileges:true")

    # Ruling R20: nothing on hermes' networks reaches vaultwarden except through the sidecar.
    if networks(vw) != {"vault-backend", "vault-host"}:
        checks.fail(f"vaultwarden networks are {sorted(networks(vw))}, want ['vault-backend', 'vault-host']")
    shared = networks(vw) & networks(services.get("hermes") or {})
    if shared:
        checks.fail(f"vaultwarden shares {sorted(shared)} with hermes")
    if not (nets.get("vault-backend") or {}).get("internal"):
        checks.fail("network vault-backend must be internal")
    if (nets.get("vault-host") or {}).get("internal"):
        checks.fail("network vault-host carries the host port, so it is not internal")
    for name, service in services.items():
        joined = networks(service)
        if "vault-host" in joined and name != "vaultwarden":
            checks.fail(f"{name} joins vault-host")
        if "vault-backend" in joined and name not in LOCAL_SERVICES:
            checks.fail(f"{name} joins vault-backend")
    if "/alive" not in " ".join((vw.get("healthcheck") or {}).get("test") or []):
        checks.fail("vaultwarden healthcheck must GET /alive")

    if tls.get("image") != "nginx:1.30.6-alpine":
        checks.fail(f"vaultwarden-tls image is {tls.get('image')!r}, want the pinned nginx")
    if "image: ${VAULTWARDEN_TLS_IMAGE:-nginx:1.30.6-alpine}" not in (ROOT / "docker-compose.yml").read_text():
        checks.fail("vaultwarden-tls image must be ${VAULTWARDEN_TLS_IMAGE:-nginx:1.30.6-alpine}")
    if tls.get("ports"):
        checks.fail("vaultwarden-tls publishes a port")
    if networks(tls) != {"vault", "vault-backend"}:
        checks.fail(f"vaultwarden-tls networks are {sorted(networks(tls))}, want ['vault', 'vault-backend']")
    aliases = ((tls.get("networks") or {}).get("vault") or {}).get("aliases") or []
    if LOCAL_NAME not in aliases:
        checks.fail(f"vaultwarden-tls needs the alias {LOCAL_NAME} on vault")
    want = {
        "/etc/nginx/templates/default.conf.template": str(ROOT / "compose/bitwarden-tls/default.conf.template"),
        "/etc/nginx/tls": f"{local}/tls/server",
    }
    got = {v.get("target"): str(v.get("source")) for v in mounts(tls)}
    if set(got) != set(want) or any(not got[t].endswith(src) for t, src in want.items()):
        checks.fail(f"vaultwarden-tls mounts {sorted(got.items())}, want the template and local/tls/server")
    for vol in mounts(tls):
        if not vol.get("read_only"):
            checks.fail(f"vaultwarden-tls mounts {vol.get('target')} read-write")
    if "vaultwarden" not in (tls.get("depends_on") or {}):
        checks.fail("vaultwarden-tls must depend on vaultwarden")
    template = (ROOT / "compose/bitwarden-tls/default.conf.template").read_text(encoding="utf-8")
    for needle in ("listen 443 ssl", "proxy_pass", "http://vaultwarden:80", "/notifications/hub",
                   "Upgrade", "/etc/nginx/tls/server.crt", "/etc/nginx/tls/server.key",
                   "proxy_set_header X-Real-IP $remote_addr;"):
        if needle not in template:
            checks.fail(f"compose/bitwarden-tls/default.conf.template lacks {needle!r}")
    forwarded = re.findall(r"proxy_set_header X-Forwarded-For (\S+);", template)
    if not forwarded or set(forwarded) != {"$remote_addr"}:
        checks.fail(f"X-Forwarded-For must be $remote_addr, overwriting the client's, not {sorted(set(forwarded))}")

    # Local mode's secrets: the admin token, its hash and the CA key live under local/.
    setup = services.get("bitwarden-setup") or {}
    vol = mount_at(setup, "/local")
    if vol is None or not str(vol.get("source", "")).endswith(local) or vol.get("read_only"):
        checks.fail("bitwarden-setup mounts local/ read-write at /local")
    allowed_local = {"bitwarden-setup": local, "vaultwarden": f"{local}/vaultwarden",
                     "vaultwarden-tls": f"{local}/tls/server"}
    for name, service in services.items():
        for v in mounts(service):
            source = str(v.get("source", "")).rstrip("/")
            if source.endswith(ADDON_DATA) or (local in source and not source.endswith(allowed_local.get(name, "\0"))):
                checks.fail(f"{name} mounts {source}")
    broker = services.get("bitwarden-broker") or {}
    for v in mounts(broker):
        source = str(v.get("source", ""))
        if "admin_token" in source or "ca.key" in source or local in source:
            checks.fail(f"bitwarden-broker mounts {source}, which exposes the admin token or the CA key")
    allowed = {"bitwarden-broker", "bitwarden-setup"}
    for name, service in services.items():
        if name not in allowed and any(f"{ADDON_DATA}/secrets" in str(v.get("source", "")) for v in mounts(service)):
            checks.fail(f"{name} mounts the Bitwarden secrets")
    rows = (ROOT / "scripts/sync-profiles.sh").read_text(encoding="utf-8").splitlines()
    if "BITWARDEN_LOCAL_ENABLED vault 0" not in rows:
        checks.fail("sync-profiles.sh must map BITWARDEN_LOCAL_ENABLED to vault, default off")


def main() -> None:
    check_off()
    check_profile_flags()
    check_broker(config("core,bitwarden").get("services", {}))
    check_store_follows_data_root()
    for backend in ("ssh", "local"):
        cfg = config("core,bitwarden,bitwarden-tools", backend)
        check_hermes(cfg, backend)
    check_tools(config("core,bitwarden,bitwarden-tools"))
    check_local(config("core,bitwarden,bitwarden-tools,vault"))
    checks.finish("ok")


if __name__ == "__main__":
    main()
