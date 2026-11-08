#!/usr/bin/env python3
"""scripts/twenty_admin.py against a stub Twenty GraphQL server.

Checks the order of calls make crm-init makes, re-runs, rotation, the
single-workspace refusal, slug checks, and that no key or password is ever
printed. No Docker, no network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "scripts" / "twenty_admin.py"
PASSWORD = "pw-SECRET-0123!Aa1"
OP = re.compile(r"^\s*(?:mutation|query)?\s*(?:\([^)]*\))?\s*\{\s*(\w+)", re.S)


class Stub:
    """Canned Twenty answers; records the first field of every operation."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.sign_up_error: str | None = None
        self.roles: list[dict] = []
        self.keys: list[dict] = []
        self.minted = 0
        self.fail_once: str | None = None
        self.workspaces = 0
        self.upserts: list[dict] = []
        self.passwords: list[str] = []

    def answer(self, query: str, variables: dict) -> dict:
        m = OP.match(query)
        op = m.group(1) if m else "?"
        self.ops.append(op)
        if op == self.fail_once:
            self.fail_once = None
            return {"errors": [{"message": f"stub failure in {op}"}]}
        tokens = {"tokens": {"accessOrWorkspaceAgnosticToken": {"token": "agnostic-SECRET"}}}
        if op in ("signUp", "signIn"):
            self.passwords.append(variables.get("p"))
        if op == "signUp":
            if self.sign_up_error:
                return {"errors": [{"message": self.sign_up_error}]}
            return {"data": {"signUp": tokens}}
        if op == "signIn":
            avail = {"availableWorkspacesForSignIn": [
                {"id": f"ws{i + 1}", "loginToken": f"login-SECRET-{i + 1}"} for i in range(max(self.workspaces, 1))
            ]}
            return {"data": {"signIn": {**tokens, "availableWorkspaces": avail}}}
        if op == "signUpInNewWorkspace":
            self.workspaces += 1
            sub = (variables.get("i") or {}).get("subdomain")
            url = f"http://{sub}.localhost:3000/" if sub else "http://localhost:3000/"
            return {"data": {"signUpInNewWorkspace": {
                "loginToken": {"token": "login-SECRET"},
                "workspace": {"id": f"ws{self.workspaces}", "workspaceUrls": {"subdomainUrl": url}},
            }}}
        if op == "getAuthTokensFromLoginToken":
            return {"data": {"getAuthTokensFromLoginToken": {"tokens": {
                "accessOrWorkspaceAgnosticToken": {"token": "access-SECRET"}}}}}
        if op == "activateWorkspace":
            return {"data": {"activateWorkspace": {"id": "ws", "activationStatus": "ACTIVE"}}}
        if op == "getRoles":
            return {"data": {"getRoles": self.roles}}
        if op == "createOneRole":
            role = {"id": f"role-{len(self.roles) + 1}", "label": variables["i"]["label"]}
            self.roles.append(role)
            return {"data": {"createOneRole": role}}
        if op == "objects":
            return {"data": {"objects": {"edges": [
                {"node": {"id": "obj-person", "nameSingular": "person", "isUIReadOnly": False, "isActive": True}},
                {"node": {"id": "obj-notetarget", "nameSingular": "noteTarget", "isUIReadOnly": False, "isActive": True}},
                {"node": {"id": "obj-agentturn", "nameSingular": "agentTurn", "isUIReadOnly": True, "isActive": True}},
                {"node": {"id": "obj-old", "nameSingular": "old", "isUIReadOnly": False, "isActive": False}},
            ]}}}
        if op == "upsertObjectPermissions":
            self.upserts.append(variables["i"])
            return {"data": {"upsertObjectPermissions": [
                {"objectMetadataId": o["objectMetadataId"]} for o in variables["i"]["objectPermissions"]]}}
        if op == "apiKeys":
            return {"data": {"apiKeys": self.keys}}
        if op == "revokeApiKey":
            for k in self.keys:
                if k["id"] == variables["i"]["id"]:
                    k["revokedAt"] = "now"
            return {"data": {"revokeApiKey": {"id": variables["i"]["id"]}}}
        if op == "createApiKey":
            key = {"id": f"key-{len(self.keys) + 1}", "name": variables["i"]["name"], "revokedAt": None}
            self.keys.append(key)
            return {"data": {"createApiKey": key}}
        if op == "generateApiKeyToken":
            self.minted += 1
            return {"data": {"generateApiKeyToken": {"token": f"apikey-SECRET-{self.minted}"}}}
        return {"errors": [{"message": f"stub has no answer for {op}"}]}


