#!/bin/sh
# compose/bitwarden-broker/setup/setup.py against scripts/fixtures/fake-bw.py
# and a fake organization API (prelogin, token, invite, member edit) on a free
# loopback port. Each run happens under a pty, so getpass and the prompts read
# from a real terminal. Checks creation per mode and scope, seeding by host,
# the transfer notice, grant, the role change, list-unassigned, three wrong
# passwords, lock and logout on every exit path (SIGINT and SIGTERM included),
# the temporary bw directory removed, and no password, token or item secret in
# any output. Needs host python3 only.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$ROOT" "$tmp" <<'PY'
import base64
import hashlib
import http.server
import json
import os
import pty
import secrets
import select
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

ROOT, TMP = Path(sys.argv[1]), Path(sys.argv[2])
SETUP = ROOT / "compose/bitwarden-broker/setup/setup.py"
FAKE_BW = ROOT / "scripts/fixtures/fake-bw.py"
USER_EMAIL = "Owner@Example.com"
USER_PW = f"syn-pw-{secrets.token_hex(6)}+/%é"
AGENT_EMAIL = "agent@example.com"
ORG = "org-5b1e"
ITERATIONS = 5000  # not 600000: setup must ask prelogin
TOKEN = f"syn-token-{secrets.token_hex(12)}"
NOTICE = ("Moving an item into an organization collection transfers it from your personal vault to the "
          "organization. Members with access to the collection can see it.")
SECRET_VALUES = {USER_PW, TOKEN}
failures = []


def fail(msg):
    failures.append(msg)


def check(cond, msg):
    if not cond:
        fail(msg)


def expected_hash(email, password, iterations):
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), email.strip().lower().encode(), iterations, 32)
    return base64.b64encode(hashlib.pbkdf2_hmac("sha256", key, password.encode(), 1, 32)).decode()


class Api(http.server.BaseHTTPRequestHandler):
    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_POST(self):
        st = self.server.st
        body = self._body()
        st["calls"].append(("POST", self.path, body))
        if self.path == "/identity/accounts/prelogin":
            return self._reply(200, st.get("prelogin") or {"kdf": 0, "kdfIterations": ITERATIONS})
        if self.path == "/identity/connect/token":
            form = {k: v[0] for k, v in parse_qs(body).items()}
            st["token_scope"] = form.get("scope")
            if st.get("redirect"):
                self.send_response(307)
                self.send_header("Location", f"{BASE}/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if st["refuse"]:
                return self._reply(400, {"error": "invalid_grant", "error_description": "new device verification"})
            ok = (form.get("grant_type") == "password" and form.get("username") == USER_EMAIL
                  and form.get("password") == expected_hash(USER_EMAIL, USER_PW, ITERATIONS)
                  and "api" in (form.get("scope") or "").split())
            st["token_ok"] = ok
            if not ok:
                return self._reply(400, {"error": "invalid_grant"})
            return self._reply(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600})
        if self.path == f"/api/organizations/{ORG}/users/invite":
            if not self._authed():
                return self._reply(401, {})
            payload = json.loads(body)
            st["invites"].append(payload)
            vault = json.loads(st["vault"].read_text())
            for email in payload["emails"]:
                vault["members"].append({"id": f"m-{uuid.uuid4().hex[:8]}", "email": email, "status": 0,
                                         "type": payload["type"], "organizationId": ORG})
            st["vault"].write_text(json.dumps(vault))
            return self._reply(200, {})
        self._reply(404, {})

    def do_GET(self):
        self.server.st["calls"].append(("GET", self.path, ""))
        self._reply(404, {})

    def do_PUT(self):
        st = self.server.st
        body = self._body()
        st["calls"].append(("PUT", self.path, body))
        if not self._authed():
            return self._reply(401, {})
        st["edits"].append((self.path, json.loads(body)))
        self._reply(200, {})

    def log_message(self, *args):
        pass


api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
api.daemon_threads = True
threading.Thread(target=api.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{api.server_address[1]}"


def item(ident, name, uri, **extra):
    pw = f"syn-item-{secrets.token_hex(6)}"
    seed = "JBSWY3DPEHPK3PXP" + secrets.token_hex(2).upper().replace("0", "A").replace("1", "B")
    notes = f"syn-note-{secrets.token_hex(6)}"
    SECRET_VALUES.update({pw, seed, notes})
    obj = {"id": ident, "type": 1, "name": name, "notes": notes, "folderId": None, "organizationId": None,
           "collectionIds": [], "login": {"username": f"{ident}@example.com", "password": pw, "totp": seed,
                                          "uris": [{"uri": uri, "match": None}]}}
    obj.update(extra)
    return obj


def base_vault():
    return {
        "user_email": USER_EMAIL, "user_password": USER_PW, "master_password": "syn-agent-unused",
        "folders": [], "collections": [], "members": [
            {"id": "m-owner", "email": USER_EMAIL.lower(), "status": 2, "type": 0, "organizationId": ORG}],
        "items": [
            item("i-li-www", "LinkedIn", "https://www.linkedin.com/login"),
            item("i-li-bare", "LinkedIn second", "https://linkedin.com/"),
            item("i-notli", "Not LinkedIn", "https://notlinkedin.com/"),
            item("i-android", "LinkedIn app", "androidapp://com.linkedin.android"),
            item("i-gh", "GitHub", "https://github.com/"),
        ],
    }


def pty_run(argv, env, script, timeout=20.0):
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(sys.executable, [sys.executable, str(SETUP), *argv], env)
        finally:
            os._exit(127)
    buf, pos, eof = b"", 0, False

    def read_some(wait):
        nonlocal buf, eof
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            return
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            eof = True
        buf += chunk

    for expect, action in script:
        want = expect.encode()
        deadline = time.monotonic() + timeout
        while want not in buf[pos:] and not eof and time.monotonic() < deadline:
            read_some(0.1)
        if want not in buf[pos:]:
            fail(f"{argv[0]}: never saw {expect!r}")
            break
        pos = buf.index(want, pos) + len(want)
        time.sleep(0.05)
        if isinstance(action, str):
            os.write(fd, (action + "\n").encode())
        else:
            os.kill(pid, action)
    deadline = time.monotonic() + timeout
    while not eof and time.monotonic() < deadline:
        read_some(0.1)
    done, status = os.waitpid(pid, os.WNOHANG)
    if not done:
        time.sleep(0.5)
        done, status = os.waitpid(pid, os.WNOHANG)
    if not done:
        fail(f"{argv[0]}: still running; killed")
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), buf.decode("utf-8", "replace").replace("\r\n", "\n")


