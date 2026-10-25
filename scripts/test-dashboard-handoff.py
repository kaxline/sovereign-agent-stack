#!/usr/bin/env python3
"""Checks for dashboard sign-in links and board links (patch-dashboard-handoff.py,
patch-dashboard-board.py).

Host checks: mint_code / check_code (good, reused, expired, bad signature,
too-long expiry, placeholder secret).

Image checks (skip without the pinned image unless --image): both patches apply
twice (idempotent) and the patched kanban bundle still parses. Then a throwaway
``hermes`` container with generated test credentials and the dashboard on, as
compose runs it:

- a fresh code lands on its ``next`` signed in, with no-store and no-referrer;
- a reused or expired code lands on /login?next=..., a forged one on /login;
- an unsafe ``next`` lands on /;
- password sign-in still works;
- the handoff session survives ``docker restart``;
- index.html carries the embed script and the kanban bundle the task links;
- with no secret, /auth/handoff answers 404.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(HERMES_DIR))

import dashboard_handoff as dh  # noqa: E402
import hermes_image  # noqa: E402


def host_checks() -> None:
    secret = secrets.token_hex(32)
    code = dh.mint_code(secret, "/kanban?task=t_1")
    assert dh.check_code(code, secret) == ("ok", "/kanban?task=t_1")
    assert dh.check_code(code, secret) == ("used", "/kanban?task=t_1")
    assert dh.check_code(code, secrets.token_hex(32)) == ("invalid", "")
    assert dh.check_code(code[:-2] + "AA", secret) == ("invalid", "")
    assert dh.check_code("", secret) == ("invalid", "")
    old = dh.mint_code(secret, "/x", now=int(time.time()) - 120)
    assert dh.check_code(old, secret) == ("expired", "/x")
    # A code minted for longer than the limit is refused, even with a good signature.
    now = int(time.time())
    far = dh.mint_code(secret, "/x", now=now + 3600)
    assert dh.check_code(far, secret, now=now) == ("invalid", "")
    assert dh.mint_code(secret, "/x", ttl=600).count(".") == 1  # ttl is capped, not refused
    for bad in ("", "short", "change-me-generate-with-openssl-rand-hex-32"):
        assert not dh.usable_secret(bad)
        try:
            dh.mint_code(bad, "/")
        except ValueError:
            pass
        else:
            raise AssertionError(f"mint_code accepted {bad!r}")
    print("host ok")


IMAGE_DRIVER = r'''
import subprocess, sys
def run(name):
    out = subprocess.run([sys.executable, f"/bootstrap/{name}"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {name}: {out.stdout}{out.stderr}")
    return out.stdout
first = run("patch-dashboard-board.py")
assert "patched kanban task links" in first and "patched dashboard embed" in first, first
assert run("patch-dashboard-board.py").count("already applied") == 2
first = run("patch-dashboard-handoff.py")
assert "patched handoff route" in first and "patched public handoff path" in first, first
assert run("patch-dashboard-handoff.py").count("already applied") == 2
node = subprocess.run(["node", "--check", "/opt/hermes/plugins/kanban/dashboard/dist/index.js"],
                      capture_output=True, text=True)
assert node.returncode == 0, node.stderr
sys.path.insert(0, "/opt/hermes")
from hermes_cli.dashboard_auth import middleware, routes
assert middleware._path_is_public("/auth/handoff")
assert any(getattr(r, "path", "") == "/auth/handoff" for r in routes.router.routes)
print("image ok")
'''


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Dashboard:
    """A throwaway hermes container serving the dashboard on a random local port."""

    def __init__(self, tag: str, env: dict[str, str]) -> None:
        self.name = f"assistant-test-dashboard-{os.getpid()}-{secrets.token_hex(3)}"
        mounts = []
        for name in ("patch-dashboard-board.py", "dashboard_handoff.py", "patch-dashboard-handoff.py"):
            mounts += ["-v", f"{HERMES_DIR / name}:/bootstrap/{name}:ro"]
        mounts += ["-v", f"{HERMES_DIR / '13-patch-dashboard.sh'}:/etc/cont-init.d/13-patch-dashboard:ro"]
        env_args = []
        for key, value in {"HERMES_DASHBOARD": "1", "API_SERVER_ENABLED": "false", **env}.items():
            env_args += ["-e", f"{key}={value}"]
        subprocess.run(
            ["docker", "run", "-d", "--name", self.name, "-p", "127.0.0.1::9119", *env_args, *mounts,
             f"nousresearch/hermes-agent:{tag}", "gateway", "run"],
            check=True, capture_output=True)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.wait()

    def wait(self, timeout: float = 180) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            port = subprocess.run(["docker", "port", self.name, "9119/tcp"],
                                  capture_output=True, text=True).stdout.strip()
            if port:
                self.base = "http://localhost:" + port.splitlines()[0].rsplit(":", 1)[1]
                try:
                    with urllib.request.urlopen(f"{self.base}/api/status", timeout=3) as resp:
                        if resp.status == 200:
                            return
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(2)
        logs = subprocess.run(["docker", "logs", "--tail", "60", self.name],
                              capture_output=True, text=True)
        raise SystemExit(f"FAIL dashboard never came up:\n{logs.stdout}{logs.stderr}")

    def get(self, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers or {})
        try:
            resp = self.opener.open(req, timeout=15)
        except urllib.error.HTTPError as err:
            resp = err
        body = resp.read()
        return resp.status, resp.headers, body

    def restart(self) -> None:
        subprocess.run(["docker", "restart", self.name], check=True, capture_output=True)
        self.wait()

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def live_checks(tag: str) -> None:
    secret = secrets.token_hex(32)
    password = secrets.token_hex(16)
    env = {
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "admin",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": password,
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET": secrets.token_hex(32),
        "HERMES_DASHBOARD_HANDOFF_SECRET": secret,
    }
    dash = Dashboard(tag, env)
    try:
        def handoff(code: str):
            return dash.get(f"/auth/handoff?code={quote(code, safe='')}")

        status, _h, _b = dash.get("/api/auth/me")
        assert status == 401, status

        code = dh.mint_code(secret, "/kanban?task=t_1&embed=1")
        status, headers, _b = handoff(code)
        assert status == 302 and headers["Location"] == "/kanban?task=t_1&embed=1", (status, dict(headers))
        assert headers["Referrer-Policy"] == "no-referrer" and "no-store" in headers["Cache-Control"]
        status, _h, body = dash.get("/api/auth/me")
        assert status == 200 and json.loads(body).get("provider") == "basic", (status, body)

        status, headers, _b = handoff(code)
        assert status == 302 and headers["Location"] == "/login?next=%2Fkanban%3Ftask%3Dt_1%26embed%3D1", \
            headers["Location"]
        expired = dh.mint_code(secret, "/kanban", now=int(time.time()) - 120)
        assert handoff(expired)[1]["Location"] == "/login?next=%2Fkanban"
        forged = dh.mint_code(secrets.token_hex(32), "/kanban")
        assert handoff(forged)[1]["Location"] == "/login"
        assert handoff(dh.mint_code(secret, "//evil.example.com/x"))[1]["Location"] == "/"
        assert handoff(dh.mint_code(secret, "/api/auth/me"))[1]["Location"] == "/"

        status, _h, body = dash.get(
            "/auth/password-login",
            data=json.dumps({"provider": "basic", "username": "admin", "password": password,
                             "next": "/kanban"}).encode(),
            headers={"Content-Type": "application/json"})
        assert status == 200 and json.loads(body) == {"ok": True, "next": "/kanban"}, (status, body)

        # Only the handoff session from here on.
        dash.jar.clear()
        assert handoff(dh.mint_code(secret, "/"))[0] == 302
        dash.restart()
        status, _h, _b = dash.get("/api/auth/me")
        assert status == 200, f"handoff session lost on restart: {status}"

        status, _h, body = dash.get("/kanban?embed=1")
        assert status == 200 and b"assistant-stack: board embed" in body, status
        status, _h, body = dash.get("/dashboard-plugins/kanban/dist/index.js")
        assert status == 200 and b"assistant-stack: board links" in body, status
    finally:
        dash.remove()

    env["HERMES_DASHBOARD_HANDOFF_SECRET"] = ""
    dash = Dashboard(tag, env)
    try:
        status, _h, _b = dash.get(f"/auth/handoff?code={quote(dh.mint_code(secret, '/'), safe='')}")
        assert status == 404, status
    finally:
        dash.remove()
    print("live ok")


def main() -> None:
    host_checks()
    hermes_image.maybe_run(IMAGE_DRIVER)
    args = sys.argv[1:]
    tag = hermes_image.image_tag()
    if "--host-only" in args:
        return
    if "--image" in args or hermes_image.image_available(tag):
        live_checks(tag)


if __name__ == "__main__":
    main()
