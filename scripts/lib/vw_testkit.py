"""A throwaway Vaultwarden behind the stack's TLS sidecar, for tests that need a real vault.

``Vaultwarden`` holds everything scripts/test-vw-register.py and
scripts/test-bitwarden-signin-e2e.py both need: the broker image (built here
unless BITWARDEN_TEST_IMAGE names one), a temporary folder standing in for
BITWARDEN_HOST_DATA_DIR with ``local/`` and ``secrets/``, ``setup.py`` runs in
that image, two internal networks and the pinned Vaultwarden and nginx
containers configured as the ``vaultwarden`` and ``vaultwarden-tls`` services
are.

Every name carries the instance's own random tag, so parallel runs do not
collide and nothing here touches the running stack. ``cleanup`` removes the
containers, the networks, an image this instance built and the temporary
folder; call it from a ``finally``.

Order: ``build()``, then ``setup_run("tls")`` and ``setup_run("admin-token")``
(the sidecar needs the certificates and Vaultwarden the hashed token), then
``start()``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROKER_SRC = ROOT / "compose/bitwarden-broker"
TEMPLATE = ROOT / "compose/bitwarden-tls/default.conf.template"
VW_IMAGE = ("vaultwarden/server:1.37.5@sha256:"
            "4fe17f415f90087377d37c755e0ea3cf189dcc6794e49cdd2a9adbb4937d9e1f")
NGINX_IMAGE = "nginx:1.30.6-alpine"
LOCAL_NAME = "vaultwarden.vault.internal"
BASE = f"https://{LOCAL_NAME}"
# Compose runs the broker and bitwarden-setup as HERMES_UID:HERMES_GID, the host user; the image has
# no passwd entry for it.
USER = f"{os.getuid()}:{os.getgid()}"
# The one setting the vaultwarden service leaves at its default (10 password grants a minute per
# address). These tests sign in more often than that.
LOGIN_BURST = 60


class StartError(Exception):
    """A step of the bring-up failed. The text names the step and docker's last line."""