class Run:
    def __init__(self, name, cmd, account, vault, extra=(), script=None, slug="acct", refuse=False,
                 server=BASE, redirect=False):
        self.name = name
        d = TMP / name
        (d / "accounts").mkdir(parents=True)
        self.vault_file = d / "vault.json"
        self.vault_file.write_text(json.dumps(vault))
        self.vault_before = json.loads(json.dumps(vault))
        self.log_file = d / "bw.log"
        self.account_file = d / "accounts" / f"{slug}.env"
        self.account_file.write_text("\n".join(account) + "\n")
        self.account_file.chmod(0o600)
        wrapper = d / "bw"
        wrapper.write_text(f"#!/bin/sh\nexport FAKE_BW_VAULT='{self.vault_file}'\n"
                           f"export FAKE_BW_LOG='{self.log_file}'\nexec '{sys.executable}' '{FAKE_BW}' \"$@\"\n")
        wrapper.chmod(0o755)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "PYTHONDONTWRITEBYTECODE": "1",
               "BITWARDEN_ACCOUNTS_DIR": str(d / "accounts"), "BITWARDEN_BW": str(wrapper), "LANG": "C.UTF-8"}
        api.st = {"calls": [], "invites": [], "edits": [], "refuse": refuse, "vault": self.vault_file,
                  "token_ok": None, "redirect": redirect}
        argv = [cmd, "--server", server, "--email", USER_EMAIL, "--slug", slug, *extra]
        self.code, self.out = pty_run(argv, env, script if script is not None else [("Master password", USER_PW)])
        self.api = api.st
        self.log_text = self.log_file.read_text() if self.log_file.exists() else ""
        self.calls = [json.loads(line) for line in self.log_text.splitlines()]
        self.argvs = [c["argv"] for c in self.calls]
        self.vault = json.loads(self.vault_file.read_text())
        for value in SECRET_VALUES:
            if value in self.out or value in self.log_text:
                fail(f"{name}: a password, token or item secret appears in output or bw argv")
                break
        good_hash = expected_hash(USER_EMAIL, USER_PW, ITERATIONS)
        for method, path, body in self.api["calls"]:
            texts = (body, unquote_plus(body))
            if any(v in t for v in SECRET_VALUES for t in texts):
                fail(f"{name}: a secret value was sent in {method} {path}")
            if path != "/identity/connect/token" and any(good_hash in t for t in texts):
                fail(f"{name}: the master password hash was sent in {method} {path}")
        for call in self.calls:
            if not (call["appdata"] or "").startswith(("/tmp/", "/private/tmp/")):
                fail(f"{name}: bw ran with appdata outside /tmp: {call['appdata']}")
        appdirs = {c["appdata"] for c in self.calls}
        check(len(appdirs) <= 1, f"{name}: more than one appdata dir: {appdirs}")
        for a in appdirs:
            check(a and not os.path.exists(a), f"{name}: temporary appdata dir left behind")

    def item(self, ident):
        return next(i for i in self.vault["items"] if i["id"] == ident)

    def cmds(self, *names):
        return [a for a in self.argvs if a[:1] and a[0] in names]

    def expect_code(self, code):
        check(self.code == code, f"{self.name}: exit {self.code}, want {code}\n{self.out}")

    def expect_lock_logout(self):
        tail = [a[0] for a in self.argvs[-2:]]
        check(tail == ["lock", "logout"], f"{self.name}: last bw calls {tail}, want lock then logout")


ACCOUNT = ["BW_MODE=cloud", "BW_SERVER=https://vault.bitwarden.com", "BW_UNLOCK=stored", "BW_CLIENTID=user.syn"]


def account(scope, mode="cloud", server="https://vault.bitwarden.com", extra=()):
    return [f"BW_MODE={mode}", f"BW_SERVER={server}", f"BW_SCOPE={scope}", "BW_UNLOCK=stored",
            "BW_CLIENTID=user.syn", *extra]


def with_collection(vault, cid="c-agent", name="Agent", users=()):
    vault["collections"].append({"id": cid, "organizationId": ORG, "name": name, "externalId": None,
                                 "groups": [], "users": list(users)})
    return vault


