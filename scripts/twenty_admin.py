#!/usr/bin/env python3
"""Set up a local Twenty workspace for Hermes: owner account, workspace, two
roles and two API keys, stored as an instance of the Twenty add-on
(<data root>/addons/twenty/instances/<slug>.json, keys in secrets/).

Called by scripts/crm.sh (make crm-init). Every value comes from the
environment, so a non-interactive caller can run it too. init prints one line
per step and never a key, token or password. login-link prints only a sign-in
URL on stdout, which carries a login token. list prints one line per stored
workspace for crm.sh, without keys. See docs/crm.md.

Environment:
  TWENTY_BASE_URL         where to reach the server (http://127.0.0.1:3000 from the host,
                          http://twenty-server:3000 on the crm network)
  TWENTY_USERNAME         owner account email
  TWENTY_PASSWORD         owner account password, or
  TWENTY_PASSWORD_FILE    a file holding it; generated there (mode 600) when missing
  TWENTY_MULTIWORKSPACE   1 allows more than one local workspace
  ASSISTANT_DATA_ROOT     the data root that holds addons/twenty/ (required)
  ADDONS_DIR              the add-on manifests (default: addons/ next to scripts/)
  TWENTY_DATA_DIR         where .crm-init-done is written
  TWENTY_GRANT_ADMIN_CMD  executable run as `<cmd> grant-admin <email>` on every run
                          (the grant is idempotent). Unset, the caller grants it
                          between an --owner-only run and a full one, as crm.sh
                          does when this runs in a container
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# addon_store lives in scripts/lib. Under -I the script's own directory isn't
# on sys.path, so it's added from this file's location (the repo is mounted at
# its host path when this runs in the Hermes image).
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS / "lib"))
import addon_store  # noqa: E402
from addon_manifest import truthy  # noqa: E402

LOCAL_URL = "http://twenty-server:3000"
KEY_EXPIRY = "2126-01-01T00:00:00Z"
SLUG = addon_store.SLUG_RE
KEYS = ("key_ro", "key_rw")
ROLES = {
    "ro": {
        "label": "Hermes read-only",
        "description": "Hermes on unattended runs: reads records, changes nothing.",
        "canReadAllObjectRecords": True,
        "canUpdateAllObjectRecords": False,
        "canSoftDeleteAllObjectRecords": False,
    },
    "rw": {
        "label": "Hermes read-write",
        "description": "Hermes in chats: reads, edits and soft-deletes records.",
        "canReadAllObjectRecords": True,
        "canUpdateAllObjectRecords": True,
        "canSoftDeleteAllObjectRecords": True,
    },
}
ROLE_COMMON = {
    "canUpdateAllSettings": False,
    "canAccessAllTools": False,
    "canDestroyAllObjectRecords": False,
    "canBeAssignedToUsers": False,
    "canBeAssignedToAgents": False,
    "canBeAssignedToApiKeys": True,
}


class TwentyError(Exception):
    """A GraphQL or transport error. The message never carries a secret."""


class Unreachable(TwentyError):
    """Twenty didn't answer, or answered with something that isn't GraphQL."""


class UsageError(Exception):
    """Bad flags or environment for a command; the caller fixes the call."""


class KeyFailure(Exception):
    """A key command's failure: a code for Boundary and a message. No secret."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def step(msg: str) -> None:
    print(f"[crm] {msg}", flush=True)


