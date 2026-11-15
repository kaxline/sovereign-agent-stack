#!/usr/bin/env python3
"""Resolved compose config for the browser service: the profile gate, the
sandbox and hardening, what reaches the gateway and what does not, and the
viewer's loopback port.

Needs a .env that satisfies compose interpolation, as CI writes. Prints no
environment values.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "web-browser"
GATEWAY_PORT = 9222
VIEWER_PORT = 6080
SECRETS_DIR = "/run/secrets"
PROFILE_DIR = "/profile"
PROFILE_VOLUME = "web_browser_profile"
SECCOMP = "compose/bitwarden-signin/seccomp.json"
HOST_USER = "1000:1000"  # HERMES_UID:HERMES_GID's defaults
checks = Checks()


def config(profiles: str) -> dict:
    return compose_config.render(profiles, {"HERMES_TERMINAL_BACKEND": "ssh"})


def networks(service: dict) -> set:
    return set(service.get("networks") or {})


def mount_at(service: dict, target: str) -> dict | None:
    return next((v for v in service.get("volumes") or [] if isinstance(v, dict)
                 and v.get("target") == target), None)


def main() -> None:
    # --- the gate: nothing without the profile
    core = config("core")
    checks.expect(SERVICE not in core.get("services", {}),
                  f"{SERVICE} is absent without its profile")
    # hermes joins the internal networks whether or not their service runs, as it
    # does for vault, budget and email: an internal network with no other member
    # reaches nothing.
    checks.expect({"browser", "credentials"} <= networks(core["services"]["hermes"]),
                  "hermes joins the browser and credentials networks")

    rendered = config("core,web-browser,bitwarden")
    services = rendered.get("services", {})
    if SERVICE not in services:
        checks.fail(f"{SERVICE} is missing from COMPOSE_PROFILES=core,web-browser")
        checks.finish()
    service = services[SERVICE]

    # --- hardening: as locked down as bitwarden-signin, which also runs Chromium
    checks.expect(service.get("read_only") is True, f"{SERVICE} runs read-only")
    checks.expect(service.get("user") == HOST_USER, f"{SERVICE} runs as the host user")
    checks.expect(service.get("init") is True, f"{SERVICE} reaps Chromium's crashpad handlers (init)")
    checks.expect(service.get("cap_drop") == ["ALL"], f"{SERVICE} drops every capability")
    opts = service.get("security_opt") or []
    checks.expect("no-new-privileges:true" in opts, f"{SERVICE} sets no-new-privileges")
    checks.expect(any(str(o).startswith("seccomp=") and SECCOMP in str(o) for o in opts),
                  f"{SERVICE} uses the seccomp profile Chromium's sandbox needs ({SECCOMP})")
    checks.expect((ROOT / SECCOMP).is_file(), f"{SECCOMP} is in the repository")

    # --- the profile volume and the token files
    secrets = mount_at(service, SECRETS_DIR)
    checks.expect(secrets is not None and secrets.get("read_only") is True
                  and secrets.get("source", "").endswith("data/web-browser/secrets"),
                  f"the client tokens are mounted read-only at {SECRETS_DIR}, got {secrets}")
    profile = mount_at(service, PROFILE_DIR)
    checks.expect(profile is not None and profile.get("source") == PROFILE_VOLUME,
                  f"the Chromium profile is the {PROFILE_VOLUME} volume, got {profile}")
    checks.expect(PROFILE_VOLUME in (rendered.get("volumes") or {}),
                  f"{PROFILE_VOLUME} is declared")

    # --- ports: the viewer on loopback, the gateway on no host port at all
    published = service.get("ports") or []
    checks.expect(len(published) == 1, f"{SERVICE} publishes one port (the viewer), got {published}")
    for entry in published:
        checks.expect(entry.get("host_ip") == "127.0.0.1" and int(entry.get("target")) == VIEWER_PORT,
                      f"the viewer is published on 127.0.0.1 only, got {entry}")
    checks.expect(not any(int(e.get("target", 0)) == GATEWAY_PORT for e in published),
                  "the CDP gateway has no host port")

    # --- networks: hermes and the broker reach the gateway, hermes-worker does not
    checks.expect(networks(service) == {"default", "browser"},
                  f"{SERVICE} joins browser and default (for egress), got {networks(service)}")
    nets = rendered.get("networks") or {}
    checks.expect(nets.get("browser", {}).get("internal") is True,
                  "the browser network is internal")
    checks.expect("browser" in networks(services["hermes"]),
                  "hermes joins the browser network")
    worker = services.get("hermes-worker")
    if worker is not None:
        checks.expect("browser" not in networks(worker),
                      "hermes-worker does not join the browser network")
    # --- the healthcheck asks the gateway itself
    test = " ".join(str(part) for part in (service.get("healthcheck") or {}).get("test") or [])
    checks.expect(str(GATEWAY_PORT) in test and "devtools/browser" in test,
                  f"the healthcheck connects to the gateway, got {test!r}")

    # --- the switch is off by default and known to sync-profiles
    env_example = (ROOT / ".env.example").read_text()
    checks.expect("WEB_BROWSER_ENABLED=0" in env_example,
                  "WEB_BROWSER_ENABLED=0 is in .env.example")
    sync = (ROOT / "scripts" / "sync-profiles.sh").read_text()
    checks.expect("WEB_BROWSER_ENABLED web-browser" in sync,
                  "sync-profiles.sh maps WEB_BROWSER_ENABLED to the web-browser profile")

    checks.finish("ok web-browser compose config")


if __name__ == "__main__":
    main()