# create, folder scope (cloud): bw create folder, no org API.
r = Run("create-folder", "create", account("folder:Agent"), base_vault())
r.expect_code(0)
check([f["name"] for f in r.vault["folders"]] == ["Agent"], f"create-folder: folders {r.vault['folders']}")
check(["config", "server", BASE] in r.argvs, "create-folder: bw config server not run with --server")
login = [c for c in r.calls if c["argv"][:1] == ["login"]]
check(len(login) == 1 and login[0]["argv"] == ["login", USER_EMAIL, "--passwordenv", "BW_PW", "--raw"]
      and "BW_PW" in login[0]["env"], f"create-folder: login call {login}")
check(not any("BW_PW" in c["env"] for c in r.calls if c["argv"][:1] != ["login"]),
      "create-folder: BW_PW set on a call other than login")
check(r.api["calls"] == [], "create-folder: org API called for a folder")
r.expect_lock_logout()

r = Run("create-folder-exists", "create", account("folder:Agent"),
        dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}]))
r.expect_code(0)
check(not r.cmds("create") and len(r.vault["folders"]) == 1, "create-folder-exists: created a second folder")

# create, collection scope (selfhosted): new collection, invite, Manage for the agent.
r = Run("create-collection", "create", account("collection:c-placeholder", mode="selfhosted",
                                              server="https://vault.example.com"),
        base_vault(), extra=("--agent-email", AGENT_EMAIL, "--org", ORG, "--name", "Agent"))
r.expect_code(0)
check([c[1] for c in r.api["calls"]][:2] == ["/identity/accounts/prelogin", "/identity/connect/token"],
      f"create-collection: API calls {[c[1] for c in r.api['calls']]}")
check(r.api["calls"][:1] and json.loads(r.api["calls"][0][2]) == {"email": USER_EMAIL},
      "create-collection: prelogin body")
check(r.api["token_ok"] is True, "create-collection: token request had the wrong master password hash")
check(r.api["invites"] == [{"emails": [AGENT_EMAIL], "groups": [], "type": 2, "collections": [],
                            "permissions": {}}], f"create-collection: invites {r.api['invites']}")
agent = [m for m in r.vault["members"] if m["email"] == AGENT_EMAIL]
colls = [c for c in r.vault["collections"] if c["name"] == "Agent"]
check(len(colls) == 1, f"create-collection: collections {r.vault['collections']}")
if agent and colls:
    check({"id": agent[0]["id"], "readOnly": False, "hidePasswords": False, "manage": True} in colls[0]["users"],
          f"create-collection: agent access {colls[0].get('users')}")
    check(f"BW_SCOPE=collection:{colls[0]['id']}" in r.account_file.read_text().splitlines(),
          "create-collection: account file BW_SCOPE not updated to the new collection")
    check(oct(r.account_file.stat().st_mode & 0o777) == "0o600", "create-collection: account file mode")
    check("BW_CLIENTID=user.syn" in r.account_file.read_text(), "create-collection: account file lost a key")
check("accept" in r.out.lower() and AGENT_EMAIL in r.out, "create-collection: no steps left for the user")
r.expect_lock_logout()

# create, collection exists and the agent is already confirmed: only the access edit.
v = with_collection(base_vault(), users=[{"id": "m-owner", "readOnly": False, "hidePasswords": False,
                                          "manage": True}])
v["members"].append({"id": "m-agent", "email": AGENT_EMAIL, "status": 2, "type": 2, "organizationId": ORG})
r = Run("create-collection-exists", "create", account("collection:c-agent", mode="selfhosted",
                                                     server="https://vault.example.com"),
        v, extra=("--agent-email", AGENT_EMAIL))
r.expect_code(0)
check(not r.cmds("create") and not r.api["invites"], "create-collection-exists: created or invited again")
users = r.vault["collections"][0]["users"]
check(users == [{"id": "m-owner", "readOnly": False, "hidePasswords": False, "manage": True},
                {"id": "m-agent", "readOnly": False, "hidePasswords": False, "manage": True}],
      f"create-collection-exists: users {users}")

# create, collection scope, the server refuses the password grant: web vault steps, rest goes on.
r = Run("create-collection-refused", "create", account("collection:c-placeholder", mode="selfhosted",
                                                      server="https://vault.example.com"),
        base_vault(), extra=("--agent-email", AGENT_EMAIL, "--org", ORG), refuse=True)
r.expect_code(0)
check(len([c for c in r.vault["collections"] if c["name"] == "Agent"]) == 1,
      "create-collection-refused: collection not created")
check(not r.api["invites"], "create-collection-refused: invited without a token")
check("web vault" in r.out.lower() and "invite" in r.out.lower() and AGENT_EMAIL in r.out,
      f"create-collection-refused: no web vault steps\n{r.out}")
r.expect_lock_logout()

# create, collection scope, no --agent-email: refused before the password prompt.
r = Run("create-collection-no-agent", "create", account("collection:c-agent"), base_vault(), script=[])
r.expect_code(1)
check(not r.calls, "create-collection-no-agent: ran bw")

# seed, folder scope: linkedin.com matches www.linkedin.com and linkedin.com, not notlinkedin.com.
r = Run("seed-folder", "seed", account("folder:Agent"),
        dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}]),
        script=[("Master password", USER_PW), ("Site", "linkedin.com"), ("Move which", "1"), ("Site", "")])
r.expect_code(0)
check("www.linkedin.com" in r.out and "LinkedIn second" in r.out and "i-li-www@example.com" in r.out,
      f"seed-folder: matches not shown\n{r.out}")