def docker(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def available() -> bool:
    return shutil.which("docker") is not None and docker("info", timeout=30).returncode == 0


class Vaultwarden:
    def __init__(self, prefix: str = "bwtest", login_burst: int = LOGIN_BURST, src: Path | None = None):
        self.tag = secrets.token_hex(4)
        self.prefix = f"{prefix}-{self.tag}"
        self.net = f"{self.prefix}-net"
        self.back = f"{self.prefix}-back"
        self.vw = f"{self.prefix}-vw"
        self.tls = f"{self.prefix}-tls"
        self.gen = f"{self.prefix}-gen"
        self.login_burst = login_burst
        self.src = Path(src or BROKER_SRC).resolve()
        self.image = os.environ.get("BITWARDEN_TEST_IMAGE") or f"{prefix}-broker:{self.tag}"
        self.built = False
        self.tmp = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
        # bitwarden-setup's /local and /secrets, as BITWARDEN_HOST_DATA_DIR/local and /secrets.
        self.local = self.tmp / "local"
        self.secrets_dir = self.tmp / "secrets"
        for folder in (self.local, self.secrets_dir):
            folder.mkdir(mode=0o700)
            os.chmod(folder, 0o700)  # the containers write here as the host user, as compose runs them
        (self.tmp / "vwdata").mkdir()
        # Containers and networks cleanup removes, in this order.
        self._containers = [self.gen, self.tls, self.vw]
        self._networks = [self.net, self.back]
        self.output: list[str] = []

    # --- the broker image and setup.py --------------------------------------

    def build(self) -> None:
        if os.environ.get("BITWARDEN_TEST_IMAGE"):
            return
        proc = docker("build", "-q", "-t", self.image, str(self.src), timeout=900)
        if proc.returncode != 0:
            raise StartError(f"docker build {self.src}: {proc.stderr.strip().splitlines()[-1:]}")
        self.built = True

    def setup_run(self, *args: str) -> subprocess.CompletedProcess:
        """``setup/setup.py ARGS`` in the broker image, with ``local/`` and ``secrets/`` mounted."""
        proc = docker("run", "--rm", "--name", self.gen, "--user", USER, "--read-only", "--tmpfs", "/tmp",
                      "--entrypoint", "python3", "-v", f"{self.src}:/app:ro", "-v", f"{self.local}:/local",
                      "-v", f"{self.secrets_dir}:/secrets",
                      self.image, "/app/setup/setup.py", *args)
        self.output.append(proc.stdout + proc.stderr)
        return proc

    def prepare(self) -> None:
        """build(), then the TLS material and the admin token the containers need."""
        self.build()
        for step in ("tls", "admin-token"):
            proc = self.setup_run(step)
            if proc.returncode != 0:
                raise StartError(f"setup.py {step}: exit {proc.returncode}: {proc.stdout.strip()[-400:]}")

    @property
    def admin_token(self) -> str:
        return (self.local / "admin_token").read_text(encoding="utf-8").strip()

    @property
    def ca_cert(self) -> Path:
        return self.secrets_dir / "local/tls/ca.crt"

    # --- the containers -----------------------------------------------------

    def start(self) -> None:
        """The two internal networks, Vaultwarden and the TLS sidecar.

        As in compose (Ruling R20): ``net`` stands for the vault network, which the broker and
        bitwarden-setup are on, ``back`` for vault-backend, which only Vaultwarden and the sidecar
        are on.
        """
        for name in (self.net, self.back):
            proc = docker("network", "create", "--internal", name)
            if proc.returncode != 0:
                raise StartError(f"docker network create {name}: {proc.stderr.strip()}")
        proc = docker("run", "-d", "--name", self.vw, "--network", self.back, "--network-alias", "vaultwarden",
                      "-e", "SIGNUPS_ALLOWED=false", "-e", "INVITATIONS_ALLOWED=true",
                      "-e", "DOMAIN=http://localhost:8222", "-e", "DISABLE_ICON_DOWNLOAD=true",
                      "--security-opt", "no-new-privileges:true",
                      "-e", f"LOGIN_RATELIMIT_MAX_BURST={self.login_burst}",
                      "-e", "ADMIN_TOKEN_FILE=/run/vaultwarden/admin_token.phc",
                      "-v", f"{self.tmp / 'vwdata'}:/data",
                      "-v", f"{self.local / 'vaultwarden'}:/run/vaultwarden:ro", VW_IMAGE)
        if proc.returncode != 0:
            raise StartError(f"start Vaultwarden: {proc.stderr.strip()}")
        proc = docker("run", "-d", "--name", self.tls, "--network", self.net, "--network-alias", LOCAL_NAME,
                      "--security-opt", "no-new-privileges:true",
                      "-e", "NGINX_ENVSUBST_FILTER=^VAULTWARDEN_TLS_",
                      "-v", f"{TEMPLATE}:/etc/nginx/templates/default.conf.template:ro",
                      "-v", f"{self.local / 'tls/server'}:/etc/nginx/tls:ro", NGINX_IMAGE)
        if proc.returncode != 0:
            raise StartError(f"start the TLS sidecar: {proc.stderr.strip()}")
        proc = docker("network", "connect", self.back, self.tls)
        if proc.returncode != 0:
            raise StartError(f"connect the TLS sidecar to the backend network: {proc.stderr.strip()}")

    def also_remove(self, *containers: str) -> None:
        """Containers the caller starts, removed before this instance's own."""
        self._containers[:0] = containers

    def container_logs(self, *names: str) -> dict[str, str]:
        out = {}
        for name in (*names, self.tls, self.vw):
            proc = docker("logs", name, timeout=120)
            out[name] = proc.stdout + proc.stderr
        return out

    def state(self, name: str) -> str:
        return docker("inspect", "-f", "{{.State.Status}} exit {{.State.ExitCode}}", name).stdout.strip()

    def cleanup(self) -> None:
        """Remove everything this instance made. One call per name, so a failure skips nothing."""
        steps = [("rm", "-f", name) for name in self._containers]
        steps += [("network", "rm", name) for name in self._networks]
        if self.built:
            steps.append(("image", "rm", self.image))
        for args in steps:
            try:
                docker(*args, timeout=120)
            except (OSError, subprocess.SubprocessError):
                print(f"warning: docker {' '.join(args)} failed; remove it by that name")
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except OSError:
            pass