class Twenty:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def gql(self, query: str, variables: dict | None = None, token: str | None = None) -> dict:
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(f"{self.base_url}/metadata", body, headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise Unreachable(f"can't reach Twenty at {self.base_url}: {exc}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise Unreachable("Twenty answered with something other than JSON") from None
        if data.get("errors"):
            raise TwentyError("; ".join(str(e.get("message", "error")) for e in data["errors"]))
        if not isinstance(data.get("data"), dict):
            raise TwentyError("Twenty answered with no data")
        return data["data"]


def _token(payload: dict | None, *path: str) -> str:
    for key in path:
        if not isinstance(payload, dict) or key not in payload:
            raise TwentyError(f"unexpected answer from Twenty (no {'.'.join(path)})")
        payload = payload[key]
    if not isinstance(payload, str) or not payload:
        raise TwentyError(f"unexpected answer from Twenty (empty {'.'.join(path)})")
    return payload


TOKENS = "tokens { accessOrWorkspaceAgnosticToken { token } }"


def sign_up(tw: Twenty, email: str, password: str) -> str:
    d = tw.gql(f"mutation($e: String!, $p: String!) {{ signUp(email: $e, password: $p) {{ {TOKENS} }} }}",
               {"e": email, "p": password})
    return _token(d, "signUp", "tokens", "accessOrWorkspaceAgnosticToken", "token")


def sign_in(tw: Twenty, email: str, password: str) -> tuple[str, list[dict]]:
    """Workspace-agnostic token and the workspaces this account can enter."""
    d = tw.gql(
        "mutation($e: String!, $p: String!) { signIn(email: $e, password: $p) { "
        f"{TOKENS} availableWorkspaces {{ availableWorkspacesForSignIn {{ id loginToken }} }} }} }}",
        {"e": email, "p": password},
    )
    token = _token(d, "signIn", "tokens", "accessOrWorkspaceAgnosticToken", "token")
    avail = ((d["signIn"].get("availableWorkspaces") or {}).get("availableWorkspacesForSignIn")) or []
    return token, avail


def _access_from_login(tw: Twenty, login_token: str, origin: str) -> str:
    d = tw.gql(
        "mutation($t: String!, $o: String!) { getAuthTokensFromLoginToken(loginToken: $t, origin: $o) "
        f"{{ {TOKENS} }} }}",
        {"t": login_token, "o": origin},
    )
    return _token(d, "getAuthTokensFromLoginToken", "tokens", "accessOrWorkspaceAgnosticToken", "token")


def create_workspace(tw: Twenty, agnostic: str, display_name: str, subdomain: str | None) -> tuple[str, str, str]:
    """Create and activate a workspace. Returns (workspace id, access token, URL)."""
    ws_input: dict = {"displayName": display_name}
    if subdomain:
        ws_input["subdomain"] = subdomain
    d = tw.gql(
        "mutation($i: SignUpInNewWorkspaceInput) { signUpInNewWorkspace(input: $i) { "
        "loginToken { token } workspace { id workspaceUrls { subdomainUrl } } } }",
        {"i": ws_input}, token=agnostic,
    )
    created = d.get("signUpInNewWorkspace") or {}
    login = _token(created, "loginToken", "token")
    ws = created.get("workspace") or {}
    ws_id = ws.get("id")
    url = (ws.get("workspaceUrls") or {}).get("subdomainUrl")
    if not ws_id or not url:
        raise TwentyError("unexpected answer from Twenty (no workspace id or URL)")
    access = _access_from_login(tw, login, url.rstrip("/"))
    tw.gql("mutation($d: ActivateWorkspaceInput!) { activateWorkspace(data: $d) { id } }",
           {"d": {"displayName": display_name}}, token=access)
    return ws_id, access, url


def workspace_access_token(tw: Twenty, available: list[dict], workspace_id: str, origin: str) -> str:
    for ws in available:
        if ws.get("id") == workspace_id and ws.get("loginToken"):
            return _access_from_login(tw, ws["loginToken"], origin)
    raise TwentyError(f"the owner account can't enter workspace {workspace_id}")


def ensure_roles(tw: Twenty, access: str) -> dict[str, str]:
    """The two Hermes roles, found by label or created. Returns {"ro": id, "rw": id}."""
    d = tw.gql("query { getRoles { id label } }", token=access)
    roles = d.get("getRoles")
    if not isinstance(roles, list):
        raise TwentyError("unexpected answer from Twenty (getRoles)")
    by_label = {r.get("label"): r.get("id") for r in roles if isinstance(r, dict)}
    ids: dict[str, str] = {}
    for kind, spec in ROLES.items():
        if by_label.get(spec["label"]):
            ids[kind] = by_label[spec["label"]]
            continue
        d = tw.gql("mutation($i: CreateRoleInput!) { createOneRole(createRoleInput: $i) { id label } }",
                   {"i": {**ROLE_COMMON, **spec}}, token=access)
        ids[kind] = _token(d, "createOneRole", "id")
    return ids


def deny_writes_per_object(tw: Twenty, access: str, role_id: str) -> int:
    """Give the read-only role an explicit read-only permission on every object.

    Twenty lets a role without canUpdateAllObjectRecords still create and
    delete junction and system records (noteTarget, attachment, and from v2.60
    the isUIReadOnly agentChatThreadTarget). A per-object permission closes
    that. Twenty refuses a grant of any action an object lists in
    disabledObjectPermissionActions, so read is granted only where it isn't
    disabled. Each call replaces the role's whole set, so everything goes in
    one call. New objects need a re-run (make crm-init ROTATE=1).
    """
    d = tw.gql("query { objects(paging: { first: 1000 }) { edges { node { "
               "id isActive disabledObjectPermissionActions } } } }", token=access)
    edges = ((d.get("objects") or {}).get("edges")) or []
    perms = []
    for e in edges:
        node = e.get("node") if isinstance(e, dict) else None
        if not isinstance(node, dict) or not node.get("isActive"):
            continue
        disabled = node.get("disabledObjectPermissionActions") or []
        perms.append({"objectMetadataId": node["id"],
                      "canReadObjectRecords": "canReadObjectRecords" not in disabled,
                      "canUpdateObjectRecords": False, "canSoftDeleteObjectRecords": False,
                      "canDestroyObjectRecords": False})
    if not perms:
        raise TwentyError("unexpected answer from Twenty (no objects to restrict)")
    tw.gql("mutation($i: UpsertObjectPermissionsInput!) { "
           "upsertObjectPermissions(upsertObjectPermissionsInput: $i) { objectMetadataId } }",
           {"i": {"roleId": role_id, "objectPermissions": perms}}, token=access)
    return len(perms)


def revoke_keys_named(tw: Twenty, access: str, name: str) -> int:
    d = tw.gql("query { apiKeys { id name revokedAt } }", token=access)
    count = 0
    for key in d.get("apiKeys") or []:
        if key.get("name") == name and not key.get("revokedAt"):
            tw.gql("mutation($i: RevokeApiKeyInput!) { revokeApiKey(input: $i) { id } }",
                   {"i": {"id": key["id"]}}, token=access)
            count += 1
    return count


def mint_key(tw: Twenty, access: str, name: str, role_id: str) -> tuple[str, str]:
    """Create an API key and its token. Returns (key id, token)."""
    d = tw.gql("mutation($i: CreateApiKeyInput!) { createApiKey(input: $i) { id } }",
               {"i": {"name": name, "expiresAt": KEY_EXPIRY, "roleId": role_id}}, token=access)
    key_id = _token(d, "createApiKey", "id")
    try:
        d = tw.gql("mutation($k: UUID!, $e: String!) { generateApiKeyToken(apiKeyId: $k, expiresAt: $e) { token } }",
                   {"k": key_id, "e": KEY_EXPIRY}, token=access)
        return key_id, _token(d, "generateApiKeyToken", "token")
    except TwentyError as exc:
        exc.key_id = key_id  # the key exists without a token; key create revokes it
        raise


class Workspaces:
    """Twenty workspaces in the add-on instance store, read and written with
    addon_store. Values use the manifest's instances.settings names; get()
    adds key_ro and key_rw from secrets/ when they are set."""

    def __init__(self, env) -> None:
        root = env.get("ASSISTANT_DATA_ROOT", "")
        if not root:
            raise TwentyError("ASSISTANT_DATA_ROOT is not set")
        addons = Path(env.get("ADDONS_DIR") or SCRIPTS.parent / "addons")
        try:
            self.manifest = json.loads((addons / "twenty" / "addon.json").read_text())
        except (OSError, ValueError):
            raise TwentyError(f"can't read {addons / 'twenty' / 'addon.json'}") from None
        self.data_root = Path(root)
        self.dir = addon_store.addon_dir(self.data_root, self.manifest)

    def slugs(self) -> list[str]:
        return addon_store.slugs(self.dir)

    def get(self, slug: str) -> dict | None:
        if slug not in self.slugs():
            return None
        values = addon_store._instance_values(self.dir, slug)
        for name in KEYS:
            path = self.dir / "secrets" / f"{slug}.{name}"
            try:
                text = path.read_text()
            except OSError:
                continue
            if text:
                values[name] = text
        return values

    def local(self) -> list[str]:
        """Slugs of instances on the local instance."""
        return [s for s in self.slugs() if (self.get(s) or {}).get("local") is True]

    def write(self, slug: str, values: dict) -> None:
        """Merge values into the instance; keys go to secrets/ (0600)."""
        addon_store.apply_instance(self.manifest, self.data_root, slug, values)

    def where(self, slug: str) -> Path:
        return self.dir / "instances" / f"{slug}.json"


def owner_password(env) -> tuple[str, str]:
    """The owner password and where it is kept, for the summary line.

    With TWENTY_PASSWORD_FILE set, the password lives only in that file: read
    when present, generated (8 to 50 characters, Twenty's rule) when not.
    """
    value = env.get("TWENTY_PASSWORD", "")
    file_name = env.get("TWENTY_PASSWORD_FILE", "")
    if value and file_name:
        raise TwentyError("set TWENTY_PASSWORD or TWENTY_PASSWORD_FILE, not both")
    if not file_name:
        return value, "TWENTY_PASSWORD in .env"
    path = Path(file_name).expanduser()
    if path.exists():
        value = path.read_text().strip()
        if not value:
            raise TwentyError(f"TWENTY_PASSWORD_FILE ({path}) is empty")
    else:
        value = secrets.token_hex(16) + "Aa1!"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
        step(f"owner password: generated in {path}")
    return value, str(path)


def cmd_init(args: argparse.Namespace) -> int:
    slug = args.slug
    if not SLUG.match(slug):
        raise TwentyError(f"slug '{slug}' must use lowercase letters, digits and hyphens")
    env = os.environ
    email = env.get("TWENTY_USERNAME", "")
    if env.get("TWENTY_PASSWORD") and env.get("TWENTY_PASSWORD_FILE"):
        raise TwentyError("set TWENTY_PASSWORD or TWENTY_PASSWORD_FILE, not both")
    if not email or not (env.get("TWENTY_PASSWORD") or env.get("TWENTY_PASSWORD_FILE")):
        raise TwentyError("TWENTY_USERNAME and TWENTY_PASSWORD (or TWENTY_PASSWORD_FILE) must be set")
    store = Workspaces(env)
    multi = truthy(env.get("TWENTY_MULTIWORKSPACE"))

    stored = store.get(slug)
    existing = stored or {}
    complete = bool(existing.get("key_rw") and existing.get("key_ro"))
    if complete and not args.rotate:
        step(f"{slug}: workspace exists, nothing to do (--rotate re-mints its keys)")
        return 0
    others = [s for s in store.local() if s != slug]
    if stored is None and others and not multi:
        raise TwentyError(
            f"the local instance already has workspace '{others[0]}'. Single-workspace mode allows one; "
            "set TWENTY_MULTIWORKSPACE=1 with make crm-mode MULTI=1 first"
        )

    # Resolved only once there is work to do, so a no-op run never creates the file.
    password, password_where = owner_password(env)
    if not 8 <= len(password) <= 50:
        # Twenty's own rule (PASSWORD_REGEX); it reports both cases as "Password too weak".
        raise TwentyError("TWENTY_PASSWORD must be 8 to 50 characters")
    tw = Twenty(env.get("TWENTY_BASE_URL", "http://127.0.0.1:3000"))
    step("owner account: running")
    created = False
    available: list[dict] = []
    try:
        agnostic = sign_up(tw, email, password)
        created = True
    except TwentyError as exc:
        if not re.search(r"already|exist", str(exc), re.I):
            raise
        agnostic, available = sign_in(tw, email, password)
    step(f"owner account: done ({'created' if created else 'signed in'} {email})")
    if args.owner_only:
        # crm.sh grants server admin next, then runs init again, which signs in.
        return 0

    if env.get("TWENTY_GRANT_ADMIN_CMD"):
        # The owner creates further workspaces, which Twenty limits to server
        # admins. Idempotent, so it runs every time and repairs a failed grant.
        step("server admin: running")
        subprocess.run([env["TWENTY_GRANT_ADMIN_CMD"], "grant-admin", email], check=True,
                       stdout=subprocess.DEVNULL)
        step("server admin: done")

    if stored is not None:
        # A complete workspace being rotated, or a partial one left by a failed run.
        ws_id = existing.get("workspace_id", "")
        ws_url = existing.get("workspace_url", "")
        if not ws_id or not ws_url:
            raise TwentyError(f"{store.where(slug)} has no workspace_id or workspace_url; "
                              f"remove it (crm.sh remove --slug {slug}) and re-run")
        if not complete:
            step(f"{slug}: resuming a setup that stopped before its keys were written")
        if not available:
            agnostic, available = sign_in(tw, email, password)
        step(f"{slug}: signing in to the workspace")
        access = workspace_access_token(tw, available, ws_id, ws_url.rstrip("/"))
    else:
        step(f"{slug}: workspace: running")
        ws_id, access, ws_url = create_workspace(
            tw, agnostic, args.display_name or slug, slug if multi else None)
        # Recorded at once, so a failure below resumes this workspace instead
        # of creating another. Without keys, the bootstrap skips the instance.
        # access is set only here, so a later crm.sh access choice survives
        # --rotate.
        store.write(slug, {
            "url": LOCAL_URL,
            "local": True,
            "access": "write",
            "workspace_url": ws_url,
            "workspace_id": ws_id,
        })
        step(f"{slug}: workspace: done ({ws_url})")

    step(f"{slug}: roles: running")
    roles = ensure_roles(tw, access)
    restricted = deny_writes_per_object(tw, access, roles["ro"])
    step(f"{slug}: roles: done (read-only on {restricted} objects)")
    step(f"{slug}: keys: running")
    keys = {}
    for kind, role_id in (("ro", roles["ro"]), ("rw", roles["rw"])):
        name = ROLES[kind]["label"]
        if existing:
            # Rotation, or keys an interrupted run minted but never wrote down.
            revoke_keys_named(tw, access, name)
        _, keys[kind] = mint_key(tw, access, name, role_id)
    step(f"{slug}: keys: done")

    store.write(slug, {
        "url": LOCAL_URL,
        "local": True,
        "workspace_url": ws_url,
        "workspace_id": ws_id,
        "key_ro": keys["ro"],
        "key_rw": keys["rw"],
    })
    step(f"{slug}: wrote {store.where(slug)}")
    data_dir = Path(env.get("TWENTY_DATA_DIR", "data/twenty"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".crm-init-done").write_text(f"{slug}\n")
    step(f"done: {ws_url} — sign in as {email}; the password is {password_where}")
    return 0


def cmd_login_link(args: argparse.Namespace) -> int:
    """Print a URL that signs a browser in to a local workspace as the owner.

    The URL is the only thing on stdout; progress and errors go to stderr. It
    carries a login token that Twenty accepts until LOGIN_TOKEN_EXPIRES_IN
    (15 minutes by default) runs out.
    """
    slug = args.slug
    if not SLUG.match(slug):
        raise TwentyError(f"slug '{slug}' must use lowercase letters, digits and hyphens")
    env = os.environ
    store = Workspaces(env)
    values = store.get(slug)
    if values is None:
        raise TwentyError(f"no workspace '{slug}' — make crm-init")
    if values.get("local") is not True:
        raise TwentyError(f"'{slug}' isn't on the local instance; sign in on its own Twenty")
    ws_id = values.get("workspace_id", "")
    ws_url = values.get("workspace_url", "")
    if not ws_id or not ws_url:
        raise TwentyError(f"{store.where(slug)} has no workspace_id or workspace_url; run make crm-init")
    email = env.get("TWENTY_USERNAME", "")
    password_file = env.get("TWENTY_PASSWORD_FILE", "")
    if password_file and not Path(password_file).expanduser().exists():
        raise TwentyError(f"TWENTY_PASSWORD_FILE ({password_file}) doesn't exist; run make crm-init")
    if not email or not (env.get("TWENTY_PASSWORD") or password_file):
        raise TwentyError("TWENTY_USERNAME and TWENTY_PASSWORD (or TWENTY_PASSWORD_FILE) must be set")
    password, _ = owner_password(env)
    tw = Twenty(env.get("TWENTY_BASE_URL", "http://127.0.0.1:3000"))
    print(f"[crm] {slug}: signing in as {email}", file=sys.stderr, flush=True)
    _, available = sign_in(tw, email, password)
    for ws in available:
        if ws.get("id") == ws_id and ws.get("loginToken"):
            if not ws_url.endswith("/"):
                ws_url += "/"
            print(f"{ws_url}verify?loginToken={ws['loginToken']}", flush=True)
            return 0
    raise TwentyError(f"the owner account can't enter workspace {ws_id}")


def owner_session(env, slug: str) -> tuple[Twenty, str]:
    """Sign in as the owner of workspace SLUG. Returns (client, workspace access token).

    main is the local instance: owner login and base URL as crm init uses them
    (TWENTY_USERNAME, TWENTY_PASSWORD or TWENTY_PASSWORD_FILE, TWENTY_BASE_URL).
    Any other slug uses the instance's stored url, with the owner login from
    TWENTY_OWNER_EMAIL and TWENTY_OWNER_PASSWORD, which are only read.
    Raises UsageError for a bad slug or missing settings, KeyFailure for a
    sign-in Twenty refuses or can't be reached for.
    """
    if not SLUG.match(slug):
        raise UsageError(f"slug '{slug}' must use lowercase letters, digits and hyphens")
    values = Workspaces(env).get(slug)
    if values is None:
        raise UsageError(f"no workspace '{slug}'")
    if slug == "main":
        email = env.get("TWENTY_USERNAME", "")
        password = env.get("TWENTY_PASSWORD", "")
        password_file = env.get("TWENTY_PASSWORD_FILE", "")
        if not password and password_file:
            try:
                password = Path(password_file).expanduser().read_text().strip()
            except OSError:
                raise UsageError(f"TWENTY_PASSWORD_FILE ({password_file}) can't be read; run make crm-init") from None
        if not email or not password:
            raise UsageError("TWENTY_USERNAME and TWENTY_PASSWORD (or TWENTY_PASSWORD_FILE) must be set")
        base = env.get("TWENTY_BASE_URL", "http://127.0.0.1:3000")
        ws_id = values.get("workspace_id", "")
        origin = values.get("workspace_url", "")
        if not ws_id or not origin:
            raise UsageError(f"workspace '{slug}' has no workspace_id or workspace_url; run make crm-init")
    else:
        email = env.get("TWENTY_OWNER_EMAIL", "")
        password = env.get("TWENTY_OWNER_PASSWORD", "")
        if not email or not password:
            raise UsageError(f"TWENTY_OWNER_EMAIL and TWENTY_OWNER_PASSWORD must be set for workspace '{slug}'")
        base = values.get("url", "")
        if not base:
            raise UsageError(f"workspace '{slug}' has no url")
        ws_id = values.get("workspace_id", "")
        origin = values.get("workspace_url") or base
    tw = Twenty(base)
    print(f"[crm] {slug}: signing in as {email}", file=sys.stderr, flush=True)
    try:
        _, available = sign_in(tw, email, password)
        if not ws_id:
            # Without a stored workspace_id only a single workspace is safe to
            # pick; minting in the wrong one would hand out the wrong access.
            if len(available) != 1:
                raise KeyFailure("ambiguous_workspace",
                                 f"the owner account can enter {len(available)} workspaces on {base}; "
                                 f"store the workspace ID on the instance (workspace_id for '{slug}')")
            ws_id = available[0].get("id", "")
        return tw, workspace_access_token(tw, available, ws_id, origin.rstrip("/"))
    except Unreachable as exc:
        raise KeyFailure("unreachable", str(exc)) from None
    except TwentyError as exc:
        raise KeyFailure("login_refused", f"Twenty refused the sign-in: {exc}") from None


PERMISSION = re.compile(r"permission|forbidden|not allowed|unauthori[sz]ed", re.I)


def _failure(exc: TwentyError, role: bool = False) -> KeyFailure:
    """The KeyFailure for a Twenty error after sign-in: unreachable, no_permission
    for a refusal that reads as one, no_role for a refused role create, else failed."""
    if isinstance(exc, Unreachable):
        return KeyFailure("unreachable", str(exc))
    if PERMISSION.search(str(exc)):
        return KeyFailure("no_permission", str(exc))
    return KeyFailure("no_role" if role else "failed", str(exc))


def _stage(fn, *args, role: bool = False):
    """fn(*args), with a Twenty failure turned into a KeyFailure."""
    try:
        return fn(*args)
    except TwentyError as exc:
        raise _failure(exc, role) from None


def _role_id(tw: Twenty, access: str, kind: str) -> str:
    """The Hermes role for KIND (ro or rw): found by label, else both roles are
    created as crm init creates them."""
    label = ROLES[kind]["label"]
    roles = _stage(lambda: tw.gql("query { getRoles { id label } }", token=access).get("getRoles"))
    for role in roles if isinstance(roles, list) else []:
        if isinstance(role, dict) and role.get("label") == label and role.get("id"):
            return role["id"]
    created = _stage(ensure_roles, tw, access, role=True)
    # As crm init does: the read-only role also needs its per-object denials.
    _stage(deny_writes_per_object, tw, access, created["ro"], role=True)
    return created[kind]


def _revoke(tw: Twenty, access: str, key_id: str) -> None:
    tw.gql("mutation($i: RevokeApiKeyInput!) { revokeApiKey(input: $i) { id } }",
           {"i": {"id": key_id}}, token=access)


def cmd_key(args: argparse.Namespace) -> int:
    """Mint, revoke or list one person's API key. Boundary runs these directly.

    create prints one JSON line {"id", "token"}; the token is the only secret
    that reaches stdout, from this command alone. revoke prints nothing. list
    prints {"id", "name", "revoked"} for each active key with the given name
    (Twenty does not list revoked keys). A failure
    prints {"error", "message"} on stdout and exits 4 (login_refused,
    ambiguous_workspace, no_permission, unreachable, no_role, not_found, failed); progress goes to stderr.
    """
    try:
        tw, access = owner_session(os.environ, args.slug)
        if args.action == "create":
            kind = "ro" if args.access == "read" else "rw"
            role_id = _role_id(tw, access, kind)
            print(f"[crm] {args.slug}: creating key '{args.name}' ({args.access})", file=sys.stderr, flush=True)
            try:
                key_id, token = mint_key(tw, access, args.name, role_id)
            except TwentyError as exc:
                failure = _failure(exc)
                orphan = getattr(exc, "key_id", None)
                if orphan:
                    # Created but never given a token: revoke it so it isn't left behind.
                    try:
                        _revoke(tw, access, orphan)
                    except TwentyError:
                        failure = KeyFailure(failure.code, f"{failure} (key {orphan} was created and "
                                             f"couldn't be revoked; revoke it with key revoke --id {orphan})")
                raise failure from None
            print(json.dumps({"id": key_id, "token": token}), flush=True)
        elif args.action == "revoke":
            # Idempotent, for deprovisioning: an already revoked key is a success.
            # The apiKeys query omits revoked keys (Twenty v2.60.3), so look the id
            # up with apiKey, which returns a key of any state and null for no key.
            d = _stage(lambda: tw.gql("query($i: GetApiKeyInput!) { apiKey(input: $i) { id revokedAt } }",
                                      {"i": {"id": args.id}}, token=access))
            found = d.get("apiKey")
            if not found:
                raise KeyFailure("not_found", f"no key with id {args.id}")
            if not found.get("revokedAt"):
                _stage(_revoke, tw, access, args.id)
        else:
            # Twenty's apiKeys query returns only active keys and nothing else
            # lists them, so a revoked key never appears here.
            d = _stage(lambda: tw.gql("query { apiKeys { id name revokedAt } }", token=access))
            for key in d.get("apiKeys") or []:
                if key.get("name") == args.name:
                    print(json.dumps({"id": key.get("id"), "name": key["name"], "revoked": bool(key.get("revokedAt"))}),
                          flush=True)
    except UsageError as exc:
        print(f"[crm] ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyFailure as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), flush=True)
        return 4
    return 0


def _field(value) -> str:
    """One tab-separated field: "-" when empty or unsafe for the shell's read."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return "-"
    return value


def cmd_list(_args: argparse.Namespace) -> int:
    """One line per workspace for crm.sh, tab-separated: slug, local (1/0),
    access, url, workspace_url, workspace_id, has key_ro (1/0), has key_rw
    (1/0). Empty values are "-". No key value is printed."""
    store = Workspaces(os.environ)
    for slug in store.slugs():
        v = store.get(slug) or {}
        print("\t".join([
            slug, _field(v.get("local") is True), _field(v.get("access")), _field(v.get("url")),
            _field(v.get("workspace_url")), _field(v.get("workspace_id")),
            _field(bool(v.get("key_ro"))), _field(bool(v.get("key_rw"))),
        ]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create or re-key a local workspace")
    p_init.add_argument("--slug", default="main")
    p_init.add_argument("--display-name", default="")
    p_init.add_argument("--rotate", action="store_true", help="revoke and re-mint the workspace's keys")
    p_init.add_argument("--owner-only", action="store_true",
                        help="stop after creating or signing in to the owner account")
    p_link = sub.add_parser("login-link", help="print a sign-in URL for a local workspace")
    p_link.add_argument("--slug", default="main")
    p_key = sub.add_parser("key", help="create, revoke or list one person's API key")
    ksub = p_key.add_subparsers(dest="action", required=True)
    p_kc = ksub.add_parser("create")
    p_kc.add_argument("--name", required=True)
    p_kc.add_argument("--access", required=True, choices=("read", "write"))
    p_kr = ksub.add_parser("revoke")
    p_kr.add_argument("--id", required=True)
    p_kl = ksub.add_parser("list")
    p_kl.add_argument("--name", required=True)
    p_kl.add_argument("--json", action="store_true")
    for p in (p_kc, p_kr, p_kl):
        p.add_argument("--slug", default="main")
    sub.add_parser("list", help="the stored workspaces, one tab-separated line each")
    args = parser.parse_args(argv)
    run = {"login-link": cmd_login_link, "list": cmd_list, "key": cmd_key}.get(args.cmd, cmd_init)
    try:
        return run(args)
    except (TwentyError, addon_store.InputError, subprocess.CalledProcessError, OSError) as exc:
        print(f"[crm] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