check("Not LinkedIn" not in r.out and "LinkedIn app" not in r.out, "seed-folder: wrong items matched")
check(r.item("i-li-www")["folderId"] == "f-agent", "seed-folder: picked item not moved")
check(all(r.item(i)["folderId"] is None for i in ("i-li-bare", "i-notli", "i-gh")), "seed-folder: moved others")
check(r.cmds("edit") == [["edit", "item", "i-li-www"]], f"seed-folder: edits {r.cmds('edit')}")
check(not r.cmds("move") and NOTICE not in r.out, "seed-folder: collection move or notice in folder mode")
r.expect_lock_logout()

# seed, collection scope: the transfer notice once, before the first move.
r = Run("seed-collection", "seed", account("collection:c-agent"), with_collection(base_vault()),
        script=[("Master password", USER_PW), ("Site", "linkedin.com"), ("Move which", "1 2"),
                ("Site", "github.com"), ("Move which", "1"), ("Site", "")])
r.expect_code(0)
check(r.out.count(NOTICE) == 1, f"seed-collection: notice printed {r.out.count(NOTICE)} times")
first_move = r.out.find("Moved ")
check(0 <= r.out.find(NOTICE) < first_move, "seed-collection: notice not before the first move")
check(r.cmds("move") == [["move", "i-li-www", ORG], ["move", "i-li-bare", ORG], ["move", "i-gh", ORG]],
      f"seed-collection: moves {r.cmds('move')}")
for ident in ("i-li-www", "i-li-bare", "i-gh"):
    check(r.item(ident)["organizationId"] == ORG and r.item(ident)["collectionIds"] == ["c-agent"],
          f"seed-collection: {ident} not in the collection")
r.expect_lock_logout()

# grant --site --item: no prompt but the password, prints what moved.
v = with_collection(base_vault())
v["items"].append(item("i-org-loose", "Org loose", "https://www.example.com/login", organizationId=ORG))
r = Run("grant-item", "grant", account("collection:c-agent"), v, extra=("--site", "linkedin.com", "--item",
                                                                        "i-li-bare"))
r.expect_code(0)
check(r.cmds("move") == [["move", "i-li-bare", ORG]], f"grant-item: moves {r.cmds('move')}")
check("Moved" in r.out and "LinkedIn second" in r.out and NOTICE in r.out, f"grant-item: output\n{r.out}")
r.expect_lock_logout()

# grant of an unassigned organization item: edit item-collections, no move, no notice.
r = Run("grant-org-item", "grant", account("collection:c-agent"), v, extra=("--item", "i-org-loose"))
r.expect_code(0)
check(r.cmds("move") == [] and r.cmds("edit") == [["edit", "item-collections", "i-org-loose", "--organizationid",
                                                   ORG]], f"grant-org-item: calls {r.argvs}")
check(r.item("i-org-loose")["collectionIds"] == ["c-agent"], "grant-org-item: not assigned")
check(NOTICE not in r.out, "grant-org-item: transfer notice for an item already in the organization")

# grant --item whose URIs do not match --site: refused, nothing moved.
r = Run("grant-mismatch", "grant", account("collection:c-agent"), with_collection(base_vault()),
        extra=("--site", "linkedin.com", "--item", "i-notli"))
r.expect_code(1)
check(not r.cmds("move", "edit"), "grant-mismatch: moved an item that does not match the site")
r.expect_lock_logout()

# grant --site with a prompt for the pick.
r = Run("grant-site", "grant", account("folder:Agent"), dict(base_vault(), folders=[{"id": "f-agent",
                                                                                     "name": "Agent"}]),
        extra=("--site", "www.linkedin.com"), script=[("Master password", USER_PW), ("Move which", "1")])
r.expect_code(0)
check(r.item("i-li-www")["folderId"] == "f-agent" and "LinkedIn second" not in r.out,
      f"grant-site: wrong match or move\n{r.out}")

# role --slug local: the member edit exactly as spike 2.
v = with_collection(base_vault())
with_collection(v, cid="c-secrets", name="Stack secrets")
v["members"].append({"id": "m-agent", "email": "agent@vault.localhost", "status": 2, "type": 0,
                     "organizationId": ORG})
r = Run("role-local", "role", account("collection:c-agent", mode="local", server="https://vaultwarden.vault.internal",
                                      extra=("BW_SECRETS_ITEM_SCOPE=collection:c-secrets",)),
        v, slug="local")
r.expect_code(0)
check(r.api["edits"] == [(f"/api/organizations/{ORG}/users/m-agent", {
    "type": 2, "groups": [], "permissions": {}, "collections": [
        {"id": "c-agent", "readOnly": False, "hidePasswords": False, "manage": True},
        {"id": "c-secrets", "readOnly": True, "hidePasswords": False, "manage": False}]})],
      f"role-local: edits {r.api['edits']}")
check(not r.cmds("create", "edit", "move"), "role-local: changed the vault")
r.expect_lock_logout()

# role, token refused: web vault steps and exit 1.
r = Run("role-refused", "role", account("collection:c-agent", mode="local",
                                        server="https://vaultwarden.vault.internal"), v, slug="local", refuse=True)
r.expect_code(1)
check("web vault" in r.out.lower() and not r.api["edits"], f"role-refused: output\n{r.out}")
r.expect_lock_logout()

# list-unassigned: organization items with no collection, by name and host.
v = with_collection(base_vault())
v["items"].append(item("i-org-loose", "Org loose", "https://www.example.com/login", organizationId=ORG))
v["items"].append(item("i-org-agent", "Org filed", "https://filed.example.com/", organizationId=ORG,
                       collectionIds=["c-agent"]))
r = Run("list-unassigned", "list-unassigned", account("collection:c-agent"), v)
r.expect_code(0)
check("Org loose" in r.out and "www.example.com" in r.out and "Org filed" not in r.out,
      f"list-unassigned: output\n{r.out}")
