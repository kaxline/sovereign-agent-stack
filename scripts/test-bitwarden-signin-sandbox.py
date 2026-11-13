#!/usr/bin/env python3
"""Chromium's sandbox in the bitwarden-signin image, run as compose runs it.

Builds compose/bitwarden-signin FROM the pinned Hermes image and starts it with
the service's rendered settings (user, read-only root, no capabilities,
no-new-privileges, the seccomp profile, init, tmpfs, memory limit, environment)
and a copy of the bitwarden_signin tmpfs volume. Then:

- the service's healthcheck passes and GET /health says ready, so the signer's
  startup probe started Chromium with its sandbox on;
- a second container, standing in for the broker (same uid, the volume
  mounted read-only), reaches the socket, which is mode 0600 and owned by
  HERMES_UID; another uid cannot;
- the same container under Docker's default seccomp profile reports
  sandbox_unavailable and its healthcheck fails, so the profile is what lets
  the sandbox start and the signer does not fall back to --no-sandbox.

Uses throwaway names (container, volume, image tag) and removes them. Never
touches the running stack. Exit 77 without Docker or the Hermes image.
Docker Desktop runs it here; a Linux Docker host is a separate run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import compose_config  # noqa: E402
import hermes_image  # noqa: E402
from testkit import expect, fail  # noqa: E402

SERVICE = "bitwarden-signin"
VOLUME = "bitwarden_signin"
SOCKET = "/run/bitwarden-signin/signin.sock"
PYTHON = "/opt/hermes/.venv/bin/python"
NAME = f"bwsignin-sandbox-test-{os.getpid()}"
IMAGE = f"{NAME}:local"

# GET /health over the socket; prints the JSON body.
HEALTH = (
    "import http.client, socket, sys\n"
    "c = http.client.HTTPConnection('localhost', timeout=5)\n"
    "c.sock = socket.socket(socket.AF_UNIX)\n"
    "c.sock.settimeout(5)\n"
    f"c.sock.connect({SOCKET!r})\n"
    "c.request('GET', '/health')\n"
    "print(c.getresponse().read().decode())\n"
)
STAT = f"import os; s = os.stat({SOCKET!r}); print(oct(s.st_mode & 0o777), s.st_uid, s.st_gid)"


def docker(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        fail(f"docker {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    return proc


def skip(reason: str) -> None:
    print(f"SKIP {reason}")
    raise SystemExit(77)


def run_args(service: dict, profile: bool = True) -> list[str]:
    """docker run flags for the service's hardening, from the rendered config.
    profile=False leaves out its seccomp profile, so Docker's default applies."""
    args = ["--user", service["user"], "--network", "none"]
    if service.get("read_only"):
        args.append("--read-only")
    if service.get("init"):
        args.append("--init")
    for cap in service.get("cap_drop") or []:
        args += ["--cap-drop", cap]
    for opt in service.get("security_opt") or []:
        if opt.startswith("seccomp="):
            if not profile:
                continue
            # compose reads the path from the project directory; docker run from its cwd.
            opt = "seccomp=" + str((ROOT / opt.split("=", 1)[1]).resolve())
        args += ["--security-opt", opt]
    for mount in service.get("tmpfs") or []:
        args += ["--tmpfs", mount]
    if service.get("mem_limit"):
        args += ["--memory", str(service["mem_limit"])]
    for key, value in (service.get("environment") or {}).items():
        args += ["-e", f"{key}={value}"]
    return args


