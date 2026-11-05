#!/usr/bin/env python3
"""Set up a local Twenty workspace for Hermes: owner account, workspace, two
roles and two API keys, written to compose/twenty/workspaces/<slug>.env.

Called by scripts/crm.sh (make crm-init). Every value comes from the
environment, so a non-interactive caller can run it too. Prints one line per
step and never a key, token or password. See docs/crm.md.

Environment:
  TWENTY_BASE_URL         where to reach the server from the host (http://127.0.0.1:3000)
  TWENTY_USERNAME         owner account email
  TWENTY_PASSWORD         owner account password, or
  TWENTY_PASSWORD_FILE    a file holding it; generated there (mode 600) when missing
  TWENTY_MULTIWORKSPACE   1 allows more than one local workspace
  TWENTY_WORKSPACES_DIR   compose/twenty/workspaces
  TWENTY_DATA_DIR         where .crm-init-done is written
  TWENTY_GRANT_ADMIN_CMD  executable run as `<cmd> grant-admin <email>` on every run
                          (the grant is idempotent)
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

LOCAL_URL = "http://twenty-server:3000"
KEY_EXPIRY = "2126-01-01T00:00:00Z"
SLUG = re.compile(r"^[a-z0-9-]+$")
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
            raise TwentyError(f"can't reach Twenty at {self.base_url}: {exc}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TwentyError("Twenty answered with something other than JSON") from None
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

    Twenty v2.55 lets a role without canUpdateAllObjectRecords still create
    junction and system records (noteTarget, taskTarget, attachment, ...).
    A per-object permission closes that. Twenty refuses one on isUIReadOnly
    objects, which have no write tools anyway. Each call replaces the role's
    whole set, so everything goes in one call. New objects need a re-run
    (make crm-init ROTATE=1).
    """
    d = tw.gql("query { objects(paging: { first: 1000 }) { edges { node { id isUIReadOnly isActive } } } }",
               token=access)
    edges = ((d.get("objects") or {}).get("edges")) or []
    perms = [
        {"objectMetadataId": e["node"]["id"], "canReadObjectRecords": True, "canUpdateObjectRecords": False,
         "canSoftDeleteObjectRecords": False, "canDestroyObjectRecords": False}
        for e in edges
        if isinstance(e, dict) and isinstance(e.get("node"), dict)
        and e["node"].get("isActive") and not e["node"].get("isUIReadOnly")
    ]
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


def mint_key(tw: Twenty, access: str, name: str, role_id: str) -> str:
    d = tw.gql("mutation($i: CreateApiKeyInput!) { createApiKey(input: $i) { id } }",
               {"i": {"name": name, "expiresAt": KEY_EXPIRY, "roleId": role_id}}, token=access)
    key_id = _token(d, "createApiKey", "id")
    d = tw.gql("mutation($k: UUID!, $e: String!) { generateApiKeyToken(apiKeyId: $k, expiresAt: $e) { token } }",
               {"k": key_id, "e": KEY_EXPIRY}, token=access)
    return _token(d, "generateApiKeyToken", "token")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def local_workspaces(ws_dir: Path) -> list[str]:
    """Slugs of workspace files that point at the local instance."""
    slugs = []
    for f in sorted(ws_dir.glob("*.env")):
        if f.name.startswith("."):
            continue
        if read_env_file(f).get("TWENTY_URL", "").rstrip("/") in (LOCAL_URL, f"{LOCAL_URL}/mcp"):
            slugs.append(f.stem)
    return slugs


def write_workspace_file(path: Path, values: dict[str, str]) -> None:
    lines = [
        "# Written by make crm-init. Hermes registers it as mcp_servers.crm-"
        f"{path.stem}. See docs/crm.md.",
        *(f"{k}={v}" for k, v in values.items()),
    ]
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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
    ws_dir = Path(env.get("TWENTY_WORKSPACES_DIR", "compose/twenty/workspaces"))
    ws_dir.mkdir(parents=True, exist_ok=True)
    target = ws_dir / f"{slug}.env"
    multi = is_true(env.get("TWENTY_MULTIWORKSPACE"))

    existing = read_env_file(target) if target.exists() else {}
    complete = bool(existing.get("TWENTY_KEY_RW") and existing.get("TWENTY_KEY_RO"))
    if complete and not args.rotate:
        step(f"{slug}: workspace file exists, nothing to do (--rotate re-mints its keys)")
        return 0
    others = [s for s in local_workspaces(ws_dir) if s != slug]
    if not target.exists() and others and not multi:
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

    if env.get("TWENTY_GRANT_ADMIN_CMD"):
        # The owner creates further workspaces, which Twenty limits to server
        # admins. Idempotent, so it runs every time and repairs a failed grant.
        step("server admin: running")
        subprocess.run([env["TWENTY_GRANT_ADMIN_CMD"], "grant-admin", email], check=True,
                       stdout=subprocess.DEVNULL)
        step("server admin: done")

    if target.exists():
        # A complete file being rotated, or a partial one left by a failed run.
        ws_id = existing.get("TWENTY_WORKSPACE_ID", "")
        ws_url = existing.get("TWENTY_WORKSPACE_URL", "")
        if not ws_id or not ws_url:
            raise TwentyError(f"{target.name} has no TWENTY_WORKSPACE_ID or URL; move it aside and re-run")
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
        # of creating another. Without keys, Hermes skips the file.
        write_workspace_file(target, {
            "TWENTY_URL": LOCAL_URL,
            "TWENTY_WORKSPACE_URL": ws_url,
            "TWENTY_WORKSPACE_ID": ws_id,
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
        keys[kind] = mint_key(tw, access, name, role_id)
    step(f"{slug}: keys: done")

    write_workspace_file(target, {
        "TWENTY_URL": LOCAL_URL,
        "TWENTY_WORKSPACE_URL": ws_url,
        "TWENTY_WORKSPACE_ID": ws_id,
        "TWENTY_KEY_RO": keys["ro"],
        "TWENTY_KEY_RW": keys["rw"],
    })
    step(f"{slug}: wrote {target}")
    data_dir = Path(env.get("TWENTY_DATA_DIR", "data/twenty"))
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".crm-init-done").write_text(f"{slug}\n")
    step(f"done: {ws_url} — sign in as {email}; the password is {password_where}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create or re-key a local workspace")
    p_init.add_argument("--slug", default="main")
    p_init.add_argument("--display-name", default="")
    p_init.add_argument("--rotate", action="store_true", help="revoke and re-mint the workspace's keys")
    args = parser.parse_args(argv)
    try:
        return cmd_init(args)
    except (TwentyError, subprocess.CalledProcessError) as exc:
        print(f"[crm] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