check(not r.cmds("create", "edit", "move"), "list-unassigned: changed the vault")
r.expect_lock_logout()

# Three wrong passwords: exit 2, nothing changed, still lock and logout.
r = Run("wrong-password", "seed", account("collection:c-agent"), with_collection(base_vault()),
        script=[("Master password", "syn-wrong-1"), ("Master password", "syn-wrong-2"),
                ("Master password", "syn-wrong-3")])
r.expect_code(2)
check(not r.cmds("create", "edit", "move"), "wrong-password: changed the vault")
check(len(r.cmds("login")) == 3, f"wrong-password: {len(r.cmds('login'))} login attempts")
check(not any(w in r.out for w in ("syn-wrong-1", "syn-wrong-2", "syn-wrong-3")),
      "wrong-password: bw's stderr reached the output")
check(r.vault == r.vault_before, "wrong-password: vault changed")
r.expect_lock_logout()

# A failure after unlock (the folder is missing): exit 1, lock and logout.
r = Run("missing-folder", "seed", account("folder:Agent"), base_vault())
r.expect_code(1)
check("create" in r.out, f"missing-folder: no hint to run create\n{r.out}")
r.expect_lock_logout()

# SIGINT and SIGTERM at the site prompt: lock, logout, temporary dir removed.
for sig in (signal.SIGINT, signal.SIGTERM):
    name = f"signal-{sig.name.lower()}"
    r = Run(name, "seed", account("folder:Agent"), dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}]),
            script=[("Master password", USER_PW), ("Site", sig)])
    check(r.code == 130, f"{name}: exit {r.code}, want 130")
    check(any(a[:1] == ["login"] for a in r.argvs), f"{name}: never logged in")
    r.expect_lock_logout()

# bw config server fails, or leaves bw on another server: exit 1 before the prompt, nothing sent.
for knob in ("refuse_config", "ignore_config"):
    v = with_collection(base_vault())
    v[knob] = "Connection refused" if knob == "refuse_config" else True
    r = Run(f"config-{knob}", "create", account("collection:c-agent", mode="selfhosted",
                                                 server="https://vault.example.com"),
            v, extra=("--agent-email", AGENT_EMAIL), script=[])
    r.expect_code(1)
    check("Master password" not in r.out, f"config-{knob}: asked for the password")
    check(not r.cmds("login") and not r.api["calls"], f"config-{knob}: login or API call after a bad config")
    check("nothing was sent" in r.out, f"config-{knob}: output\n{r.out}")
    r.expect_lock_logout()

# --server other than BW_SERVER (and not loopback): refused before bw runs.
r = Run("server-mismatch", "create", account("folder:Agent"), base_vault(), server="https://other.example.com",
        script=[])
r.expect_code(1)
check(not r.calls and "BW_SERVER" in r.out, f"server-mismatch: calls {r.argvs}\n{r.out}")

# The token endpoint redirects: refused, the form is not re-sent, web vault steps follow.
r = Run("token-redirect", "create", account("collection:c-placeholder", mode="selfhosted",
                                           server="https://vault.example.com"),
        base_vault(), extra=("--agent-email", AGENT_EMAIL, "--org", ORG), redirect=True)
r.expect_code(0)
check(not any(c[1] == "/redirected" for c in r.api["calls"]), "token-redirect: followed the redirect")
check("redirect refused" in r.out and not r.api["invites"], f"token-redirect: output\n{r.out}")
check(r.api.get("token_scope") == "api offline_access", f"token-redirect: token scope {r.api.get('token_scope')!r}")

# grant --site that is not a host: exit 1. grant of an item already in scope: no "marks done" line.
r = Run("grant-not-host", "grant", account("folder:Agent"),
        dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}]), extra=("--site", "not a host"))
r.expect_code(1)
check(not r.cmds("edit", "move") and "next sync" not in r.out, f"grant-not-host: output\n{r.out}")
# A single label would match every login under it (items match subdomains).
for single in ("com", "localhost"):
    r = Run(f"grant-single-{single}", "grant", account("folder:Agent"),
            dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}]), extra=("--site", single))
    r.expect_code(1)
    check(not r.cmds("edit", "move") and "is not a host name" in r.out, f"grant --site {single}: output\n{r.out}")
v = dict(base_vault(), folders=[{"id": "f-agent", "name": "Agent"}])
v["items"][0]["folderId"] = "f-agent"
r = Run("grant-already", "grant", account("folder:Agent"), v, extra=("--item", "i-li-www"))
r.expect_code(0)
check("already in" in r.out and "next sync" not in r.out, f"grant-already: output\n{r.out}")

# local-init: Local mode steps 2-5 against a fake Vaultwarden, re-run from local/state.
try:
    import cryptography  # noqa: F401  (vw_register's registration and organization crypto)
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
    print("skip: local-init (host python3 has no cryptography)")

LOCAL_AGENT = "agent@vault.localhost"
LOCAL_OWNER = "owner@example.com"
LOCAL_ORG = "org-local"
ADMIN_TOKEN = f"syn-admin-{secrets.token_hex(8)}"
AGENT_SECRET = f"syn-apikey-{secrets.token_hex(8)}"
SECRET_VALUES.update({ADMIN_TOKEN, AGENT_SECRET})


def jwt(claims):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


LOCAL_TOKEN = jwt({"sub": "uid-agent", "email": LOCAL_AGENT, "nonce": secrets.token_hex(8)})
SECRET_VALUES.add(LOCAL_TOKEN)