def wait_for_socket(container: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if docker("exec", container, PYTHON, "-c", f"import os; os.stat({SOCKET!r})", check=False).returncode == 0:
            return
        state = docker("inspect", "-f", "{{.State.Running}}", container, check=False).stdout.strip()
        if state != "true":
            logs = docker("logs", container, check=False)
            fail(f"{container} stopped before listening: {(logs.stdout + logs.stderr).strip()[-800:]}")
        time.sleep(1)
    fail(f"{container} did not create {SOCKET} within 90 s")


def health(container: str) -> dict:
    out = docker("exec", container, PYTHON, "-c", HEALTH, check=False)
    expect(out.returncode == 0, f"GET /health in {container} failed: {out.stderr.strip()[-400:]}")
    return json.loads(out.stdout)


def healthcheck_exit(container: str, test: list[str]) -> int:
    expect(test[:1] == ["CMD"], f"healthcheck is {test[:1]}, want an exec form")
    return docker("exec", container, *test[1:], check=False).returncode


def main() -> None:
    if not shutil.which("docker") or docker("info", check=False, timeout=30).returncode != 0:
        skip("no Docker daemon")
    # The image is the build's base: --host-only or a missing image skips.
    if hermes_image.image_gate() is None:
        skip(f"sandbox check needs nousresearch/hermes-agent:{hermes_image.image_tag()}")

    cfg = compose_config.render("core,bitwarden")
    service = (cfg.get("services") or {}).get(SERVICE)
    expect(service is not None, f"{SERVICE} missing from core,bitwarden")
    expect(service.get("init") is True, f"{SERVICE} runs with init")
    volume = (cfg.get("volumes") or {}).get(VOLUME)
    expect(volume is not None, f"volume {VOLUME} missing")
    flags = run_args(service)
    expect(any(f.startswith("seccomp=") for f in flags), f"{SERVICE} has a seccomp profile")
    test = service["healthcheck"]["test"]
    uid = service["user"].split(":")[0]

    signer, default_sc, client = f"{NAME}-signer", f"{NAME}-default", f"{NAME}-client"
    vol_name = f"{NAME}-vol"
    try:
        build_arg = f"HERMES_AGENT_IMAGE_TAG={service['build']['args']['HERMES_AGENT_IMAGE_TAG']}"
        docker("build", "-q", "-t", IMAGE, "--build-arg", build_arg, service["build"]["context"])
        opts = volume.get("driver_opts") or {}
        docker("volume", "create", *[a for k, v in opts.items() for a in ("--opt", f"{k}={v}")], vol_name)
        mount = ["-v", f"{vol_name}:/run/bitwarden-signin"]

        docker("run", "-d", "--name", signer, *flags, *mount, IMAGE)
        wait_for_socket(signer)
        expect(docker("exec", signer, "cat", "/proc/1/cmdline", check=False).stdout.startswith("/sbin/docker-init"),
               "PID 1 is docker-init, which reaps Chromium's crashpad handlers")
        expect(health(signer) == {"status": "ready"}, f"/health with the profile: {health(signer)}")
        expect(healthcheck_exit(signer, test) == 0, "the service healthcheck passes with the profile")

        # The broker's side: same uid, read-only mount of the volume.
        reader = ["--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges:true", "--entrypoint", PYTHON,
                  "-v", f"{vol_name}:/run/bitwarden-signin:ro"]
        out = docker("run", "--name", client, "--user", service["user"], *reader, IMAGE, "-c", HEALTH, check=False)
        expect(out.returncode == 0 and json.loads(out.stdout or "{}") == {"status": "ready"},
               f"a second container reaches the socket over a read-only mount: {out.stderr.strip()[-400:]}")
        out = docker("run", "--name", client, "--user", service["user"], *reader, IMAGE, "-c", STAT)
        mode, owner, _ = out.stdout.split()
        expect((mode, owner) == ("0o600", uid), f"socket is {mode} owned by {owner}, want 0o600 owned by {uid}")
        out = docker("run", "--name", client, "--user", "4242:4242", *reader, IMAGE, "-c", HEALTH, check=False)
        expect(out.returncode != 0, "another uid cannot reach the socket")
        docker("rm", "-f", signer)

        # Docker's default profile: the sandbox cannot start, and nothing falls back.
        no_profile = run_args(service, profile=False)
        expect(not any("seccomp" in f for f in no_profile), f"default-profile flags {no_profile}")
        docker("run", "-d", "--name", default_sc, *no_profile, *mount, IMAGE)
        wait_for_socket(default_sc)
        got = health(default_sc)
        expect(got == {"status": "sandbox_unavailable"}, f"/health under Docker's default profile: {got}")
        expect(healthcheck_exit(default_sc, test) != 0, "the healthcheck fails when the sandbox is unavailable")
    finally:
        for name in (signer, default_sc, client):
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", vol_name, check=False)
        docker("image", "rm", "-f", IMAGE, check=False)
    print("ok")


if __name__ == "__main__":
    main()
