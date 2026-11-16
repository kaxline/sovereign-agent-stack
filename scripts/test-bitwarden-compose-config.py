#!/usr/bin/env python3
"""Resolved compose config for the Bitwarden services: profiles, the broker's
sandbox, the sign-in signer's hardening and socket volume, what hermes and
bitwarden-setup can and cannot reach, and the local Vaultwarden with its TLS
sidecar (profile vault).

Needs a .env that satisfies compose interpolation, as CI writes. Prints no
environment values.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402
import hermes_image  # noqa: E402
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("bitwarden-broker", "bitwarden-setup")
SIGNIN = "bitwarden-signin"
LOCAL_SERVICES = ("vaultwarden", "vaultwarden-tls")
VAULTWARDEN_IMAGE = ("vaultwarden/server:1.37.5@sha256:"
                     "4fe17f415f90087377d37c755e0ea3cf189dcc6794e49cdd2a9adbb4937d9e1f")
LOCAL_NAME = "vaultwarden.vault.internal"
ADDON_DATA = "addons/bitwarden"
API_TOKEN_DIR = "/run/bitwarden"
API_TOKEN_FILE = f"{API_TOKEN_DIR}/api-token"
STACK_TOKEN_FILE = "/run/browser/stack.token"
STACK_PROVIDER_URL = "ws://web-browser:9222/devtools/browser/credentials"
SYNC_DIR = "/run/bitwarden-sync"
HOST_USER = "1000:1000"  # HERMES_UID:HERMES_GID's defaults
SIGNIN_DIR = "/run/bitwarden-signin"
SIGNIN_SOCKET = f"{SIGNIN_DIR}/signin.sock"
SIGNIN_VOLUME = "bitwarden_signin"
SIGNIN_SECCOMP = "compose/bitwarden-signin/seccomp.json"
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
    so the project renders without hermes and the broker has nothing to serve. The
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
    for name in SERVICES + LOCAL_SERVICES + (SIGNIN,):
        if name in services:
            checks.fail(f"{name} is in the core profile")
    services = config("core,bitwarden,bitwarden-tools").get("services", {})
    for name in LOCAL_SERVICES:
        if name in services:
            checks.fail(f"{name} starts without the vault profile")


def check_broker(cfg: dict) -> None:
    services = cfg.get("services", {})
    broker = services.get("bitwarden-broker")
    if broker is None:
        checks.fail("bitwarden-broker missing from core,bitwarden")
        return
    # Its own networks, not hermes' namespace: credentials carries the API (hermes alone joins it),
    # browser is the route to a provider's CDP gateway. A recreate of hermes now leaves it running.
    if broker.get("network_mode"):
        checks.fail(f"bitwarden-broker must not use network_mode, got {broker.get('network_mode')!r}")
    if broker.get("depends_on"):
        checks.fail(f"bitwarden-broker does not depend on hermes any more, got {broker.get('depends_on')!r}")
    # vault reaches the local Vaultwarden; bitwarden-egress, the broker's alone, reaches Bitwarden's
    # servers for bw login, unlock and sync, and the host, where Boundary's relay listens (CR 0038).
    want = ["bitwarden-egress", "browser", "credentials", "vault"]
    if sorted(broker.get("networks") or {}) != want:
        checks.fail(f"bitwarden-broker networks are {sorted(broker.get('networks') or {})!r}, want {want!r}")
    nets = cfg.get("networks") or {}
    if "bitwarden-egress" not in nets or (nets.get("bitwarden-egress") or {}).get("internal"):
        checks.fail("network bitwarden-egress carries the broker's egress, so it exists and is not internal")
    for name, service in services.items():
        if name != "bitwarden-broker" and "bitwarden-egress" in networks(service):
            checks.fail(f"{name} joins bitwarden-egress")
    if "host.docker.internal=host-gateway" not in (broker.get("extra_hosts") or []) \
            and "host.docker.internal:host-gateway" not in (broker.get("extra_hosts") or []):
        checks.fail(f"bitwarden-broker needs host.docker.internal:host-gateway, got {broker.get('extra_hosts')!r}")
    if env_value(broker, "BITWARDEN_LISTEN") != "0.0.0.0:8099":
        checks.fail(f"bitwarden-broker needs BITWARDEN_LISTEN=0.0.0.0:8099, got "
                    f"{env_value(broker, 'BITWARDEN_LISTEN')!r}")
    if env_value(broker, "BITWARDEN_API_TOKEN_FILE") != API_TOKEN_FILE:
        checks.fail(f"bitwarden-broker needs BITWARDEN_API_TOKEN_FILE={API_TOKEN_FILE}")
    check_providers(broker)
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
    if API_TOKEN_FILE not in test:
        checks.fail("bitwarden-broker healthcheck sends the bearer token: the API refuses a call without it")
    want = {
        "/store/instances": (True, f"{ADDON_DATA}/instances"),
        "/secrets": (True, f"{ADDON_DATA}/secrets"),
        "/data/bw": (False, f"{ADDON_DATA}/bw"),
        "/data/broker": (False, f"{ADDON_DATA}/broker"),
        API_TOKEN_DIR: (True, f"{ADDON_DATA}/broker-api"),
        STACK_TOKEN_FILE: (True, "web-browser/secrets/credentials"),
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
    extra = {v.get("target") for v in mounts(broker)} - set(want) - {SIGNIN_DIR}
    if extra:
        checks.fail(f"bitwarden-broker mounts more than it needs: {sorted(extra)}")
    vol = mount_at(broker, SIGNIN_DIR)
    if vol is None or vol.get("type") != "volume" or vol.get("source") != SIGNIN_VOLUME or not vol.get("read_only"):
        checks.fail(f"bitwarden-broker mounts the {SIGNIN_VOLUME} volume read-only at {SIGNIN_DIR}, not {vol!r}")
    if env_value(broker, "BITWARDEN_SIGNIN_SOCKET") != SIGNIN_SOCKET:
        checks.fail(f"bitwarden-broker needs BITWARDEN_SIGNIN_SOCKET={SIGNIN_SOCKET}")
    if mount_at(broker, "/accounts") is not None or "compose/bitwarden/" in str(broker.get("volumes")):
        checks.fail("bitwarden-broker still mounts the account files")
    if env_value(broker, "BITWARDEN_INSTANCES_DIR") != "/store/instances":
        checks.fail("bitwarden-broker needs BITWARDEN_INSTANCES_DIR=/store/instances")
    if env_value(broker, "BITWARDEN_ACCOUNTS_DIR") is not None:
        checks.fail("bitwarden-broker still sets BITWARDEN_ACCOUNTS_DIR")


def check_providers(broker: dict) -> None:
    """BROWSER_PROVIDERS: the browsers a fill or a handover may be sent to, by name.

    The broker holds each one's endpoint and token, so nothing in hermes needs them. stack is this
    stack's web-browser service; boundary's relay writes its own endpoint and token into the files
    under secrets/, which the broker already mounts."""
    raw = env_value(broker, "BROWSER_PROVIDERS")
    if raw is None:
        checks.fail("bitwarden-broker needs BROWSER_PROVIDERS")
        return
    try:
        providers = json.loads(raw)
    except ValueError as exc:
        checks.fail(f"BROWSER_PROVIDERS is not JSON: {exc}")
        return
    if sorted(providers) != ["boundary", "stack"]:
        checks.fail(f"BROWSER_PROVIDERS names {sorted(providers)!r}, want ['boundary', 'stack']")
        return
    stack = providers["stack"]
    if stack.get("url") != STACK_PROVIDER_URL:
        checks.fail(f"the stack provider is {stack.get('url')!r}, want {STACK_PROVIDER_URL}")
    if stack.get("token_file") != STACK_TOKEN_FILE:
        checks.fail(f"the stack provider's token is {stack.get('token_file')!r}, want {STACK_TOKEN_FILE}")
    boundary = providers["boundary"]
    # Boundary's relay endpoint is not known until it writes it, so the broker reads it from a file.
    if not str(boundary.get("url_file", "")).startswith("/secrets/"):
        checks.fail(f"the boundary provider reads its endpoint from /secrets/..., not {boundary!r}")
    if not str(boundary.get("token_file", "")).startswith("/secrets/"):
        checks.fail(f"the boundary provider reads its token from /secrets/..., not {boundary!r}")
    # The broker has egress, so the endpoint Boundary writes must name the host or the token stays put.
    if sorted(boundary.get("hosts") or []) != ["host.containers.internal", "host.docker.internal"]:
        checks.fail(f"the boundary provider's hosts are {boundary.get('hosts')!r}, "
                    "want host.docker.internal and host.containers.internal")


def check_signin(cfg: dict) -> None:
    """The sign-in Chromium: hardened, alone on its egress network, reached
    only over the socket volume it shares with the broker."""
    services = cfg.get("services", {})
    signin = services.get(SIGNIN)
    if signin is None:
        checks.fail(f"{SIGNIN} missing from core,bitwarden")
        return
    if signin.get("profiles") != ["bitwarden"]:
        checks.fail(f"{SIGNIN} profiles are {signin.get('profiles')!r}, want ['bitwarden']")
    if signin.get("image") != "assistant-bitwarden-signin:local":
        checks.fail(f"{SIGNIN} image is {signin.get('image')!r}, want assistant-bitwarden-signin:local")
    build = signin.get("build") or {}
    if not str(build.get("context", "")).endswith("compose/bitwarden-signin"):
        checks.fail(f"{SIGNIN} builds from {build.get('context')!r}, want compose/bitwarden-signin")
    hermes_image = (services.get("hermes") or {}).get("image", "")
    tag = (build.get("args") or {}).get("HERMES_AGENT_IMAGE_TAG")
    if not tag or hermes_image != f"nousresearch/hermes-agent:{tag}":
        checks.fail(f"{SIGNIN} builds FROM hermes-agent:{tag}, but hermes runs {hermes_image!r}")
    if signin.get("user") != HOST_USER:
        checks.fail(f"{SIGNIN} runs as HERMES_UID:HERMES_GID ({HOST_USER}), not {signin.get('user')!r}")
    if signin.get("read_only") is not True:
        checks.fail(f"{SIGNIN} needs read_only: true")
    if signin.get("cap_drop") != ["ALL"]:
        checks.fail(f"{SIGNIN} cap_drop is {signin.get('cap_drop')!r}, want ['ALL']")
    if signin.get("cap_add"):
        checks.fail(f"{SIGNIN} adds capabilities {signin.get('cap_add')!r}")
    if signin.get("privileged"):
        checks.fail(f"{SIGNIN} is privileged")
    opts = signin.get("security_opt") or []
    if sorted(opts) != sorted(["no-new-privileges:true", f"seccomp=./{SIGNIN_SECCOMP}"]):
        checks.fail(f"{SIGNIN} security_opt is {opts!r}, want no-new-privileges and seccomp=./{SIGNIN_SECCOMP}")
    if not (ROOT / SIGNIN_SECCOMP).is_file():
        checks.fail(f"{SIGNIN_SECCOMP} is missing")
    if signin.get("tmpfs") != ["/tmp:size=512m"]:
        checks.fail(f"{SIGNIN} tmpfs is {signin.get('tmpfs')!r}, want ['/tmp:size=512m']")
    if str(signin.get("mem_limit")) != str(768 * 1024 * 1024):
        checks.fail(f"{SIGNIN} mem_limit is {signin.get('mem_limit')!r}, want 768m")
    if signin.get("init") is not True:
        checks.fail(f"{SIGNIN} needs init: true to reap Chromium's crashpad handlers")
    if networks(signin) != {"signin"}:
        checks.fail(f"{SIGNIN} networks are {sorted(networks(signin))}, want ['signin']")
    if signin.get("network_mode"):
        checks.fail(f"{SIGNIN} must not use network_mode")
    if signin.get("ports") or signin.get("expose"):
        checks.fail(f"{SIGNIN} publishes or exposes a port")
    if signin.get("env_file"):
        checks.fail(f"{SIGNIN} reads an env_file")
    for key, want in (("BITWARDEN_SIGNIN_SOCKET", SIGNIN_SOCKET), ("BITWARDEN_SIGNIN_IDLE_MINUTES", "5"),
                      ("BITWARDEN_SIGNIN_ACTION_SECONDS", "30")):
        if env_value(signin, key) != want:
            checks.fail(f"{SIGNIN} needs {key}={want}, not {env_value(signin, key)!r}")
    vols = signin.get("volumes") or []
    want_vol = {"type": "volume", "source": SIGNIN_VOLUME, "target": SIGNIN_DIR}
    if len(vols) != 1 or {k: vols[0].get(k) for k in want_vol} != want_vol or vols[0].get("read_only"):
        checks.fail(f"{SIGNIN} mounts {vols!r}, want only {SIGNIN_VOLUME} read-write at {SIGNIN_DIR}")
    test = (signin.get("healthcheck") or {}).get("test") or []
    joined = " ".join(test)
    if test[:1] != ["CMD"] or "AF_UNIX" not in joined or SIGNIN_SOCKET not in joined \
            or "'GET', '/health'" not in joined:
        checks.fail(f"{SIGNIN} healthcheck must GET /health over {SIGNIN_SOCKET}")
    if "'sandbox_unavailable'" not in joined:
        checks.fail(f"{SIGNIN} healthcheck must count sandbox_unavailable as up: a signer that answers "
                    "refuses sessions but still serves in-page fills, and compose up --wait fails otherwise")

    net = (cfg.get("networks") or {}).get("signin")
    if net is None:
        checks.fail("network signin missing from compose config")
    elif net.get("internal"):
        checks.fail("network signin carries the signer's egress, so it is not internal")
    for name, service in services.items():
        if name != SIGNIN and "signin" in networks(service):
            checks.fail(f"{name} joins the signin network")
        uses = any(v.get("type") == "volume" and v.get("source") == SIGNIN_VOLUME for v in mounts(service))
        if uses != (name in ("bitwarden-broker", SIGNIN)):
            checks.fail(f"{name} {'mounts' if uses else 'does not mount'} the {SIGNIN_VOLUME} volume")
    # A tmpfs owned by HERMES_UID, shared by the two containers: the owner
    # follows HERMES_UID at run time, which an image-made folder cannot.
    vol = (cfg.get("volumes") or {}).get(SIGNIN_VOLUME) or {}
    opts = vol.get("driver_opts") or {}
    o = set(str(opts.get("o", "")).split(","))
    if opts.get("type") != "tmpfs" or opts.get("device") != "tmpfs" \
            or not {"uid=1000", "gid=1000", "mode=0700"} <= o:
        checks.fail(f"volume {SIGNIN_VOLUME} must be a tmpfs, mode 0700, owned by HERMES_UID:HERMES_GID, not {opts!r}")


def check_signin_dockerfile() -> None:
    """The signer's base tag: one pin. The Dockerfile's ARG default is only for
    a bare docker build, so it must equal docker-compose.yml's, which a Hermes
    bump changes."""
    text = (ROOT / "compose/bitwarden-signin/Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ARG HERMES_AGENT_IMAGE_TAG=(\S+)$", text, re.M)
    if not m:
        checks.fail("compose/bitwarden-signin/Dockerfile has no ARG HERMES_AGENT_IMAGE_TAG=<tag> default")
    elif m.group(1) != hermes_image.compose_default_tag():
        checks.fail(f"compose/bitwarden-signin/Dockerfile pins {m.group(1)}, "
                    f"docker-compose.yml pins {hermes_image.compose_default_tag()}")
    if not re.search(r"^FROM nousresearch/hermes-agent:\$\{HERMES_AGENT_IMAGE_TAG\}$", text, re.M):
        checks.fail("compose/bitwarden-signin/Dockerfile must build FROM "
                    "nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG}")


def check_broker_rebuild_documented() -> None:
    """The release that introduces bitwarden-signin must tell operators to rebuild
    the broker.

    The broker's image is built from compose/bitwarden-broker and tagged
    <prefix>-bitwarden-broker:local. ``docker compose up`` reuses a tag it already
    has, so an install upgrading into this release keeps a broker with no
    /signin/ routes while bitwarden-signin starts and reports ready. Only the
    CHANGELOG's upgrade steps close that gap. The check applies to the topmost
    section, named Unreleased before the tag and v0.3.16 after it, and retires
    itself once a later section no longer introduces the signer."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", text)
    if len(sections) < 2:
        checks.fail("CHANGELOG.md has no '## ' section")
        return
    top = sections[1]
    if SIGNIN not in top:
        return
    if not re.search(r"--build\s+bitwarden-broker", top):
        checks.fail("CHANGELOG.md's newest section introduces bitwarden-signin but its "
                    "upgrade steps never say to rebuild bitwarden-broker "
                    "(--build bitwarden-broker); an upgraded install keeps a broker "
                    "with no /signin/ routes")