class LocalApi(http.server.BaseHTTPRequestHandler):
    def _reply(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {LOCAL_TOKEN}"

    def _vault(self):
        return json.loads(self.server.st["vault"].read_text())

    def do_POST(self):
        st = self.server.st
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        st["calls"].append(("POST", self.path, body))
        if self.path == "/admin":
            ok = parse_qs(body).get("token") == [ADMIN_TOKEN]
            return self._reply(303 if ok else 401, {}, [("Location", "/admin"), ("Set-Cookie", "VW_ADMIN=c1; Path=/admin")]
                               if ok else [])
        if self.path == "/admin/invite":
            return self._reply(200 if self.headers.get("Cookie") == "VW_ADMIN=c1" else 401, {})
        if self.path == "/identity/accounts/register/send-verification-email":
            return self._reply(200, "verify-token")
        if self.path == "/identity/accounts/register/finish":
            if st.get("refuse_register"):
                return self._reply(400, {"message": "changed API"})
            payload = json.loads(body)
            st["registered"][payload["email"]] = payload["masterPasswordHash"]
            vault = self._vault()
            vault["user_email"] = payload["email"]
            vault["user_password"] = (st["secrets"] / "master_password").read_text().strip()
            st["vault"].write_text(json.dumps(vault))
            return self._reply(200, {})
        if self.path == "/identity/accounts/prelogin":
            return self._reply(200, {"kdf": 0, "kdfIterations": 600000})
        if self.path == "/identity/connect/token":
            if st.get("limit_once"):
                st["limit_once"] = False
                return self._reply(429, {"message": "Too many requests"})
            form = {k: v[0] for k, v in parse_qs(body).items()}
            if st["registered"].get(form.get("username")) != form.get("password"):
                return self._reply(400, {"error": "invalid_grant"})
            return self._reply(200, {"access_token": LOCAL_TOKEN, "token_type": "Bearer"})
        if not self._authed():
            return self._reply(401, {})
        if self.path == "/api/accounts/api-key":
            return self._reply(200, {"apiKey": AGENT_SECRET})
        if self.path == "/api/organizations":
            st["orgs"].append({"id": LOCAL_ORG, "name": "Assistant"})
            vault = self._vault()
            if not any(c.get("id") == "c-agent" for c in vault["collections"]):
                vault["collections"].append({"id": "c-agent", "organizationId": LOCAL_ORG, "name": "Agent"})
            st["vault"].write_text(json.dumps(vault))
            return self._reply(200, {"id": LOCAL_ORG})
        if self.path == f"/api/organizations/{LOCAL_ORG}/users/invite":
            payload = json.loads(body)
            st["members"].extend({"id": "m-owner", "email": e, "status": 0, "type": payload["type"]}
                                 for e in payload["emails"])
            return self._reply(200, {})
        self._reply(404, {})

    def do_GET(self):
        st = self.server.st
        st["calls"].append(("GET", self.path, ""))
        if not self._authed():
            return self._reply(401, {})
        if self.path == "/api/accounts/profile":
            return self._reply(200, {"organizations": [] if st.get("hide_orgs") else st["orgs"]})
        if self.path == f"/api/organizations/{LOCAL_ORG}/collections":
            return self._reply(200, {"data": [{"id": "c-agent"}]})
        if self.path == "/api/users/uid-agent/public-key":
            return self._reply(200, {"userId": "uid-agent", "publicKey": base64.b64encode(st["pub"]).decode()})
        if self.path == f"/api/organizations/{LOCAL_ORG}/users":
            st["polls"] += 1
            confirmed = {c["id"] for c in self._vault().get("confirmed", [])}
            for m in st["members"]:
                if m["id"] in confirmed:
                    m["status"] = 2
                elif st["accept_after"] is not None and st["polls"] >= st["accept_after"]:
                    m["status"] = max(m["status"], 1)
            return self._reply(200, {"data": [{"id": "m-agent", "email": LOCAL_AGENT, "status": 2, "type": 0}]
                                     + st["members"]})
        self._reply(404, {})

    def log_message(self, *args):
        pass


def local_run(st, d, extra=(), owner=LOCAL_OWNER):
    wrapper = d / "bw"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(d), "PYTHONDONTWRITEBYTECODE": "1",
           "LANG": "C.UTF-8", "BITWARDEN_ACCOUNTS_DIR": str(d / "accounts"), "BITWARDEN_SECRETS_DIR": str(d / "secrets"),
           "BITWARDEN_LOCAL_DIR": str(d / "local"), "BITWARDEN_BW": str(wrapper),
           "BITWARDEN_LOCAL_POLL_SECONDS": "0.1", "BITWARDEN_RATE_LIMIT_WAIT": "0.2"}
    argv = ["local-init", "--owner", owner, "--server", local_base, "--signup-url", "http://localhost:8222",
            "--wait", "1", *extra]
    code, text = pty_run(argv, env, [])
    log_text = (d / "bw.log").read_text() if (d / "bw.log").exists() else ""
    secret_list = SECRET_VALUES | {(d / "secrets/local/master_password").read_text().strip()
                                   if (d / "secrets/local/master_password").exists() else USER_PW}
    for value in secret_list:
        if value in text or value in log_text:
            fail("local-init: a password, token, API key or admin token appears in output or bw argv")
            break
    return code, text, [json.loads(line)["argv"] for line in log_text.splitlines()]


def local_state(d):
    path = d / "local/state"
    rows = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line) if path.exists() else {}
    return rows


def count(st, path):
    return sum(1 for c in st["calls"] if c[1] == path)