def serve(stub: Stub) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            out = json.dumps(stub.answer(body["query"], body.get("variables") or {})).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CrmInit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "workspaces"
        self.ws.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.hook_log = self.tmp / "grant.log"
        (self.tmp / "dir with space").mkdir()
        hook = self.tmp / "dir with space" / "grant.sh"
        hook.write_text(f'#!/bin/sh\necho "$1 $2" >> "{self.hook_log}"\n')
        hook.chmod(0o755)
        self.hook = hook
        self.stub = Stub()
        self.server = serve(self.stub)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def run_init(self, *args: str, multi: str = "0", password: str = PASSWORD,
                 password_file: str = "") -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"],
            "TWENTY_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "TWENTY_WORKSPACES_DIR": str(self.ws),
            "TWENTY_DATA_DIR": str(self.data),
            "TWENTY_USERNAME": "owner@crm.localhost",
            "TWENTY_PASSWORD": password,
            "TWENTY_MULTIWORKSPACE": multi,
            "TWENTY_GRANT_ADMIN_CMD": str(self.hook),
        }
        if password_file:
            env["TWENTY_PASSWORD_FILE"] = password_file
        proc = subprocess.run([sys.executable, "-I", str(ADMIN), "init", *args],
                              env=env, capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.last_out = out
        secrets = ["SECRET", PASSWORD]
        if password_file and Path(password_file).exists():
            secrets.append(Path(password_file).read_text().strip())
        for secret in secrets:
            self.assertNotIn(secret, out, f"output leaks a secret:\n{out}")
        return proc

    def file(self, slug: str) -> dict:
        lines = (self.ws / f"{slug}.env").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))

    def test_first_init_order(self):
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops, [
            "signUp", "signUpInNewWorkspace", "getAuthTokensFromLoginToken", "activateWorkspace",
            "getRoles", "createOneRole", "createOneRole", "objects", "upsertObjectPermissions",
            "createApiKey", "generateApiKeyToken", "createApiKey", "generateApiKeyToken",
        ])
        f = self.file("main")
        self.assertEqual(f["TWENTY_URL"], "http://twenty-server:3000")
        self.assertEqual(f["TWENTY_WORKSPACE_URL"], "http://localhost:3000/")
        self.assertEqual(f["TWENTY_WORKSPACE_ID"], "ws1")
        self.assertTrue(f["TWENTY_KEY_RO"].startswith("apikey-SECRET-"))
        self.assertTrue(f["TWENTY_KEY_RW"].startswith("apikey-SECRET-"))
        self.assertNotEqual(f["TWENTY_KEY_RO"], f["TWENTY_KEY_RW"])
        self.assertEqual(stat.S_IMODE((self.ws / "main.env").stat().st_mode), 0o600)
        self.assertEqual(self.hook_log.read_text(), "grant-admin owner@crm.localhost\n")
        self.assertTrue((self.data / ".crm-init-done").exists())

    def test_roles_have_the_spec_permissions(self):
        self.run_init()
        ro, rw = self.stub.roles
        self.assertEqual((ro["label"], rw["label"]), ("Hermes read-only", "Hermes read-write"))

    def test_read_only_role_denies_writes_object_by_object(self):
        # Twenty v2.55 lets a role without canUpdateAllObjectRecords still write
        # junction objects (noteTarget, attachment...) unless each object carries
        # its own permission. It refuses permissions on isUIReadOnly objects.
        self.run_init()
        self.assertEqual(len(self.stub.upserts), 1)
        upsert = self.stub.upserts[0]
        self.assertEqual(upsert["roleId"], self.stub.roles[0]["id"])  # read-only, never read-write
        perms = {p["objectMetadataId"]: p for p in upsert["objectPermissions"]}
        self.assertEqual(set(perms), {"obj-person", "obj-notetarget"})
        for p in perms.values():
            self.assertEqual((p["canReadObjectRecords"], p["canUpdateObjectRecords"],
                              p["canSoftDeleteObjectRecords"], p["canDestroyObjectRecords"]),
                             (True, False, False, False))

    def test_rerun_signs_in(self):
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops[:3], ["signUp", "signIn", "signUpInNewWorkspace"])
        self.assertEqual(self.stub.ops.count("signUp"), 1)
        # The grant is idempotent and runs every time, so a failed first grant is repaired.
        self.assertEqual(self.hook_log.read_text(), "grant-admin owner@crm.localhost\n")

    def test_failure_after_workspace_creation_resumes(self):
        self.stub.fail_once = "upsertObjectPermissions"
        proc = self.run_init()
        self.assertNotEqual(proc.returncode, 0)
        partial = self.file("main")
        self.assertEqual(partial["TWENTY_WORKSPACE_ID"], "ws1")
        self.assertNotIn("TWENTY_KEY_RW", partial)
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops.count("signUpInNewWorkspace"), 1)
        done = self.file("main")
        self.assertEqual(done["TWENTY_WORKSPACE_ID"], "ws1")
        self.assertTrue(done["TWENTY_KEY_RW"].startswith("apikey-SECRET-"))

    def test_partial_file_blocks_a_second_workspace_in_single_mode(self):
        self.stub.fail_once = "createApiKey"
        self.run_init()
        self.stub.ops.clear()
        proc = self.run_init("--slug", "sales")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.stub.ops, [])

    def test_failed_grant_is_retried(self):
        failing = self.tmp / "dir with space" / "fail.sh"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(0o755)
        good = self.hook
        self.hook = failing
        self.assertNotEqual(self.run_init().returncode, 0)
        self.hook = good
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.hook_log.read_text(), "grant-admin owner@crm.localhost\n")

    def test_existing_slug_untouched(self):
        self.run_init()
        before = self.file("main")
        self.stub.ops.clear()
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops, [])
        self.assertEqual(self.file("main"), before)

    def test_rotate_revokes_then_mints(self):
        self.run_init()
        old = self.file("main")
        self.stub.ops.clear()
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init("--rotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("signUpInNewWorkspace", self.stub.ops)
        self.assertEqual(self.stub.ops.count("revokeApiKey"), 2)
        self.assertLess(self.stub.ops.index("revokeApiKey"), self.stub.ops.index("createApiKey"))
        new = self.file("main")
        self.assertNotEqual(new["TWENTY_KEY_RW"], old["TWENTY_KEY_RW"])
        self.assertEqual(new["TWENTY_WORKSPACE_ID"], old["TWENTY_WORKSPACE_ID"])

    def test_rotate_keeps_lines_init_does_not_own(self):
        self.run_init()
        path = self.ws / "main.env"
        path.write_text(path.read_text() + "TWENTY_ACCESS=read\nTWENTY_EXTRA=kept\n")
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init("--rotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        new = self.file("main")
        self.assertEqual(new["TWENTY_ACCESS"], "read")
        self.assertEqual(new["TWENTY_EXTRA"], "kept")
        self.assertEqual(path.read_text().count("TWENTY_ACCESS="), 1)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def run_link(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"],
            "TWENTY_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "TWENTY_WORKSPACES_DIR": str(self.ws),
            "TWENTY_USERNAME": "owner@crm.localhost",
            "TWENTY_PASSWORD": PASSWORD,
            **extra,
        }
        proc = subprocess.run([sys.executable, "-I", str(ADMIN), "login-link", *args],
                              env=env, capture_output=True, text=True)
        self.assertNotIn(PASSWORD, proc.stdout + proc.stderr)
        self.assertNotIn("SECRET", proc.stderr, "the login token must stay off stderr")
        return proc

    def test_login_link_prints_only_the_workspace_url(self):
        self.run_init(multi="1")
        self.run_init("--slug", "sales", multi="1")
        self.stub.ops.clear()
        proc = self.run_link("--slug", "sales")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "http://sales.localhost:3000/verify?loginToken=login-SECRET-2\n")
        self.assertEqual(self.stub.ops, ["signIn"])

    def test_login_link_defaults_to_main(self):
        self.run_init()
        proc = self.run_link()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "http://localhost:3000/verify?loginToken=login-SECRET-1\n")

    def test_login_link_refuses_remote_and_unknown_workspaces(self):
        (self.ws / "hosted.env").write_text("TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=x\n")
        for slug, why in (("hosted", "local instance"), ("nope", "no workspace")):
            proc = self.run_link("--slug", slug)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn(why, proc.stderr)
        self.assertEqual(self.stub.ops, [])

    def test_login_link_workspace_the_owner_cannot_enter(self):
        self.run_init()
        path = self.ws / "main.env"
        path.write_text(path.read_text().replace("TWENTY_WORKSPACE_ID=ws1", "TWENTY_WORKSPACE_ID=other"))
        proc = self.run_link()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("can't enter", proc.stderr)

    def test_login_link_never_creates_a_password_file(self):
        self.run_init()
        missing = self.tmp / "pw"
        proc = self.run_link(TWENTY_PASSWORD="", TWENTY_PASSWORD_FILE=str(missing))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(missing.exists())

    def test_single_mode_refuses_second_workspace(self):
        self.run_init()
        self.stub.ops.clear()
        proc = self.run_init("--slug", "sales")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.stub.ops, [])
        self.assertIn("TWENTY_MULTIWORKSPACE", proc.stderr)

    def test_multi_mode_second_workspace(self):
        self.run_init(multi="1")
        proc = self.run_init("--slug", "sales", multi="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.file("sales")["TWENTY_WORKSPACE_URL"], "http://sales.localhost:3000/")

    def test_remote_files_do_not_count_as_local(self):
        (self.ws / "hosted.env").write_text("TWENTY_URL=https://crm.example.com\nTWENTY_KEY_RW=x\n")
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_password_outside_twentys_limits_fails_before_any_call(self):
        # Twenty v2.55 accepts 8 to 50 characters and answers "Password too weak" otherwise.
        for bad in ("short1!", "x" * 51):
            proc = self.run_init(password=bad)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("8 to 50", proc.stderr)
        self.assertEqual(self.stub.ops, [])

    def test_summary_names_url_user_and_where_the_password_is(self):
        self.run_init()
        last = self.last_out.strip().splitlines()[-1]
        self.assertIn("http://localhost:3000/", last)
        self.assertIn("owner@crm.localhost", last)
        self.assertIn("TWENTY_PASSWORD in .env", last)

    def test_password_file_is_generated_once_and_reused(self):
        pw_file = self.tmp / "secrets" / "twenty-owner"
        proc = self.run_init(password="", password_file=str(pw_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(pw_file.stat().st_mode), 0o600)
        generated = pw_file.read_text().strip()
        self.assertTrue(8 <= len(generated) <= 50)
        self.assertEqual(self.stub.passwords, [generated])
        self.assertIn(str(pw_file), self.last_out.strip().splitlines()[-1])
        self.stub.sign_up_error = "User already exists"
        self.run_init("--rotate", password="", password_file=str(pw_file))
        self.assertEqual(pw_file.read_text().strip(), generated)
        self.assertTrue(all(p == generated for p in self.stub.passwords))

    def test_password_value_and_file_together_fail(self):
        pw_file = self.tmp / "pw"
        pw_file.write_text("file-password-123\n")
        proc = self.run_init(password_file=str(pw_file))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TWENTY_PASSWORD_FILE", proc.stderr)
        self.assertEqual(self.stub.ops, [])

    def test_bad_slug_rejected(self):
        proc = self.run_init("--slug", "Sales_1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.stub.ops, [])

    def test_graphql_error_leaves_no_keys(self):
        self.stub.roles = None  # getRoles answers null; ensure_roles must fail cleanly
        proc = self.run_init()
        self.assertNotEqual(proc.returncode, 0)
        # Only the workspace's identity is kept, for the resume; Hermes skips a file without keys.
        self.assertEqual(set(self.file("main")), {"TWENTY_URL", "TWENTY_WORKSPACE_URL", "TWENTY_WORKSPACE_ID"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