def check_signin_follows_uid() -> None:
    """The socket volume's owner and the signer's user follow HERMES_UID:HERMES_GID."""
    cfg = compose_config.render("core,bitwarden", {"HERMES_UID": "1234", "HERMES_GID": "1235"})
    o = set(str((((cfg.get("volumes") or {}).get(SIGNIN_VOLUME) or {}).get("driver_opts") or {}).get("o", "")).split(","))
    if not {"uid=1234", "gid=1235"} <= o:
        checks.fail(f"volume {SIGNIN_VOLUME} owner does not follow HERMES_UID:HERMES_GID: {sorted(o)}")
    user = ((cfg.get("services") or {}).get(SIGNIN) or {}).get("user")
    if user != "1234:1235":
        checks.fail(f"{SIGNIN} user does not follow HERMES_UID:HERMES_GID: {user!r}")


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
        if source.endswith(f"{ADDON_DATA}/broker-api"):
            continue  # the API's bearer token, below
        if ADDON_DATA in source or "compose/bitwarden/" in source:
            checks.fail(f"hermes ({backend}) mounts {source}")
    # The one Bitwarden folder hermes reads: the bearer token for the broker's API, which the
    # plugin and the fill overlay send. Its own folder, so no other secret comes with it, and
    # mounted whatever BITWARDEN_ENABLED says, because compose needs the bind source to exist.
    vol = mount_at(hermes, API_TOKEN_DIR)
    if vol is None or not str(vol.get("source", "")).endswith(f"{ADDON_DATA}/broker-api"):
        checks.fail(f"hermes ({backend}) mounts {ADDON_DATA}/broker-api at {API_TOKEN_DIR}, not {vol!r}")
    elif not vol.get("read_only"):
        checks.fail(f"hermes ({backend}) must mount {API_TOKEN_DIR} read-only")
    if "credentials" not in networks(hermes):
        checks.fail(f"hermes ({backend}) is not on the credentials network, which carries the broker API")
    credentials = (cfg.get("networks") or {}).get("credentials")
    if not credentials:
        checks.fail("network credentials missing from compose config")
    elif not credentials.get("internal"):
        checks.fail("network credentials must be internal")
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
    # The row comes from addons/bitwarden/addon.json's gate; --table lists it.
    rows = subprocess.run([str(ROOT / "scripts/sync-profiles.sh"), "--table"], capture_output=True,
                          text=True, check=True).stdout.splitlines()
    if "BITWARDEN_LOCAL_ENABLED vault 0" not in rows:
        checks.fail("sync-profiles.sh must map BITWARDEN_LOCAL_ENABLED to vault, default off")


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Answers the healthcheck's GET /health with the body the server carries."""

    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _HealthServer(socketserver.ThreadingUnixStreamServer):
    body = b"{}"