if HAVE_CRYPTO:
    local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalApi)
    local.daemon_threads = True
    threading.Thread(target=local.serve_forever, daemon=True).start()
    local_base = f"http://127.0.0.1:{local.server_address[1]}"
    d = TMP / "local-init"
    for sub in ("accounts", "secrets/local", "local"):
        (d / sub).mkdir(parents=True)
    (d / "local/admin_token").write_text(ADMIN_TOKEN + "\n")
    (d / "vault.json").write_text(json.dumps({"folders": [], "collections": [], "items": [], "members": []}))
    (d / "bw").write_text(f"#!/bin/sh\nexport FAKE_BW_VAULT='{d / 'vault.json'}'\nexport FAKE_BW_LOG='{d / 'bw.log'}'\n"
                          f"exec '{sys.executable}' '{FAKE_BW}' \"$@\"\n")
    (d / "bw").chmod(0o755)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    local.st = {"calls": [], "registered": {}, "members": [], "orgs": [], "polls": 0, "accept_after": None,
                "vault": d / "vault.json", "secrets": d / "secrets/local", "pub": pub, "refuse_register": True}

    # Registration refused and no token: exit 1, the sign-up fallback, no step recorded.
    code, out, argvs = local_run(local.st, d)
    check(code == 1, f"local-init refused registration: exit {code}\n{out}")
    check(LOCAL_AGENT in out and "http://localhost:8222" in out and "master_password" in out,
          f"local-init refused registration: no sign-up fallback\n{out}")
    check(not local_state(d).get("STEP"), f"local-init refused registration: state {local_state(d)}")
    pw_file = d / "secrets/local/master_password"
    check(pw_file.exists() and oct(pw_file.stat().st_mode & 0o777) == "0o600", "local-init: master_password mode")
    first_pw = pw_file.read_text() if pw_file.exists() else None
    local.st["calls"].clear()  # the counts below are for the runs that follow

    # Fresh run; one 429 on the token grant; the owner never registers within --wait.
    local.st.update(refuse_register=False, limit_once=True)
    code, out, argvs = local_run(local.st, d)
    check(code == 4, f"local-init without the owner: exit {code}, want 4\n{out}")
    check(pw_file.read_text() == first_pw, "local-init generated a second agent password")
    state = local_state(d)
    check(state.get("STEP") == "invited" and state.get("ORG_ID") == LOCAL_ORG and state.get("OWNER_EMAIL") == LOCAL_OWNER
          and state.get("AGENT_COLLECTION") == "c-agent" and state.get("STACK_COLLECTION"),
          f"local-init state after the invite: {state}")
    check(oct((d / "local/state").stat().st_mode & 0o777) == "0o600", "local-init: state file mode")
    check(count(local.st, "/identity/connect/token") == 2, "local-init: token grants (one 429, one retry)")
    check("rate limit" in out.lower(), f"local-init: no note about the 429\n{out}")
    check(count(local.st, "/admin/invite") == 1 and count(local.st, "/identity/accounts/register/finish") == 1,
          "local-init: admin invite or registration count")
    check(count(local.st, "/api/organizations") == 1, "local-init: organization created more than once")
    check([m["email"] for m in local.st["members"]] == [LOCAL_OWNER] and local.st["members"][0]["type"] == 0,
          f"local-init: owner invite {local.st['members']}")
    check(sum(1 for a in argvs if a[:1] == ["login"]) == 1, f"local-init: bw logins {argvs}")
    stack = [c for c in json.loads((d / "vault.json").read_text())["collections"] if c.get("name") == "Stack secrets"]
    check(len(stack) == 1 and stack[0]["id"] == state.get("STACK_COLLECTION"), f"local-init: Stack secrets {stack}")
    secret_file = d / "secrets/local/client_secret"
    check(secret_file.read_text().strip() == AGENT_SECRET and oct(secret_file.stat().st_mode & 0o777) == "0o600",
          "local-init: client_secret content or mode")
    acct = d / "accounts/local.env"
    lines = acct.read_text().splitlines() if acct.exists() else []
    for want in ("BW_MODE=local", "BW_SERVER=https://vaultwarden.vault.internal", "BW_SCOPE=collection:c-agent",
                 "BW_UNLOCK=stored", "BW_CLIENTID=user.uid-agent",
                 f"BW_SECRETS_ITEM_SCOPE=collection:{state.get('STACK_COLLECTION')}"):
        check(want in lines, f"local-init: local.env lacks {want}: {lines}")
    check(acct.exists() and oct(acct.stat().st_mode & 0o777) == "0o600", "local-init: local.env mode")
    check(LOCAL_OWNER in out and "http://localhost:8222" in out and "again" in out,
          f"local-init: no sign-up address or re-run hint\n{out}")
    check(argvs[-2:] == [["lock"], ["logout"]], f"local-init: bw not locked and logged out: {argvs[-2:]}")

    # A different owner than recorded: refused before any call.
    calls_before = len(local.st["calls"])
    code, out, _ = local_run(local.st, d, owner="other@example.com")
    check(code == 1 and LOCAL_OWNER in out and len(local.st["calls"]) == calls_before,
          f"local-init other owner: exit {code}\n{out}")

    # Re-run: the owner registers after two polls; confirmed with bw, nothing created twice.
    local.st["accept_after"] = local.st["polls"] + 2
    code, out, argvs = local_run(local.st, d)
    check(code == 0, f"local-init resume: exit {code}\n{out}")
    check(local_state(d).get("STEP") == "confirmed", f"local-init resume: state {local_state(d)}")
    check(["confirm", "org-member", "m-owner", "--organizationid", LOCAL_ORG] in argvs,
          f"local-init resume: no bw confirm in {argvs}")
    check(count(local.st, "/identity/accounts/register/finish") == 1 and count(local.st, "/api/organizations") == 1
          and len(local.st["members"]) == 1, "local-init resume: registered, created or invited again")
    check(count(local.st, "/identity/connect/token") == 3, "local-init resume: more than one token grant")

    # Recorded confirmed: nothing to do and no calls.
    calls_before = len(local.st["calls"])
    code, out, _ = local_run(local.st, d)
    check(code == 0 and len(local.st["calls"]) == calls_before, f"local-init done: exit {code}, calls made\n{out}")

    # Resume before the organization was recorded: the one that exists is reused, not made twice.
    state_path = d / "local/state"
    stack_id = local_state(d).get("STACK_COLLECTION")
    state_path.write_text("STEP=api_key\nCLIENT_ID=user.uid-agent\n")
    code, out, argvs = local_run(local.st, d)
    check(code == 0, f"local-init resume before the organization: exit {code}\n{out}")
    check(count(local.st, "/api/organizations") == 1 and "Reusing" in out,
          f"local-init: organization created again instead of reused\n{out}")
    check(not any(c[1] == "/api/users/uid-agent/public-key" for c in local.st["calls"]),
          "local-init: fetched the public key while reusing the organization")
    check(local_state(d).get("AGENT_COLLECTION") == "c-agent" and local_state(d).get("ORG_ID") == LOCAL_ORG,
          f"local-init reuse: state {local_state(d)}")

    # A local.env naming other ids than the state is refused; --rotate writes it again.
    acct = d / "accounts/local.env"
    state_path.write_text(f"STEP=stack\nCLIENT_ID=user.uid-agent\nORG_ID={LOCAL_ORG}\nAGENT_COLLECTION=c-agent\n"
                          f"STACK_COLLECTION={stack_id}\nOWNER_EMAIL={LOCAL_OWNER}\n")
    acct.write_text(acct.read_text().replace("collection:c-agent", "collection:c-other"))
    code, out, _ = local_run(local.st, d)
    check(code == 1 and "other ids" in out, f"local-init: a local.env with other ids was not refused\n{out}")
    check("BW_SCOPE=collection:c-other" in acct.read_text(), "local-init: the refused local.env was changed")
    code, out, _ = local_run(local.st, d, extra=("--rotate",))
    check(code == 0 and "BW_SCOPE=collection:c-agent" in acct.read_text().splitlines(),
          f"local-init --rotate did not rewrite local.env: exit {code}\n{out}")

    # No organization on the account: created, with the public key from the server.
    local.st["hide_orgs"] = True
    state_path.write_text("STEP=api_key\nCLIENT_ID=user.uid-agent\n")
    code, out, argvs = local_run(local.st, d)
    check(code == 0, f"local-init resume, no organization: exit {code}\n{out}")
    check(any(c[1] == "/api/users/uid-agent/public-key" for c in local.st["calls"]),
          "local-init: no public key fetch when creating the organization on resume")
    check(count(local.st, "/api/organizations") == 2, "local-init: organization not created on resume")
    local.shutdown()

