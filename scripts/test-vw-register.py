#!/usr/bin/env python3
"""Local mode end to end: setup/vw_register.py, setup/tls.py, the admin token
and the TLS sidecar against a scratch Vaultwarden, with the real bw.

    python3 scripts/test-vw-register.py

On a throwaway internal network it starts the pinned Vaultwarden (configured
as the vaultwarden service is) and the pinned nginx with
compose/bitwarden-tls/default.conf.template as vaultwarden.vault.internal.
The certificates and the admin token come from setup.py tls and admin-token in
a temporary folder. A driver container from the broker image (built here, or
BITWARDEN_TEST_IMAGE) then repeats spike 2's sequence with vw_register, the
broker's Bw class, setup.py and bw, and runs the live checks other tasks
deferred to this test:

  1. The Owner's ``bw edit item-collections`` with the ids on stdin takes an item out
     of the Agent collection, and the agent's next sync no longer lists it.
  2. The Owner sees that unassigned item through ``bw list items --organizationid``.
  3. bw's real stderr for a wrong password, a 401 and a missing item, through
     bwcli.classify.
  4. Whether bw prints Retry-After on a 429.

Accounts and passwords are synthetic. No output may contain a password, the
admin token, an API key, an access token or a session key. Containers, the
network and a built image are removed by name when the run ends. Skips when
Docker is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROKER_SRC = ROOT / "compose/bitwarden-broker"
TEMPLATE = ROOT / "compose/bitwarden-tls/default.conf.template"
VW_IMAGE = ("vaultwarden/server:1.37.4@sha256:"
            "efb3cde962015fcc036b2ea625242248611943b27212ebf4392841fb30fad055")
NGINX_IMAGE = "nginx:1.30.6-alpine"
LOCAL_NAME = "vaultwarden.vault.internal"
BASE = f"https://{LOCAL_NAME}"
AGENT_EMAIL = "agent@vault.localhost"
OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"
DRIVER_TIMEOUT = 900
LOGIN_BURST = 60


# Host side

def docker(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def host_main() -> int:
    if shutil.which("docker") is None or docker("info", timeout=30).returncode != 0:
        print("SKIP vw-register: Docker is not available")
        return 77
    src = Path(os.environ.get("BITWARDEN_TEST_SRC") or BROKER_SRC).resolve()
    tag = secrets.token_hex(4)
    net, back, vw, tls, gen, drv = (f"bwtest-{kind}-{tag}" for kind in ("net", "back", "vw", "tls", "gen", "drv"))
    image = os.environ.get("BITWARDEN_TEST_IMAGE") or f"bwtest-broker:{tag}"
    built = False
    tmp = Path(tempfile.mkdtemp(prefix="bwtest-vw-"))
    # bitwarden-setup's /local and /secrets, as BITWARDEN_HOST_DATA_DIR/local and /secrets.
    local, secrets_dir = tmp / "local", tmp / "secrets"
    for d in (local, secrets_dir):
        d.mkdir(mode=0o777)
        os.chmod(d, 0o777)  # the containers write here as uid 10001
    (tmp / "vwdata").mkdir()
    synthetic = {
        "agent_pw": secrets.token_urlsafe(24),
        "owner_pw": f"syn-{secrets.token_urlsafe(12)}+/%é",
        "item_pw": f"syn-item-{secrets.token_hex(8)}",
    }
    (tmp / "synthetic.json").write_text(json.dumps(synthetic), encoding="utf-8")
    os.chmod(tmp / "synthetic.json", 0o644)
    output: list[str] = []
    failed = False
    admin_token = ""

    def note(text: str) -> None:
        output.append(text)
        print(text, flush=True)

    def broker_run(*args: str) -> subprocess.CompletedProcess:
        proc = docker("run", "--rm", "--name", gen, "--user", "10001:10001", "--read-only", "--tmpfs", "/tmp",
                      "--entrypoint", "python3", "-v", f"{src}:/app:ro", "-v", f"{local}:/local",
                      "-v", f"{secrets_dir}:/secrets",
                      image, "/app/setup/setup.py", *args)
        output.append(proc.stdout + proc.stderr)
        return proc

    try:
        if not os.environ.get("BITWARDEN_TEST_IMAGE"):
            proc = docker("build", "-q", "-t", image, str(BROKER_SRC), timeout=900)
            if proc.returncode != 0:
                note(f"FAIL: docker build {BROKER_SRC}: {proc.stderr.strip().splitlines()[-1:]}")
                return 1
            built = True

        # TLS material and the admin token, as vault.sh init makes them, with setup.py's default folders.
        for args in (("tls",), ("admin-token",)):
            proc = broker_run(*args)
            if proc.returncode != 0:
                note(f"FAIL: setup.py {args[0]}: exit {proc.returncode}: {proc.stdout.strip()}")
                return 1
            note(f"ok: setup.py {args[0]}")
        files = {"local/tls/ca.crt": 0o644, "local/tls/ca.key": 0o600, "local/tls/server/server.crt": 0o644,
                 "local/tls/server/server.key": 0o600, "local/admin_token": 0o600,
                 "local/vaultwarden/admin_token.phc": 0o600, "secrets/local/tls/ca.crt": 0o644}
        modes = {name: (tmp / name).stat().st_mode & 0o777 for name in files if (tmp / name).exists()}
        found = sorted(str(f.relative_to(tmp)) for d in (local, secrets_dir) for f in d.rglob("*") if f.is_file())
        if found != sorted(files):
            note(f"FAIL: setup wrote {found}, want {sorted(files)}")
            failed = True
        if (tmp / "secrets/local/tls/ca.crt").read_bytes() != (local / "tls/ca.crt").read_bytes():
            note("FAIL: secrets/local/tls/ca.crt is not a copy of local/tls/ca.crt")
            failed = True
        if modes != files:
            note(f"FAIL: file modes {sorted((k, oct(v)) for k, v in modes.items())}, want "
                 f"{sorted((k, oct(v)) for k, v in files.items())}")
            failed = True
        else:
            note("ok: keys and tokens are mode 600, certificates 644")
        phc = (local / "vaultwarden/admin_token.phc").read_text(encoding="utf-8")
        if not phc.startswith("$argon2id$"):
            note("FAIL: admin_token.phc is not an Argon2id PHC string")
            failed = True
        before = {name: (tmp / name).read_bytes() for name in files}
        for args in (("tls",), ("admin-token",)):
            proc = broker_run(*args)
            if proc.returncode != 0:
                note(f"FAIL: second setup.py {args[0]}: exit {proc.returncode}")
                failed = True
        if {name: (tmp / name).read_bytes() for name in files} != before:
            note("FAIL: a second tls or admin-token run changed the files")
            failed = True
        else:
            note("ok: a second tls and admin-token run keeps every file")
        server = local / "tls/server"
        (server / "server.key").rename(server / "server.key.aside")
        proc = broker_run("tls")
        (server / "server.key.aside").rename(server / "server.key")
        if proc.returncode == 0 or (server / "server.crt").read_bytes() != before["local/tls/server/server.crt"]:
            note("FAIL: tls with server.key missing replaced server.crt instead of refusing")
            failed = True
        else:
            note("ok: tls refuses a half-present server pair")
        admin_token = (local / "admin_token").read_text(encoding="utf-8").strip()

        # As in compose (Ruling R20): net stands for vault (the sidecar and the driver, which plays the
        # broker and bitwarden-setup), back for vault-backend (vaultwarden and the sidecar).
        for name in (net, back):
            proc = docker("network", "create", "--internal", name)
            if proc.returncode != 0:
                note(f"FAIL: docker network create: {proc.stderr.strip()}")
                return 1
        proc = docker("run", "-d", "--name", vw, "--network", back, "--network-alias", "vaultwarden",
                      "-e", "SIGNUPS_ALLOWED=false", "-e", "INVITATIONS_ALLOWED=true",
                      "-e", "DOMAIN=http://localhost:8222", "-e", "DISABLE_ICON_DOWNLOAD=true",
                      "--security-opt", "no-new-privileges:true",
                      # The one setting the vaultwarden service leaves at its default (10 password grants
                      # a minute per address): this sequence signs in more often, and check 4 spends the
                      # allowance on purpose.
                      "-e", f"LOGIN_RATELIMIT_MAX_BURST={LOGIN_BURST}",
                      "-e", "ADMIN_TOKEN_FILE=/run/vaultwarden/admin_token.phc",
                      "-v", f"{tmp / 'vwdata'}:/data",
                      "-v", f"{local / 'vaultwarden'}:/run/vaultwarden:ro", VW_IMAGE)
        if proc.returncode != 0:
            note(f"FAIL: start Vaultwarden: {proc.stderr.strip()}")
            return 1
        proc = docker("run", "-d", "--name", tls, "--network", net, "--network-alias", LOCAL_NAME,
                      "--security-opt", "no-new-privileges:true",
                      "-e", "NGINX_ENVSUBST_FILTER=^VAULTWARDEN_TLS_",
                      "-v", f"{TEMPLATE}:/etc/nginx/templates/default.conf.template:ro",
                      "-v", f"{server}:/etc/nginx/tls:ro", NGINX_IMAGE)
        if proc.returncode != 0:
            note(f"FAIL: start the TLS sidecar: {proc.stderr.strip()}")
            return 1
        proc = docker("network", "connect", back, tls)
        if proc.returncode != 0:
            note(f"FAIL: connect the TLS sidecar to the backend network: {proc.stderr.strip()}")
            return 1

        try:
            proc = subprocess.run(
                ["docker", "run", "--rm", "--name", drv, "--network", net, "--user", "10001:10001",
                 "--read-only", "--tmpfs", "/tmp", "--entrypoint", "python3",
                 "-e", "HOME=/tmp", "-e", "NODE_EXTRA_CA_CERTS=/secrets/local/tls/ca.crt",
                 "-e", f"VW_TEST_SIDECAR={tls}",
                 "-v", f"{src}:/app:ro", "-v", f"{Path(__file__).resolve()}:/test/test-vw-register.py:ro",
                 "-v", f"{secrets_dir / 'local/tls/ca.crt'}:/secrets/local/tls/ca.crt:ro",
                 "-v", f"{local / 'admin_token'}:/secrets/local/admin_token:ro",
                 "-v", f"{tmp / 'synthetic.json'}:/test/synthetic.json:ro",
                 image, "/test/test-vw-register.py", "--inside"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=DRIVER_TIMEOUT)
            driver_out, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            driver_out, code = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) \
                else (exc.stdout or ""), 1
            driver_out += f"\nFAIL: the driver ran past {DRIVER_TIMEOUT} s"
        output.append(driver_out)
        print(driver_out, end="" if driver_out.endswith("\n") else "\n", flush=True)
        if code != 0:
            failed = True
            for name in (vw, tls):
                state = docker("inspect", "-f", "{{.State.Status}} exit {{.State.ExitCode}}", name)
                note(f"  {name}: {state.stdout.strip()}")
            tail = docker("logs", "--tail", "8", vw)
            note("  Vaultwarden's last log lines:\n" + "\n".join(
                f"  | {ln}" for ln in (tail.stdout + tail.stderr).strip().splitlines()))

        # The sidecar's healthcheck command, as compose runs it.
        proc = docker("exec", tls, "sh", "-c",
                      "wget -q -O /dev/null --no-check-certificate https://127.0.0.1/alive || exit 1")
        if proc.returncode != 0:
            note("FAIL: the vaultwarden-tls healthcheck command fails")
            failed = True
        else:
            note("ok: the vaultwarden-tls healthcheck command passes")
        proc = docker("exec", vw, "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:80/alive")
        if proc.returncode != 0:
            note("FAIL: the vaultwarden healthcheck command fails")
            failed = True
        else:
            note("ok: the vaultwarden healthcheck command passes")
        logs = docker("logs", tls)
        output.append(logs.stdout + logs.stderr)
        logs = docker("logs", vw)
        output.append(logs.stdout + logs.stderr)
    finally:
        # One try per call, so a failing removal never skips the others.
        cleanup = [("rm", "-f", name) for name in (drv, tls, vw, gen)]
        cleanup += [("network", "rm", name) for name in (net, back)]
        if built:
            cleanup.append(("image", "rm", image))
        for args in cleanup:
            try:
                docker(*args, timeout=120)
            except (OSError, subprocess.SubprocessError):
                print(f"warning: docker {' '.join(args)} failed; remove it by that name")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass

    text = "\n".join(output)
    leaked = [k for k, v in {**synthetic, "admin_token": admin_token}.items() if v and v in text]
    if leaked:
        print(f"FAIL: output (driver, setup, sidecar and Vaultwarden logs) contains {sorted(leaked)}")
        failed = True
    # The driver withholds tokens, keys and sessions it knows; this catches any access token it did not.
    if re.search(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}", text):
        print("FAIL: output contains a JWT")
        failed = True
    if failed:
        print("FAIL")
        return 1
    print("ok")
    return 0


# Driver, inside the broker image on the scratch network

class Abort(Exception):
    pass


class Detail:
    """What a step prints after its name; anything else a step returns is never printed."""

    def __init__(self, text: str):
        self.text = text


def inside_main() -> int:
    import base64
    import pty
    import select
    import signal
    import ssl
    import time
    import urllib.error
    import urllib.parse
    import urllib.request
    import uuid

    sys.path[:0] = ["/app", "/app/setup"]
    import vw_register as vw
    from broker.accounts import parse_file
    from broker.bwcli import Bw, BwError, classify, run_bw

    syn = json.loads(Path("/test/synthetic.json").read_text(encoding="utf-8"))
    admin_token = Path("/secrets/local/admin_token").read_text(encoding="utf-8").strip()
    secret_values = {"agent password": syn["agent_pw"], "owner password": syn["owner_pw"],
                     "item password": syn["item_pw"], "admin token": admin_token}
    failures: list[str] = []
    st: dict = {}

    def say(line: str) -> None:
        hits = [k for k, v in secret_values.items() if v and v in line]
        if hits:
            failures.append(f"output would contain {hits}")
            line = f"FAIL: a line with {sorted(hits)} was withheld"
        print(line, flush=True)

    def secret(name: str, value: str) -> str:
        if value:
            secret_values[name] = value
        return value

    def step(name: str, fn, fatal: bool = True):
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - the test reports and goes on or stops
            failures.append(name)
            say(f"FAIL: {name}: {type(exc).__name__}: {exc}")
            if fatal:
                raise Abort from None
            return None
        # Only a step's own description is printed; values a step returns stay unprinted.
        note = detail.text if isinstance(detail, Detail) else ""
        say(f"ok: {name}" + (f" ({note})" if note else ""))
        return detail

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    def b64(obj) -> str:
        return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

    def bw(appdata: str, args: list[str], env: dict | None = None, stdin: str | None = None):
        return run_bw("bw", appdata, args, dict(env or {}), stdin)

    def bw_ok(appdata: str, args: list[str], env: dict | None = None, stdin: str | None = None) -> str:
        code, out, err = bw(appdata, args, env, stdin)
        if code != 0:
            raise AssertionError(f"bw {args[0]} {args[1] if len(args) > 1 else ''}: exit {code}, "
                                 f"class {classify(err)[0]}: {first_line(err)}")
        return out

    def first_line(stderr: str) -> str:
        lines = [ln.strip() for ln in stderr.splitlines()
                 if ln.strip() and not ln.lstrip().startswith(("(node:", "(Use `node"))]
        return lines[-1][:160] if lines else "(empty)"

    def item_json(name: str, org: str | None, collections: list[str]) -> dict:
        return {"type": 1, "name": name, "organizationId": org, "collectionIds": collections,
                "login": {"username": "synthetic", "password": syn["item_pw"],
                          "uris": [{"uri": f"https://{name}.example.com", "match": None}]},
                "notes": None, "favorite": False, "fields": [], "secureNote": None, "card": None,
                "identity": None, "reprompt": 0, "folderId": None}

    def run_setup(args: list[str], password: str, timeout: float = 180,
                  replies: tuple[tuple[bytes, str], ...] = ()) -> tuple[int, str]:
        """setup.py under a pty, answering the master password prompt once, then each
        (prompt, reply) in ``replies`` in order."""
        env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "NODE_EXTRA_CA_CERTS": os.environ["NODE_EXTRA_CA_CERTS"],
               "BITWARDEN_ACCOUNTS_DIR": "/tmp/accounts", "PYTHONDONTWRITEBYTECODE": "1"}
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execve(sys.executable, [sys.executable, "/app/setup/setup.py", *args], env)
            finally:
                os._exit(127)
        buf, answered, pending, seen = b"", False, list(replies), 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                if not answered and b"Master password" in buf:
                    os.write(fd, (password + "\n").encode("utf-8"))
                    answered, seen = True, len(buf)
                elif answered and pending and pending[0][0] in buf[seen:]:
                    os.write(fd, (pending[0][1] + "\n").encode("utf-8"))
                    pending.pop(0)
                    seen = len(buf)
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done:
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), buf.decode("utf-8", "replace").replace("\r\n", "\n")

    def org_members(tok: str) -> list[dict]:
        data = vw.api(BASE, tok, "GET", f"/organizations/{st['org']}/users?includeCollections=true")
        return [m for m in (data or {}).get("data") or [] if isinstance(m, dict)]

    def member(tok: str, email: str) -> dict:
        found = next((m for m in org_members(tok) if str(m.get("email", "")).lower() == email), None)
        expect(found is not None, f"{email} is not a member")
        return found

    # --- TLS sidecar ---------------------------------------------------------

    def wait_alive():
        deadline = time.monotonic() + 120
        while True:
            try:
                with vw.opener().open(f"{BASE}/alive", timeout=5) as resp:
                    expect(resp.status == 200, f"/alive returned {resp.status}")
                    return Detail("Vaultwarden answers through the sidecar")
            except (urllib.error.URLError, OSError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def untrusted_without_ca():
        try:
            urllib.request.urlopen(f"{BASE}/alive", timeout=10, context=ssl.create_default_context())
        except urllib.error.URLError as exc:
            expect(isinstance(exc.reason, ssl.SSLCertVerificationError), f"failed for another reason: {exc.reason}")
            return
        raise AssertionError("the certificate verified without the generated CA")

    try:
        step("the TLS sidecar serves Vaultwarden with the generated certificate", wait_alive)
        step("the certificate verifies only through NODE_EXTRA_CA_CERTS", untrusted_without_ca)

        def no_plain_http():
            try:
                urllib.request.urlopen("http://vaultwarden:80/alive", timeout=10)
            except (urllib.error.URLError, OSError) as exc:
                return Detail(f"refused: {type(getattr(exc, 'reason', exc)).__name__}")
            raise AssertionError("Vaultwarden answered plain HTTP on the vault network")

        step("Ruling R20: vaultwarden:80 is not reachable from the vault network", no_plain_http)

        # --- Sign-ups off, admin invite, registration ------------------------

        def stranger():
            try:
                vw.register(BASE, STRANGER_EMAIL, "x" * 16)
            except vw.VwError as exc:
                return Detail(f"{exc}")
            raise AssertionError("registration succeeded")

        step("uninvited registration is refused (SIGNUPS_ALLOWED=false)", stranger)

        def admin_wrong():
            try:
                vw.admin_invite(BASE, "not-the-admin-token", AGENT_EMAIL)
            except vw.VwError as exc:
                return Detail(f"{exc}")
            raise AssertionError("a wrong admin token was accepted")

        step("admin login with a wrong token is refused", admin_wrong)
        step("admin invite of the agent account", lambda: vw.admin_invite(BASE, admin_token, AGENT_EMAIL))
        st["agent_keys"] = step("client-side registration of the agent account",
                                lambda: vw.register(BASE, AGENT_EMAIL, syn["agent_pw"]))
        def agent_token():
            st["agent_tok"] = secret("agent token", vw.token(BASE, AGENT_EMAIL, syn["agent_pw"]))

        step("access token for the agent account", agent_token)

        def get_api_key():
            client_id, client_secret = vw.api_key(BASE, st["agent_tok"], AGENT_EMAIL, syn["agent_pw"])
            secret("agent API key", client_secret)
            expect(client_id.startswith("user."), "client id is not user.<id>")
            st["client_id"] = client_id
            st["client_secret"] = client_secret

        step("personal API key of the agent account", get_api_key)

        # --- Organization ----------------------------------------------------

        def org():
            st["org"], st["agent_coll"] = vw.create_org(BASE, st["agent_tok"], st["agent_keys"], "Assistant", "Agent")

        step("the agent creates the organization Assistant with the collection Agent", org)

        Path("/tmp/accounts").mkdir(mode=0o700)
        Path("/tmp/secrets/local").mkdir(parents=True, mode=0o700)
        account_file = Path("/tmp/accounts/local.env")
        account_file.write_text(f"BW_MODE=local\nBW_SERVER={BASE}\nBW_SCOPE=collection:{st['agent_coll']}\n"
                                f"BW_UNLOCK=stored\nBW_CLIENTID={st['client_id']}\n", encoding="utf-8")
        os.chmod(account_file, 0o600)
        for name, value in (("client_secret", st["client_secret"]), ("master_password", syn["agent_pw"])):
            Path(f"/tmp/secrets/local/{name}").write_text(value + "\n", encoding="utf-8")
        agent = Bw(parse_file(account_file), Path("/tmp/bw"), Path("/tmp/secrets"))
        agent_dir = str(agent.data_dir)

        def agent_unlock():
            agent.unlock()
            expect(agent.state == "unlocked", f"state {agent.state}")
            secret("agent session", agent._session or "")

        step("broker Bw: bw login --apikey, then bw unlock --passwordenv with the stored password", agent_unlock)

        def agent_env() -> dict:
            return {"BW_SESSION": agent._session}

        # --- The owner joins -------------------------------------------------

        step("organization invite of the owner as Owner",
             lambda: vw.invite_member(BASE, st["agent_tok"], st["org"], OWNER_EMAIL, 0, []))
        step("the owner registers off the organization invite (no SMTP)",
             lambda: vw.register(BASE, OWNER_EMAIL, syn["owner_pw"]))

        def accepted():
            m = member(st["agent_tok"], OWNER_EMAIL)
            st["owner_member"] = m["id"]
            st["agent_member"] = member(st["agent_tok"], AGENT_EMAIL)["id"]
            expect(m.get("status") == 1, f"status {m.get('status')}, want 1 (Accepted)")
            expect(m.get("type") == 0, f"type {m.get('type')}, want 0 (Owner)")
            return Detail("Accepted, Owner")

        step("the owner's membership after registering", accepted)

        def confirm():
            bw_ok(agent_dir, ["sync"], agent_env())
            bw_ok(agent_dir, ["confirm", "org-member", st["owner_member"], "--organizationid", st["org"]],
                  agent_env())
            expect(member(st["agent_tok"], OWNER_EMAIL).get("status") == 2, "not Confirmed")

        step("bw confirm org-member: the agent confirms the owner", confirm)

        # --- The owner demotes the agent with setup.py role ------------------

        def role():
            code, out = run_setup(["role", "--slug", "local", "--email", OWNER_EMAIL], syn["owner_pw"])
            say("  | " + "\n  | ".join(ln for ln in out.strip().splitlines() if "Master password" not in ln))
            expect(code == 0, f"setup.py role exit {code}")
            st["owner_tok"] = secret("owner token", vw.token(BASE, OWNER_EMAIL, syn["owner_pw"]))
            m = member(st["owner_tok"], AGENT_EMAIL)
            got = sorted((c.get("id"), c.get("readOnly"), c.get("manage")) for c in m.get("collections") or [])
            want = [(st["agent_coll"], False, True)]
            expect(m.get("type") == 2, f"agent type {m.get('type')}, want 2 (User)")
            expect(got == want, f"agent collections {got}, want {want}")

        step("setup.py role: the owner makes the agent a User with Manage on Agent only", role)

        def agent_cannot_edit_owner():
            try:
                vw.api(BASE, st["agent_tok"], "PUT", f"/organizations/{st['org']}/users/{st['owner_member']}",
                       {"type": 2, "groups": [], "permissions": {}, "collections": []})
            except vw.VwError as exc:
                return Detail(str(exc))
            raise AssertionError("the demoted agent changed the owner's role")

        step("the demoted agent is refused when it edits the owner", agent_cannot_edit_owner)

        # --- Items -----------------------------------------------------------

        owner_dir = "/tmp/bw-owner"
        owner_env: dict = {}

        def owner_login():
            bw_ok(owner_dir, ["config", "server", BASE])
            session = bw_ok(owner_dir, ["login", OWNER_EMAIL, "--passwordenv", "BW_PW", "--raw"],
                            {"BW_PW": syn["owner_pw"]}).strip()
            owner_env["BW_SESSION"] = secret("owner session", session)

        step("bw login as the owner", owner_login)

        def owner_items():
            out = bw_ok(owner_dir, ["create", "org-collection", "--organizationid", st["org"]], owner_env,
                        stdin=b64({"organizationId": st["org"], "name": "Owner only", "externalId": None,
                                   "groups": [], "users": []}))
            st["other_coll"] = json.loads(out)["id"]
            out = bw_ok(owner_dir, ["create", "item"], owner_env,
                        stdin=b64(item_json("linkedin", st["org"], [st["agent_coll"]])))
            st["agent_item"] = json.loads(out)["id"]
            bw_ok(owner_dir, ["create", "item"], owner_env,
                  stdin=b64(item_json("owner-only", st["org"], [st["other_coll"]])))
            out = bw_ok(owner_dir, ["create", "item"], owner_env, stdin=b64(item_json("personal", None, [])))
            st["personal_item"] = json.loads(out)["id"]

        step('the owner creates "linkedin" in Agent, "owner-only" in Owner only and a personal item', owner_items)

        def agent_lists():
            agent.sync()
            names = sorted(i["name"] for i in agent.list_items())
            raw = sorted(i["name"] for i in json.loads(bw_ok(agent_dir, ["list", "items"], agent_env())))
            expect(names == ["linkedin"], f"broker list {names}")
            expect(raw == ["linkedin"], f"bw list items {raw}")
            return Detail("broker and bw list only linkedin")

        step("the agent lists only the Agent item", agent_lists)

        # --- Check 1: release by empty item-collections ----------------------

        def release():
            # What the user does in a Bitwarden app after a release: take the item out of the collection.
            bw_ok(owner_dir, ["edit", "item-collections", st["agent_item"], "--organizationid", st["org"]],
                  owner_env, stdin=b64([]))
            bw_ok(owner_dir, ["sync"], owner_env)
            item = json.loads(bw_ok(owner_dir, ["get", "item", st["agent_item"]], owner_env))
            expect(item.get("collectionIds") == [], f"collectionIds {item.get('collectionIds')}")
            expect(item.get("organizationId") == st["org"], "the item left the organization")
            agent.sync()
            expect(agent.list_items() == [], "the agent still lists the item")
            return Detail("the item is in the organization with no collection and the agent's sync drops it")

        step("check 1: the Owner's bw edit item-collections with [] on stdin removes the item from Agent",
             release, fatal=False)

        # --- Check 2: the owner sees the unassigned item ---------------------

        def owner_sees():
            rows = json.loads(bw_ok(owner_dir, ["list", "items", "--organizationid", st["org"]], owner_env))
            hit = next((i for i in rows if i.get("id") == st["agent_item"]), None)
            expect(hit is not None, "not listed")
            expect(hit.get("collectionIds") == [], f"collectionIds {hit.get('collectionIds')}")
            code, out = run_setup(["list-unassigned", "--slug", "local", "--email", OWNER_EMAIL], syn["owner_pw"])
            expect(code == 0 and "linkedin  linkedin.example.com" in out, f"setup.py list-unassigned exit {code}")
            return Detail("bw list items --organizationid and setup.py list-unassigned both show it")

        step("check 2: the Owner lists the unassigned item", owner_sees, fatal=False)

        # --- Seed: setup.py seed moves the items the user picks into Agent ---

        def seed(site: str, name: str):
            code, out = run_setup(["seed", "--slug", "local", "--email", OWNER_EMAIL], syn["owner_pw"],
                                  replies=((b"Site to give", site), (b"Move which", "1"), (b"Site to give", "")))
            expect(code == 0 and f'Moved "{name}"' in out, f"setup.py seed exit {code}")
            agent.sync()
            names = sorted(i["name"] for i in agent.list_items())
            expect(name in names, f"agent lists {names}")

        step("setup.py seed: bw move of a personal item into Agent; the agent sees it",
             lambda: seed("personal.example.com", "personal"), fatal=False)
        step("setup.py seed: the unassigned organization item back into Agent; the agent sees it",
             lambda: seed("linkedin.example.com", "linkedin"), fatal=False)

        # --- WebSocket upgrade through the sidecar ---------------------------

        def websocket():
            import asyncio
            import websockets

            async def go():
                ctx = vw.ssl_context()
                async with websockets.connect(f"wss://{LOCAL_NAME}/notifications/hub?access_token={st['owner_tok']}",
                                              ssl=ctx, open_timeout=15):
                    return True

            expect(asyncio.run(go()), "no connection")
            return Detail("101 Switching Protocols")

        step("the sidecar upgrades /notifications/hub to a WebSocket", websocket, fatal=False)

        # --- Check 3: bw's real stderr through classify ----------------------

        def classified(label: str, want: str, args: list[str], appdata: str, env: dict | None = None,
                       stdin: str | None = None, unlock: bool = False):
            def run():
                code, _, err = bw(appdata, args, env, stdin)
                got = classify(err, unlock=unlock)[0]
                if unlock:
                    # The same text from any other command is not an auth failure.
                    expect(classify(err)[0] == "unknown", f"{first_line(err)!r} outside unlock is not unknown")
                st.setdefault("check3", []).append((label, got, first_line(err)))
                expect(code != 0, "bw succeeded")
                expect(got == want, f"classify gave {got!r} for {first_line(err)!r}, want {want!r}")
                return Detail(f"{got}: {first_line(err)}")
            step(f"check 3: {label} classifies as {want}", run, fatal=False)

        classified("wrong master password on unlock", "auth", ["unlock", "--passwordenv", "BW_PW", "--raw"],
                   agent_dir, {"BW_PW": "wrong-" + syn["agent_pw"]}, unlock=True)
        fresh = "/tmp/bw-fresh"
        bw_ok(fresh, ["config", "server", BASE])
        classified("wrong master password on login", "auth",
                   ["login", OWNER_EMAIL, "--passwordenv", "BW_PW", "--raw"], fresh, {"BW_PW": "wrong-password"})
        classified("wrong API key on login --apikey", "auth", ["login", "--apikey"], fresh,
                   {"BW_CLIENTID": st["client_id"], "BW_CLIENTSECRET": "wrong-client-secret"})
        classified("item not found", "unknown", ["get", "item", str(uuid.uuid4())], owner_dir, owner_env)
        classified("no session (locked)", "locked", ["list", "items"], owner_dir, {})

        def deauthorize():
            iterations = 600000
            vw.api(BASE, st["agent_tok"], "POST", "/accounts/security-stamp",
                   {"masterPasswordHash": vw.master_password_hash(AGENT_EMAIL, syn["agent_pw"], iterations)})

        step("the agent's sessions are deauthorized (security stamp)", deauthorize, fatal=False)
        classified("401 after deauthorization (bw sync)", "auth", ["sync"], agent_dir, agent_env())

        def broker_after_401():
            try:
                agent.sync()
            except BwError as exc:
                expect(exc.error_class == "auth", f"Bw.sync raised {exc.error_class}")
                return Detail(f"Bw.sync raised auth; state {agent.state}")
            raise AssertionError("Bw.sync succeeded with a deauthorized session")

        step("check 3: the broker's Bw reports auth after the 401", broker_after_401, fatal=False)

        def no_ca():
            saved = os.environ.pop("NODE_EXTRA_CA_CERTS")
            try:
                code, _, err = bw("/tmp/bw-noca", ["config", "server", BASE])
                code, _, err = bw("/tmp/bw-noca", ["login", "--apikey"],
                                  {"BW_CLIENTID": st["client_id"], "BW_CLIENTSECRET": st["client_secret"]})
            finally:
                os.environ["NODE_EXTRA_CA_CERTS"] = saved
            got = classify(err)[0]
            expect(code != 0 and got == "network", f"exit {code}, class {got}: {first_line(err)}")
            return Detail(f"network: {first_line(err)}")

        step("check 3: bw without the CA classifies as network", no_ca, fatal=False)

        # --- Check 4: 429 and Retry-After ------------------------------------

        def grant_status(spoof: str | None = None) -> tuple[int, str | None]:
            """One password grant for a made-up account; ``(status, Retry-After)``."""
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if spoof:
                headers.update({"X-Real-IP": spoof, "X-Forwarded-For": spoof})
            req = urllib.request.Request(
                f"{BASE}/identity/connect/token", method="POST", headers=headers,
                data=urllib.parse.urlencode({
                    "grant_type": "password", "username": "nobody@example.com", "password": "eA==",
                    "scope": "api offline_access", "client_id": "cli", "deviceType": "25",
                    "deviceName": "linux", "deviceIdentifier": str(uuid.uuid4())}).encode())
            try:
                vw.opener().open(req, timeout=10).close()
            except urllib.error.HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After")
                exc.close()
                return status, retry_after
            return 200, None

        def rate_limit():
            # Vaultwarden limits password grants per client address; the sidecar passes the driver's in
            # X-Real-IP. Spend the allowance with a made-up account, then sign in with bw at once.
            # The limiter gives one grant back every LOGIN_RATELIMIT_SECONDS (60 by default), so bw's
            # login can land on a fresh one and succeed. That run says nothing about a 429: try again.
            for attempt in range(1, 4):
                appdata = f"/tmp/bw-429-{attempt}"
                bw_ok(appdata, ["config", "server", BASE])  # before spending, to keep the gap short
                seen, retry_after = 0, None
                for _ in range(LOGIN_BURST + 20):
                    seen, retry_after = grant_status()
                    if seen == 429:
                        break
                expect(seen == 429, f"no 429 after {LOGIN_BURST + 20} password grants")
                code, _, err = bw(appdata, ["login", OWNER_EMAIL, "--passwordenv", "BW_PW", "--raw"],
                                  {"BW_PW": syn["owner_pw"]})
                if code != 0:
                    break
                say(f"check 4: bw signed in on attempt {attempt}; the limiter gave a grant back, trying again")
            expect(code != 0, "bw signed in on all 3 attempts; the limiter never refused it")
            got, wait = classify(err)
            has_retry = "retry-after" in err.lower()
            expect(got == "rate_limited", f"classify gave {got!r} for {first_line(err)!r} (bw exit {code})")
            return Detail(f"server Retry-After header: {retry_after!r}; bw exit {code}; bw prints Retry-After: "
                          f"{'yes' if has_retry else 'no'}; classify: {got}, wait {wait}; "
                          f"bw said: {first_line(err)}")

        step("check 4: bw on a 429", rate_limit, fatal=False)

        def spoofed():
            status, _ = grant_status(spoof="203.0.113.9")
            expect(status == 429, f"a spoofed X-Real-IP and X-Forwarded-For got HTTP {status}, want 429")
            return Detail("still 429; the sidecar overwrites both headers")

        step("Ruling R20: a client-set X-Real-IP does not escape the login limit", spoofed, fatal=False)

    except Abort:
        say("stopped: a required step failed")

    if failures:
        say(f"FAIL: {len(failures)} step(s): {failures}")
        return 1
    say("ok: driver")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside"]:
        sys.exit(inside_main())
    sys.exit(host_main())
