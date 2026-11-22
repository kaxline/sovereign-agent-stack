#!/usr/bin/env python3
"""scripts/twenty_admin.py against a stub Twenty GraphQL server.

Checks the order of calls make crm-init makes, re-runs, rotation, the
single-workspace refusal, slug checks, and that no key or password is ever
printed. Workspaces live in the Twenty add-on's instance store under a scratch
data root. No Docker, no network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
import socket
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
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import addon_store  # noqa: E402

MANIFEST = json.loads((ROOT / "addons" / "twenty" / "addon.json").read_text())
PASSWORD = "pw-SECRET-0123!Aa1"
OP = re.compile(r"^\s*(?:mutation|query)?\s*(?:\([^)]*\))?\s*\{\s*(\w+)", re.S)


ALL_ACTIONS = ["canCreateObjectRecords", "canDestroyObjectRecords", "canReadObjectRecords",
               "canSoftDeleteObjectRecords", "canUpdateObjectRecords"]
WRITES = [a for a in ALL_ACTIONS if a != "canReadObjectRecords"]
# Shapes seen on Twenty v2.60.3 (v2.55 has the same field). agentChatThreadTarget
# is isUIReadOnly but has no disabled actions, so its write tools reach a role
# with no permission on it. Twenty refuses a grant of a disabled action.
OBJECTS = [
    {"id": "obj-person", "nameSingular": "person", "isUIReadOnly": False, "isActive": True,
     "disabledObjectPermissionActions": None},
    {"id": "obj-notetarget", "nameSingular": "noteTarget", "isUIReadOnly": False, "isActive": True,
     "disabledObjectPermissionActions": None},
    {"id": "obj-threadtarget", "nameSingular": "agentChatThreadTarget", "isUIReadOnly": True, "isActive": True,
     "disabledObjectPermissionActions": None},
    {"id": "obj-participant", "nameSingular": "agentChatThreadParticipant", "isUIReadOnly": True,
     "isActive": True, "disabledObjectPermissionActions": WRITES},
    {"id": "obj-agentturn", "nameSingular": "agentTurn", "isUIReadOnly": True, "isActive": True,
     "disabledObjectPermissionActions": ALL_ACTIONS},
    {"id": "obj-old", "nameSingular": "old", "isUIReadOnly": False, "isActive": False,
     "disabledObjectPermissionActions": None},
]


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
        self.refuse: dict[str, str] = {}
        self.sign_in_error: str | None = None
        self.key_calls: list[dict] = []
        self.login_tokens: list[str] = []

    def answer(self, query: str, variables: dict) -> dict:
        m = OP.match(query)
        op = m.group(1) if m else "?"
        self.ops.append(op)
        if op == self.fail_once:
            self.fail_once = None
            return {"errors": [{"message": f"stub failure in {op}"}]}
        if op in self.refuse:
            return {"errors": [{"message": self.refuse[op]}]}
        if op == "signIn" and self.sign_in_error:
            return {"errors": [{"message": self.sign_in_error}]}
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
            self.login_tokens.append(variables.get("t"))
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
            return {"data": {"objects": {"edges": [{"node": dict(n)} for n in OBJECTS]}}}
        if op == "upsertObjectPermissions":
            by_id = {n["id"]: n for n in OBJECTS}
            for p in variables["i"]["objectPermissions"]:
                obj = by_id[p["objectMetadataId"]]
                disabled = obj["disabledObjectPermissionActions"] or []
                if any(p.get(a) for a in disabled):
                    return {"errors": [{"message": "Validation errors occurred while upserting object permissions"}]}
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
            key = {"id": f"key-{len(self.keys) + 1}", "name": variables["i"]["name"], "revokedAt": None,
                   "roleId": variables["i"].get("roleId")}
            self.key_calls.append(variables["i"])
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
        self.droot = self.tmp / "droot"
        self.droot.mkdir()
        self.store = self.droot / "addons" / "twenty"
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
            "ADDONS_DATA_ROOT": str(self.droot),
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
        """The stored instance, with its keys read from secrets/."""
        values = json.loads((self.store / "instances" / f"{slug}.json").read_text())
        for name in ("key_ro", "key_rw"):
            path = self.store / "secrets" / f"{slug}.{name}"
            if path.exists():
                values[name] = path.read_text()
        return values

    def put(self, slug: str, values: dict) -> None:
        addon_store.apply_instance(MANIFEST, self.droot, slug, values)

    def test_first_init_order(self):
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops, [
            "signUp", "signUpInNewWorkspace", "getAuthTokensFromLoginToken", "activateWorkspace",
            "getRoles", "createOneRole", "createOneRole", "objects", "upsertObjectPermissions",
            "createApiKey", "generateApiKeyToken", "createApiKey", "generateApiKeyToken",
        ])
        f = self.file("main")
        self.assertEqual(f["url"], "http://twenty-server:3000")
        self.assertIs(f["local"], True)
        self.assertEqual(f["access"], "write")
        self.assertEqual(f["workspace_url"], "http://localhost:3000/")
        self.assertEqual(f["workspace_id"], "ws1")
        self.assertTrue(f["key_ro"].startswith("apikey-SECRET-"))
        self.assertTrue(f["key_rw"].startswith("apikey-SECRET-"))
        self.assertNotEqual(f["key_ro"], f["key_rw"])
        for name in ("key_ro", "key_rw"):
            path = self.store / "secrets" / f"main.{name}"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("SECRET", (self.store / "instances" / "main.json").read_text())
        self.assertEqual(self.hook_log.read_text(), "grant-admin owner@crm.localhost\n")
        self.assertTrue((self.data / ".crm-init-done").exists())

    def test_roles_have_the_spec_permissions(self):
        self.run_init()
        ro, rw = self.stub.roles
        self.assertEqual((ro["label"], rw["label"]), ("Hermes read-only", "Hermes read-write"))

    def _ro_upsert(self) -> dict[str, dict]:
        self.assertEqual(len(self.stub.upserts), 1)
        upsert = self.stub.upserts[0]
        self.assertEqual(upsert["roleId"], self.stub.roles[0]["id"])  # read-only, never read-write
        return {p["objectMetadataId"]: p for p in upsert["objectPermissions"]}

    @staticmethod
    def _actions(p: dict) -> tuple:
        return (p["canReadObjectRecords"], p["canUpdateObjectRecords"],
                p["canSoftDeleteObjectRecords"], p["canDestroyObjectRecords"])

    def test_read_only_role_denies_writes_object_by_object(self):
        # Twenty lets a role without canUpdateAllObjectRecords still write
        # junction objects (noteTarget, attachment...) and, from v2.60, the
        # isUIReadOnly agentChatThreadTarget, unless each object carries its own
        # permission. Read is granted only where the object doesn't disable it.
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        perms = self._ro_upsert()
        self.assertEqual(set(perms), {"obj-person", "obj-notetarget", "obj-threadtarget",
                                      "obj-participant", "obj-agentturn"})
        for oid in ("obj-person", "obj-notetarget", "obj-threadtarget", "obj-participant"):
            self.assertEqual(self._actions(perms[oid]), (True, False, False, False), oid)
        self.assertEqual(self._actions(perms["obj-agentturn"]), (False, False, False, False))

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
        self.assertEqual(partial["workspace_id"], "ws1")
        self.assertNotIn("key_rw", partial)
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops.count("signUpInNewWorkspace"), 1)
        done = self.file("main")
        self.assertEqual(done["workspace_id"], "ws1")
        self.assertTrue(done["key_rw"].startswith("apikey-SECRET-"))

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
        self.assertNotEqual(new["key_rw"], old["key_rw"])
        self.assertEqual(new["workspace_id"], old["workspace_id"])

    def test_rotate_keeps_the_access_setting(self):
        self.run_init()
        self.put("main", {"access": "read"})
        self.stub.sign_up_error = "User already exists"
        proc = self.run_init("--rotate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.file("main")["access"], "read")

    def run_link(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"],
            "TWENTY_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "ADDONS_DATA_ROOT": str(self.droot),
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
        self.put("hosted", {"url": "https://crm.example.com", "local": False, "access": "write", "key_rw": "x"})
        for slug, why in (("hosted", "local instance"), ("nope", "no workspace")):
            proc = self.run_link("--slug", slug)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn(why, proc.stderr)
        self.assertEqual(self.stub.ops, [])

    def test_login_link_workspace_the_owner_cannot_enter(self):
        self.run_init()
        self.put("main", {"workspace_id": "other"})
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
        self.assertEqual(self.file("sales")["workspace_url"], "http://sales.localhost:3000/")

    def test_remote_instances_do_not_count_as_local(self):
        self.put("hosted", {"url": "https://crm.example.com", "local": False, "access": "write", "key_rw": "x"})
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

    def test_list_prints_rows_without_keys(self):
        self.run_init()
        self.put("hosted", {"url": "https://crm.example.com", "local": False, "access": "read",
                            "key_ro": "ro-SECRET"})
        proc = subprocess.run([sys.executable, "-I", str(ADMIN), "list"],
                              env={"PATH": os.environ["PATH"], "ADDONS_DATA_ROOT": str(self.droot)},
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("SECRET", proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            "hosted\t0\tread\thttps://crm.example.com\t-\t-\t1\t0",
            "main\t1\twrite\thttp://twenty-server:3000\thttp://localhost:3000/\tws1\t1\t1",
        ])

    def test_bad_slug_rejected(self):
        proc = self.run_init("--slug", "Sales_1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.stub.ops, [])

    def test_graphql_error_leaves_no_keys(self):
        self.stub.roles = None  # getRoles answers null; ensure_roles must fail cleanly
        proc = self.run_init()
        self.assertNotEqual(proc.returncode, 0)
        # Only the workspace's identity is kept, for the resume; the bootstrap skips an instance without keys.
        self.assertEqual(set(self.file("main")), {"url", "local", "access", "workspace_url", "workspace_id"})


class CrmKey(unittest.TestCase):
    """crm.sh key create|revoke|list: one person's API key (Boundary CR 0025 R1, R3)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.droot = self.tmp / "droot"
        self.droot.mkdir()
        self.stub = Stub()
        self.server = serve(self.stub)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        # A local workspace with both roles in place, as crm init leaves it.
        self.run_init()
        self.stub.ops.clear()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def run_init(self) -> None:
        env = {"PATH": os.environ["PATH"], "TWENTY_BASE_URL": self.base,
               "ADDONS_DATA_ROOT": str(self.droot), "TWENTY_DATA_DIR": str(self.tmp / "data"),
               "TWENTY_USERNAME": "owner@crm.localhost", "TWENTY_PASSWORD": PASSWORD}
        proc = subprocess.run([sys.executable, "-I", str(ADMIN), "init"], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def key(self, *args: str, base: str | None = None, **extra: str) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "TWENTY_BASE_URL": base or self.base,
               "ADDONS_DATA_ROOT": str(self.droot), "TWENTY_USERNAME": "owner@crm.localhost",
               "TWENTY_PASSWORD": PASSWORD, **extra}
        proc = subprocess.run([sys.executable, "-I", str(ADMIN), "key", *args],
                              env=env, capture_output=True, text=True)
        # The token belongs on create's stdout line only; the password and the
        # session token belong nowhere.
        for secret in (PASSWORD, "access-SECRET", "agnostic-SECRET"):
            self.assertNotIn(secret, proc.stdout + proc.stderr, "output leaks a secret")
        self.assertNotIn("apikey-SECRET", proc.stderr, "the key token must stay off stderr")
        return proc

    def lines(self, proc: subprocess.CompletedProcess) -> list[dict]:
        return [json.loads(line) for line in proc.stdout.splitlines()]

    def test_create_read_prints_id_and_token_with_the_read_only_role(self):
        proc = self.key("create", "--name", "Ada read", "--access", "read")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines(proc), [{"id": "key-3", "token": "apikey-SECRET-3"}])
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        ro = next(r for r in self.stub.roles if r["label"] == "Hermes read-only")
        self.assertEqual(self.stub.key_calls[-1]["roleId"], ro["id"])
        self.assertEqual(self.stub.key_calls[-1]["name"], "Ada read")
        self.assertEqual(self.stub.key_calls[-1]["expiresAt"], "2126-01-01T00:00:00Z")

    def test_create_write_uses_the_read_write_role(self):
        proc = self.key("create", "--name", "Ada write", "--access", "write")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rw = next(r for r in self.stub.roles if r["label"] == "Hermes read-write")
        self.assertEqual(self.stub.key_calls[-1]["roleId"], rw["id"])

    def test_create_makes_missing_roles_as_crm_init_does(self):
        self.stub.roles.clear()
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.ops.count("createOneRole"), 2)

    def test_create_when_twenty_refuses_the_role_is_no_role(self):
        self.stub.roles.clear()
        self.stub.refuse["createOneRole"] = "Role creation refused"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.lines(proc)[0]["error"], "no_role")
        self.assertNotIn("createApiKey", self.stub.ops)

    def test_revoke_calls_revoke_and_prints_nothing(self):
        self.key("create", "--name", "Ada", "--access", "read")
        self.stub.ops.clear()
        proc = self.key("revoke", "--id", "key-3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("revokeApiKey", self.stub.ops)
        self.assertEqual(self.stub.keys[2]["revokedAt"], "now")

    def test_list_marks_revoked_keys_and_filters_by_name(self):
        self.key("create", "--name", "Ada", "--access", "read")
        self.key("create", "--name", "Ada", "--access", "write")
        self.key("create", "--name", "Bo", "--access", "read")
        self.key("revoke", "--id", "key-3")
        proc = self.key("list", "--name", "Ada", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines(proc), [
            {"id": "key-3", "name": "Ada", "revoked": True},
            {"id": "key-4", "name": "Ada", "revoked": False},
        ])

    def test_wrong_password_is_login_refused_and_mints_nothing(self):
        self.stub.sign_in_error = "Wrong password"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 4)
        out = self.lines(proc)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["error"], "login_refused")
        self.assertIsInstance(out[0]["message"], str)
        self.assertNotIn("createApiKey", self.stub.ops)

    def test_roles_refused_for_permissions_is_no_permission(self):
        self.stub.refuse["getRoles"] = "Forbidden: you don't have permission"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.lines(proc)[0]["error"], "no_permission")
        self.assertNotIn("createApiKey", self.stub.ops)

    def test_connection_refused_is_unreachable(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        proc = self.key("list", "--name", "Ada", "--json", base=f"http://127.0.0.1:{port}")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.lines(proc)[0]["error"], "unreachable")

    def test_other_slug_uses_the_stored_url_and_the_owner_variables(self):
        addon_store.apply_instance(MANIFEST, self.droot, "hosted",
                                   {"url": self.base, "local": False, "access": "write", "key_rw": "x"})
        proc = self.key("create", "--name", "Ada", "--access", "read", "--slug", "hosted",
                        base="http://127.0.0.1:1", TWENTY_USERNAME="", TWENTY_PASSWORD="",
                        TWENTY_OWNER_EMAIL="boss@example.com", TWENTY_OWNER_PASSWORD=PASSWORD)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines(proc)[0]["id"], "key-3")
        self.assertEqual(self.stub.passwords[-1], PASSWORD)

    def test_other_slug_without_owner_variables_is_a_usage_error(self):
        addon_store.apply_instance(MANIFEST, self.droot, "hosted",
                                   {"url": self.base, "local": False, "access": "write", "key_rw": "x"})
        proc = self.key("list", "--name", "Ada", "--slug", "hosted")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("TWENTY_OWNER_EMAIL", proc.stderr)
        self.assertIn("TWENTY_OWNER_PASSWORD", proc.stderr)
        self.assertEqual(self.stub.ops, [])

    def test_unknown_slug_is_a_usage_error(self):
        proc = self.key("list", "--name", "Ada", "--slug", "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.stub.ops, [])


    def hosted(self, **values) -> None:
        addon_store.apply_instance(MANIFEST, self.droot, "hosted",
                                   {"url": self.base, "local": False, "access": "write", "key_rw": "x", **values})

    HOSTED_ENV = {"TWENTY_OWNER_EMAIL": "boss@example.com", "TWENTY_OWNER_PASSWORD": PASSWORD}

    def test_no_stored_workspace_id_and_two_workspaces_is_ambiguous(self):
        self.hosted()
        self.stub.workspaces = 2
        proc = self.key("create", "--name", "Ada", "--access", "read", "--slug", "hosted", **self.HOSTED_ENV)
        self.assertEqual(proc.returncode, 4)
        out = self.lines(proc)
        self.assertEqual(out[0]["error"], "ambiguous_workspace")
        self.assertIn("workspace ID", out[0]["message"])
        self.assertNotIn("createApiKey", self.stub.ops)
        self.assertNotIn("getAuthTokensFromLoginToken", self.stub.ops)

    def test_stored_workspace_id_picks_that_workspace(self):
        self.hosted(workspace_id="ws2")
        self.stub.workspaces = 2
        self.stub.login_tokens.clear()
        proc = self.key("create", "--name", "Ada", "--access", "read", "--slug", "hosted", **self.HOSTED_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stub.login_tokens, ["login-SECRET-2"])

    def test_revoke_of_an_already_revoked_key_succeeds_silently(self):
        self.key("create", "--name", "Ada", "--access", "read")
        self.key("revoke", "--id", "key-3")
        self.stub.ops.clear()
        proc = self.key("revoke", "--id", "key-3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("revokeApiKey", self.stub.ops)

    def test_revoke_of_an_unknown_key_is_not_found(self):
        proc = self.key("revoke", "--id", "key-99")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(self.lines(proc)[0]["error"], "not_found")
        self.assertNotIn("revokeApiKey", self.stub.ops)

    def test_revoke_errors_are_no_permission_or_failed(self):
        self.key("create", "--name", "Ada", "--access", "read")
        self.stub.refuse["revokeApiKey"] = "Forbidden: you don't have permission"
        proc = self.key("revoke", "--id", "key-3")
        self.assertEqual((proc.returncode, self.lines(proc)[0]["error"]), (4, "no_permission"))
        self.stub.refuse["revokeApiKey"] = "database exploded"
        proc = self.key("revoke", "--id", "key-3")
        self.assertEqual((proc.returncode, self.lines(proc)[0]["error"]), (4, "failed"))
        self.assertIn("database exploded", self.lines(proc)[0]["message"])

    def test_other_graphql_errors_on_list_and_create_are_failed(self):
        self.stub.refuse["apiKeys"] = "internal error"
        proc = self.key("list", "--name", "Ada", "--json")
        self.assertEqual(self.lines(proc)[0]["error"], "failed")
        del self.stub.refuse["apiKeys"]
        self.stub.refuse["createApiKey"] = "internal error"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(self.lines(proc)[0]["error"], "failed")

    def test_a_token_failure_revokes_the_new_key(self):
        self.stub.refuse["generateApiKeyToken"] = "token service down"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 4)
        out = self.lines(proc)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["error"], "failed")
        self.assertIn("revokeApiKey", self.stub.ops)
        self.assertEqual(self.stub.keys[-1]["revokedAt"], "now")
        self.assertNotIn("key-3", out[0]["message"])

    def test_a_token_failure_and_a_failed_revoke_names_the_key_id(self):
        self.stub.refuse["generateApiKeyToken"] = "token service down"
        self.stub.refuse["revokeApiKey"] = "still down"
        proc = self.key("create", "--name", "Ada", "--access", "read")
        self.assertEqual(proc.returncode, 4)
        out = self.lines(proc)
        self.assertEqual(out[0]["error"], "failed")
        self.assertIn("key-3", out[0]["message"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