# Unit checks: IP literals match only exactly; prelogin KDF values.
sys.path.insert(0, str(SETUP.parent))
import importlib.util
spec = importlib.util.spec_from_file_location("bw_setup", SETUP)
mod = importlib.util.module_from_spec(spec)
sys.modules["bw_setup"] = mod
spec.loader.exec_module(mod)
import tempfile as _tf
with _tf.TemporaryDirectory() as ld:
    ld = Path(ld)
    (ld / "state").write_text("STEP=confirmed\nORG_ID=org-1\n")
    mod.mark_role_done(ld)
    rows = (ld / "state").read_text().splitlines()
    check("STEP=role" in rows and "ORG_ID=org-1" in rows, f"mark_role_done: state {rows}")
    check(oct((ld / "state").stat().st_mode & 0o777) == "0o600", "mark_role_done: state mode")
    (ld / "state").write_text("STEP=invited\n")
    mod.mark_role_done(ld)
    check((ld / "state").read_text().splitlines() == ["STEP=invited"], "mark_role_done moved STEP forward early")
ip_items = [{"uris": [{"uri": "https://10.0.0.1/", "match": None}]},
            {"uris": [{"uri": "https://192.168.0.1/", "match": None}]}]
check(len(mod.match_items(ip_items, "10.0.0.1")) == 1, "ip: exact IP did not match")
check(mod.match_items(ip_items, "0.0.1") == [] and mod.match_items(ip_items, "168.0.1") == [],
      "ip: subdomain matching applied to an IP address")
import vw_register
for pre, want in (({"kdf": 1, "kdfIterations": 3}, "Argon2id"), ({"kdf": 7, "kdfIterations": 3}, "unknown KDF"),
                  ({"kdfIterations": 3}, "unknown KDF")):
    api.st = {"calls": [], "invites": [], "edits": [], "refuse": False, "prelogin": pre}
    try:
        vw_register.token(BASE, USER_EMAIL, USER_PW)
        fail(f"kdf {pre}: token returned")
    except vw_register.TokenError as exc:
        check(want in str(exc), f"kdf {pre}: {exc}")
    check(not any(c[1] == "/identity/connect/token" for c in api.st["calls"]), f"kdf {pre}: token request sent")

api.shutdown()
if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ok")
PY