def run_healthcheck(script: str, socket_path: Path, status: str | None) -> int:
    """The healthcheck command's exit code against a signer that answers with
    ``status``, or (status None) against a socket nothing listens on."""
    server = None
    thread = None
    # ThreadingUnixStreamServer leaves the socket file behind, and bind refuses an
    # existing path, so each run starts from none.
    socket_path.unlink(missing_ok=True)
    if status is not None:
        server = _HealthServer(str(socket_path), _HealthHandler)
        server.body = json.dumps({"status": status}).encode()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    try:
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        return proc.returncode
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def check_signin_healthcheck(cfg: dict) -> None:
    """Run the compose healthcheck command itself: a signer that answers is up,
    sandbox_unavailable included; anything else is unhealthy."""
    signin = (cfg.get("services") or {}).get(SIGNIN) or {}
    test = (signin.get("healthcheck") or {}).get("test") or []
    if len(test) < 4 or test[0] != "CMD":
        checks.fail(f"{SIGNIN} healthcheck is {test!r}, want a CMD with an inline script")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signin.sock"
        script = test[-1].replace(SIGNIN_SOCKET, str(path))
        if str(path) not in script:
            checks.fail(f"{SIGNIN} healthcheck does not name {SIGNIN_SOCKET}")
            return
        for status in ("ready", "busy", "sandbox_unavailable"):
            code = run_healthcheck(script, path, status)
            if code != 0:
                checks.fail(f"{SIGNIN} healthcheck exits {code} for status {status}, want 0")
        for status in ("stopped", "down"):
            code = run_healthcheck(script, path, status)
            if code == 0:
                checks.fail(f"{SIGNIN} healthcheck passes for status {status}")
        if run_healthcheck(script, path, None) == 0:
            checks.fail(f"{SIGNIN} healthcheck passes with nothing listening on the socket")


def main() -> None:
    check_off()
    check_profile_flags()
    check_broker(config("core,bitwarden"))
    check_signin(config("core,bitwarden"))
    check_signin_healthcheck(config("core,bitwarden"))
    check_signin(config("core,bitwarden,bitwarden-tools,vault"))
    check_signin_follows_uid()
    check_broker_rebuild_documented()
    check_signin_dockerfile()
    check_store_follows_data_root()
    for backend in ("ssh", "local"):
        cfg = config("core,bitwarden,bitwarden-tools", backend)
        check_hermes(cfg, backend)
    check_tools(config("core,bitwarden,bitwarden-tools"))
    check_local(config("core,bitwarden,bitwarden-tools,vault"))
    checks.finish("ok")


if __name__ == "__main__":
    main()
