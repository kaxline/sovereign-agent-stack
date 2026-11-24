#!/usr/bin/env python3
"""scripts/lib/vikunja_tokens.py against a stub Vikunja API.

Checks the bucket permission keys it resolves from /api/v1/routes on each
supported route shape (2.7.0, 2.7.1, 2.8.0), the read/write and read-only
permission sets, replace-by-title, the refusals, and that the password and
tokens' values never reach stderr. One run goes through stack_python in host
mode, called the way todo.sh calls it. No Docker, no network beyond 127.0.0.1.

The 2.7.0 bucket keys are the ones todo.sh hard-coded before a8244c0. Of the
later names only views_buckets_tasks is pinned (CHANGELOG.md, Vikunja 2.8.0:
it became the GET that lists bucket tasks); the other 2.7.1/2.8.0 names are
stand-ins, since the script finds keys by method and path, not by name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lib" / "vikunja_tokens.py"
PASSWORD = "pw-SECRET-vikunja"
JWT = "jwt-SECRET"
TITLE_RW = "assistant-hermes-rw"
TITLE_RO = "assistant-hermes-ro"

P = "/api/v1/projects/:project"
V = P + "/views/:view"

# Project routes every shape shares, none of which a bucket lookup may pick up.
COMMON = {
    "read_all": ("GET", "/api/v1/projects"),
    "read_one": ("GET", P),
    "update": ("POST", P),
    "views_read_all": ("GET", P + "/views"),
}
# Six bucket routes: list buckets, list bucket tasks, create, update, delete,
# move a task. Keys per shape, in that order.
BUCKET_PATHS = [
    ("GET", V + "/buckets"),
    ("GET", V + "/buckets/tasks"),
    ("PUT", V + "/buckets"),
    ("POST", V + "/buckets/:bucket"),
    ("DELETE", V + "/buckets/:bucket"),
    ("POST", V + "/buckets/:bucket/tasks"),
]
SHAPES = {
    "2.7.0": ["views_buckets", "views_buckets_tasks_get", "views_buckets_put",
              "views_buckets_post", "views_buckets_delete", "views_buckets_tasks"],
    "2.7.1": ["views_buckets_read_all", "views_buckets_tasks_read_all", "views_buckets_create",
              "views_buckets_update", "views_buckets_delete", "views_buckets_tasks_move"],
    "2.8.0": ["views_buckets_read_all", "views_buckets_tasks", "views_buckets_create",
              "views_buckets_update", "views_buckets_delete", "views_buckets_tasks_move"],
}
TYPED = {
    "tasks": ["read_all", "read_one", "create", "update", "delete"],
    "projects": ["read_all", "read_one", "create", "update"],
    "labels": ["read_all", "create"],
    "tasks_assignees": ["create", "delete"],
}


def routes_for(version: str) -> dict:
    projects = {k: {"method": m, "path": p} for k, (m, p) in COMMON.items()}
    for key, (method, path) in zip(SHAPES[version], BUCKET_PATHS):
        projects[key] = {"method": method, "path": path}
    return {"projects": projects, "tasks": {"read_all": {"method": "GET", "path": "/api/v1/tasks/all"}}}


class Stub:
    def __init__(self, version: str = "2.8.0") -> None:
        self.routes = routes_for(version)
        self.presets: dict | None = {"typed": TYPED}
        self.tokens: list[dict] = []
        self.next_id = 1
        self.calls: list[str] = []
        self.created: list[dict] = []
        self.login_status = 200
        self.logins: list[dict] = []

    def add_token(self, title: str) -> None:
        self.tokens.append({"id": self.next_id, "title": title})
        self.next_id += 1

    def handle(self, method: str, path: str, auth: str | None, body):
        self.calls.append(f"{method} {path}")
        if method == "POST" and path == "/api/v1/login":
            self.logins.append(body)
            if self.login_status != 200:
                return self.login_status, {"message": "Wrong username or password."}
            return 200, {"token": JWT}
        if auth != f"Bearer {JWT}":
            return 401, {"message": "missing, malformed, expired or otherwise invalid token provided"}
        if method == "GET" and path == "/api/v2/mcp/info":
            return 200, {"presets": self.presets} if self.presets is not None else {}
        if method == "GET" and path == "/api/v1/routes":
            return 200, self.routes
        if method == "GET" and path == "/api/v1/tokens":
            return 200, [dict(t) for t in self.tokens]
        m = re.fullmatch(r"/api/v1/tokens/(\d+)", path)
        if method == "DELETE" and m:
            self.tokens = [t for t in self.tokens if t["id"] != int(m.group(1))]
            return 200, {"message": "Successfully deleted."}
        if method == "PUT" and path == "/api/v1/tokens":
            tok = {"id": self.next_id, "title": body["title"], "token": f"tk_SECRET{self.next_id}"}
            self.next_id += 1
            self.created.append(body)
            self.tokens.append({"id": tok["id"], "title": tok["title"]})
            return 201, tok
        return 404, {"message": "Not Found"}


def serve(stub: Stub) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _any(self):
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            status, out = stub.handle(self.command, self.path, self.headers.get("authorization"), body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _any  # noqa: N815

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class VikunjaTokens(unittest.TestCase):
    def start(self, version: str = "2.8.0") -> Stub:
        stub = Stub(version)
        server = serve(stub)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        return stub

    def env(self) -> dict:
        return {
            "PATH": os.environ["PATH"],
            "VK_BASE": self.base,
            "VK_USER": "keith",
            "VK_PASS": PASSWORD,
            "VK_TITLE_RW": TITLE_RW,
            "VK_TITLE_RO": TITLE_RO,
        }

    def run_script(self) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], env=self.env(),
                              capture_output=True, text=True, timeout=60)
        self.assertNotIn(PASSWORD, proc.stdout + proc.stderr)
        self.assertNotIn("SECRET", proc.stderr)
        return proc

    def mint(self, stub: Stub) -> tuple[dict, dict, list[str]]:
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        by_title = {c["title"]: c for c in stub.created}
        return by_title[TITLE_RW], by_title[TITLE_RO], proc.stdout.splitlines()

    def test_route_shapes(self):
        for version, keys in SHAPES.items():
            with self.subTest(version=version):
                stub = self.start(version)
                rw, ro, _ = self.mint(stub)
                reads, writes = keys[:2], keys[2:]
                self.assertEqual(rw["permissions"]["projects"],
                                 TYPED["projects"] + reads + writes)
                self.assertEqual(ro["permissions"]["projects"], ["read_all", "read_one"] + reads)

    def test_views_buckets_tasks_changed_meaning(self):
        # 2.7.0: the POST that moves a task, so write only. 2.8.0: the GET
        # that lists bucket tasks, so on both tokens.
        stub = self.start("2.7.0")
        rw, ro, _ = self.mint(stub)
        self.assertIn("views_buckets_tasks", rw["permissions"]["projects"])
        self.assertNotIn("views_buckets_tasks", ro["permissions"]["projects"])
        stub = self.start("2.8.0")
        rw, ro, _ = self.mint(stub)
        self.assertIn("views_buckets_tasks", rw["permissions"]["projects"])
        self.assertIn("views_buckets_tasks", ro["permissions"]["projects"])
        self.assertNotIn("views_buckets_tasks_move", ro["permissions"]["projects"])

    def test_permission_sets(self):
        stub = self.start()
        rw, ro, _ = self.mint(stub)
        self.assertEqual(rw["expires_at"], "2099-12-31T00:00:00Z")
        self.assertEqual(ro["expires_at"], "2099-12-31T00:00:00Z")
        rwp, rop = rw["permissions"], ro["permissions"]
        self.assertEqual(sorted(rwp), ["labels", "mcp", "projects", "projects_views",
                                       "projects_views_tasks", "tasks", "tasks_assignees"])
        self.assertEqual(rwp["tasks"], TYPED["tasks"])
        self.assertEqual(rwp["tasks_assignees"], ["create", "delete"])
        # A group with no read actions is left off the read-only token.
        self.assertEqual(sorted(rop), ["labels", "mcp", "projects", "projects_views",
                                       "projects_views_tasks", "tasks"])
        self.assertEqual(rop["tasks"], ["read_all", "read_one"])
        self.assertEqual(rop["labels"], ["read_all"])
        for perms in (rwp, rop):
            self.assertEqual(perms["mcp"], ["access"])
            self.assertEqual(perms["projects_views"], ["read_all", "read_one"])
            self.assertEqual(perms["projects_views_tasks"], ["read_all"])

    def test_login_and_output(self):
        stub = self.start()
        _, _, lines = self.mint(stub)
        self.assertEqual(stub.logins, [{"username": "keith", "password": PASSWORD, "long_token": False}])
        self.assertEqual(stub.calls, [
            "POST /api/v1/login", "GET /api/v2/mcp/info", "GET /api/v1/routes",
            "GET /api/v1/tokens", "PUT /api/v1/tokens", "PUT /api/v1/tokens",
        ])
        self.assertEqual(lines, [
            "VIKUNJA_MCP_TOKEN_RW=tk_SECRET1",
            f"LOG Created token {TITLE_RW} (labels, mcp, projects, projects_views, "
            "projects_views_tasks, tasks, tasks_assignees)",
            "VIKUNJA_MCP_TOKEN_RO=tk_SECRET2",
            f"LOG Created token {TITLE_RO} (labels, mcp, projects, projects_views, "
            "projects_views_tasks, tasks)",
        ])

    def test_replace_by_title(self):
        stub = self.start()
        stub.add_token(TITLE_RW)
        stub.add_token("someone-elses-token")
        stub.add_token(TITLE_RO)
        _, _, lines = self.mint(stub)
        self.assertIn("DELETE /api/v1/tokens/1", stub.calls)
        self.assertIn("DELETE /api/v1/tokens/3", stub.calls)
        self.assertNotIn("DELETE /api/v1/tokens/2", stub.calls)
        self.assertEqual(lines[:2], [f"LOG Replaced token {TITLE_RW}", f"LOG Replaced token {TITLE_RO}"])
        self.assertEqual(sorted(t["title"] for t in stub.tokens),
                         sorted([TITLE_RW, TITLE_RO, "someone-elses-token"]))
        # Deletes come before the new tokens, so the fresh ones survive.
        self.assertLess(stub.calls.index("DELETE /api/v1/tokens/3"), stub.calls.index("PUT /api/v1/tokens"))
        # A second run replaces the tokens the first one made, and no more.
        self.mint(stub)
        self.assertEqual(len(stub.tokens), 3)

    def test_refuses_without_presets(self):
        stub = self.start()
        stub.presets = None
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs 2.7.0 or later", proc.stderr)
        self.assertEqual(stub.created, [])

    def test_refuses_without_a_bucket_route(self):
        stub = self.start()
        del stub.routes["projects"]["views_buckets_delete"]
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no DELETE route ending in /buckets/:bucket", proc.stderr)
        self.assertEqual(stub.created, [])

    def test_login_failure(self):
        stub = self.start()
        stub.login_status = 403
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("POST /api/v1/login failed: HTTP 403", proc.stderr)

    def test_unreachable(self):
        self.start()
        self.base = "http://127.0.0.1:9"
        proc = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot reach Vikunja at http://127.0.0.1:9", proc.stderr)

    def test_todo_sh_runs_it_through_stack_python(self):
        text = (ROOT / "scripts" / "todo.sh").read_text()
        self.assertIn('--env VK_TITLE_RW --env VK_TITLE_RO "${ROOT}/scripts/lib/vikunja_tokens.py")', text)
        self.assertNotIn("<<'PY'", text)
        stub = self.start()
        # The call as todo.sh makes it, in host mode (todo.sh drops the
        # network option there).
        cmd = ('source scripts/lib/python.sh; '
               'stack_python --env VK_BASE --env VK_USER --env VK_PASS '
               '--env VK_TITLE_RW --env VK_TITLE_RO "${ROOT}/scripts/lib/vikunja_tokens.py"')
        env = {**self.env(), "ROOT": str(ROOT), "STACK_PYTHON": "host"}
        proc = subprocess.run(["bash", "-c", cmd], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(stub.created), 2)
        self.assertIn("VIKUNJA_MCP_TOKEN_RO=", proc.stdout)


if __name__ == "__main__":
    unittest.main()
