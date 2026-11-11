#!/usr/bin/env python3
"""bitwarden-setup: the steps that need the user's own vault key, in one run.

    setup.py <command> --slug S [--server URL] --email EMAIL [command flags]

Commands:

``create``           Creates the account's folder (``folder:`` scope) or collection
                     (``collection:`` scope, ``--agent-email`` required). For a
                     collection id that does not exist yet, ``--org ORG`` and
                     ``--name NAME`` (default Agent) say where to create it, and
                     the new id replaces ``scope`` in the instance file. The
                     agent member is invited if it is not one yet and gets Manage
                     on the collection. ``local`` accounts are refused here.
``seed``             Asks for sites and moves the matching items the user picks.
``role``             Changes the agent member to the User role with Manage on the
                     scope collection (Local mode step 6).
                     ``--agent-email`` defaults to agent@vault.localhost for ``local``.
``list-unassigned``  Organization items in no collection, by name and URI host.

Local-mode files, which need no account or password. They go to /local
(``BITWARDEN_HOST_DATA_DIR/local``), which only bitwarden-setup mounts, so
the broker never sees the admin token or the CA's key:

    setup.py tls [--dir DIR] [--ca-copy FILE] [--name NAME] [--rotate]
    setup.py admin-token [--dir DIR] [--rotate]

``tls``          Writes the private CA to DIR (default /local/tls) and the
                 sidecar's server pair to DIR/server/ (setup/tls.py), then
                 copies ca.crt to FILE (default /secrets/local/tls/ca.crt),
                 where the broker's NODE_EXTRA_CA_CERTS points.
``admin-token``  Writes a random Vaultwarden admin token to DIR/admin_token and
                 its Argon2id PHC hash, which Vaultwarden reads through
                 ADMIN_TOKEN_FILE, to DIR/vaultwarden/admin_token.phc. Both are
                 mode 600. DIR defaults to /local.

Both keep files that exist and refuse a half-written set; ``--rotate``
replaces everything.

    setup.py local-init --owner EMAIL [--server URL] [--signup-url URL] [--wait SECONDS]

``local-init``   Local mode steps 2 to 5, against the running local Vaultwarden.
                 Registers agent@vault.localhost with a generated master password
                 (kept in /secrets/local/master_password) after an admin invite,
                 stores its API key in /secrets/local/client_secret, creates the
                 organization "Assistant" with the collection "Agent", writes the local
                 account (below), invites EMAIL as an Owner,
                 then waits up to SECONDS (default 600) for that address to register
                 at the sign-up URL and confirms it with ``bw confirm org-member``.
                 Each finished step is recorded in /local/state (``STEP=`` plus the
                 ids it made), so a re-run picks up after the last one. The run
                 signs in to Vaultwarden once for the API and once for bw, because
                 Vaultwarden allows 10 password logins a minute; on a 429 it waits
                 BITWARDEN_RATE_LIMIT_WAIT seconds (default 60) and tries once more.
                 Exits 4 when the owner has not registered in time.

The local account is ``/broker/local-account.json`` (``{"scope":
"collection:<id>"}``, mode 600) and ``/secrets/local/client_id``; the broker
loads it as mode ``local`` with stored unlock. It stays out of the add-on
instance store. An existing organization named "Assistant" is reused rather
than created twice. An existing local-account.json or client_id that names
other ids than /local/state is refused; ``--rotate`` writes them again from
the state.

``role`` on the ``local`` account records ``STEP=role`` in /local/state once it
succeeds after ``local-init``.

The account is ``$BITWARDEN_INSTANCES_DIR/<slug>.json`` (default
/store/instances, the add-on instance store) with its client_id in
``$BITWARDEN_SECRETS_DIR/<slug>/client_id``, or for ``local`` the files above
(``$BITWARDEN_BROKER_DIR``, default /broker). ``--server`` is the user's server
and defaults to the account's server; it must be https,
except plain http to a loopback address, which only a test can serve from
inside this container. ``BITWARDEN_BW`` names the bw binary (default ``bw``).

The master password is read with getpass, three tries. It reaches bw only as
``BW_PW`` with ``--passwordenv`` and the token request only in memory. bw runs
with ``BITWARDENCLI_APPDATA_DIR`` in a new directory under /tmp; ``bw lock``,
``bw logout`` and removing that directory happen on every exit, SIGINT and
SIGTERM included. Output carries item names, usernames and URIs, never a
password, TOTP seed, note or token.

Exit codes: 0 done (steps left to the user are printed), 1 failed, 2 three
wrong passwords and nothing changed, 130 interrupted.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vw_register  # noqa: E402
from broker.accounts import (  # noqa: E402
    ID_RE, LOCAL_FILE, LOCAL_SLUG, Account, AccountError, load_account, read_client_id,
)
from broker.bwcli import BwError, classify, run_bw, sanitize_item  # noqa: E402
from broker.vault import fill_origins  # noqa: E402

NOTICE = ("Moving an item into an organization collection transfers it from your personal vault to the "
          "organization. Members with access to the collection can see it.")
PASSWORD_TRIES = 3
EXIT_FAILED, EXIT_PASSWORD, EXIT_INTERRUPTED = 1, 2, 130
LOCAL_AGENT_EMAIL = "agent@vault.localhost"
DEFAULT_COLLECTION = "Agent"
MEMBER_INVITED, MEMBER_ACCEPTED = 0, 1
TYPE_USER = 2
COMMANDS = ("create", "seed", "role", "list-unassigned")
LOCAL_COMMANDS = ("tls", "admin-token")
LOCAL_DIR = "/local"
LOCAL_SERVER = "https://vaultwarden.vault.internal"
LOCAL_ORG_NAME = "Assistant"
LOCAL_SIGNUP_URL = "http://localhost:8222"
# "stack" and STACK_COLLECTION are no longer written. Older /local/state files still hold them.
LOCAL_STEPS = ("registered", "api_key", "org", "stack", "account_file", "invited", "confirmed", "role")
STATE_KEYS = ("STEP", "CLIENT_ID", "ORG_ID", "AGENT_COLLECTION", "STACK_COLLECTION", "OWNER_EMAIL")
STATE_FILE = "state"
EXIT_WAITING = 4
MEMBER_CONFIRMED = 2
TYPE_OWNER = 0
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
CLIENT_ID_RE = re.compile(r"user\.[A-Za-z0-9-]{1,64}")
CA_COPY = "/secrets/local/tls/ca.crt"
ADMIN_TOKEN_FILE = "admin_token"
ADMIN_HASH_FILE = "vaultwarden/admin_token.phc"


class SetupError(Exception):
    """A message for the user; the run exits 1."""


class PasswordTriesExhausted(Exception):
    pass


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


# Matching

def normalize_host(value: str) -> str | None:
    """A host from what the user typed: a bare host, host:port or a URL. None if it is not one.

    A host with no dot is refused: items match subdomains too, so "com" would
    match every .com login.
    """
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        host = urlsplit(value if "://" in value else f"https://{value}").hostname
    except ValueError:
        return None
    host = (host or "").rstrip(".")
    if not host or "." not in host or any(c.isspace() for c in host):
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _hosts(item: dict) -> list[tuple[str, str]]:
    """``(host, uri)`` for each URI the broker would fill on (http or https, match type not Never)."""
    out = []
    for uri in item.get("uris") or []:
        origins = fill_origins([uri])
        if origins:
            out.append((urlsplit(origins[0]).hostname or "", uri["uri"]))
    return out


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _matching_uri(item: dict, host: str) -> str | None:
    # An IP address has no subdomains: 0.0.1 must not match 10.0.0.1.
    subdomains = not _is_ip(host)
    for item_host, uri in _hosts(item):
        if item_host == host or (subdomains and not _is_ip(item_host) and item_host.endswith("." + host)):
            return uri
    return None


def match_items(items: list[dict], host: str) -> list:
    """Items with a fill URI whose host is ``host`` or ends with ``.`` + ``host`` (IP addresses: equal only)."""
    host = normalize_host(host)
    if host is None:
        return []
    return [i for i in items if _matching_uri(i, host) is not None]


# bw with the user's login

class UserBw:
    """bw for the user's account in a temporary data dir. The session key stays on this object."""

    def __init__(self, bw_path: str, appdata: str):
        self.bw_path = bw_path
        self.appdata = appdata
        self._session: str | None = None

    def run(self, args: list[str], stdin: str | None = None, secret_env: dict[str, str] | None = None) -> str:
        env = secret_env if secret_env is not None else {}
        if self._session is not None:
            env["BW_SESSION"] = self._session
        try:
            code, out, err = run_bw(self.bw_path, self.appdata, args, env, stdin)
        finally:
            env.clear()
            stdin = None
        if code == 0:
            return out
        error_class, _ = classify(err, unlock=args[:1] == ["unlock"])
        out = err = None
        raise BwError(error_class)

    def json(self, args: list[str], stdin: str | None = None):
        try:
            out = self.run(args, stdin)
        except BwError as exc:
            raise SetupError(f"bw {' '.join(args[:2])} failed ({exc.error_class}).") from None
        try:
            return json.loads(out or "null")
        except ValueError:
            raise SetupError(f"bw {' '.join(args[:2])} returned something other than JSON.") from None
        finally:
            out = None

    def configure(self, server: str) -> None:
        """Point bw at ``server`` and confirm with ``bw status``; SetupError otherwise.

        bw's default server is vault.bitwarden.com, so logging in after a failed
        config would send the email and password hash to a server nobody chose.
        """
        try:
            self.run(["config", "server", server])
        except BwError as exc:
            raise SetupError(f"bw config server failed ({exc.error_class}); nothing was sent.") from None
        status = self.json(["status"])
        actual = status.get("serverUrl") if isinstance(status, dict) else None
        if not isinstance(actual, str) or actual.rstrip("/") != server.rstrip("/"):
            raise SetupError("bw is not pointed at the requested server after config; nothing was sent.")

    def login(self, email: str, holder: list[str]) -> None:
        """Log in with ``holder[0]``; raises BwError("auth") on a wrong password. Call ``configure`` first."""
        key = self.run(["login", email, "--passwordenv", "BW_PW", "--raw"], secret_env={"BW_PW": holder[0]}).strip()
        if not key:
            raise BwError("unknown")
        self._session = key

    def close(self) -> None:
        for args in (["lock"], ["logout"]):
            try:
                self.run(args)
            except BwError:
                pass
        self._session = None


# One run

@dataclass
class Target:
    kind: str  # "folder" | "collection"
    id: str
    name: str
    org: str | None = None

    @property
    def label(self) -> str:
        return f'{self.kind} "{self.name}"'


class Run:
    def __init__(self, args, account: Account, bw: UserBw):
        self.args = args
        self.account = account
        self.bw = bw
        self.token: str | None = None
        self.token_error: str | None = None
        self.notice_shown = False

    # Org API

    def _org_call(self, what: str, fn, *args) -> None:
        """Run a vw_register call with the token; raises SetupError naming ``what`` and the reason."""
        try:
            fn(self.args.server, self.token, *args)
        except vw_register.VwError as exc:
            failure = exc.failure
        else:
            return
        raise SetupError(f"{what} {failure}")

    def api(self, method: str, path: str, body: dict, what: str) -> None:
        self._org_call(what, vw_register.api, method, path, body)

    def invite(self, org: str, email: str) -> None:
        self._org_call("invite", vw_register.invite_member, org, email, TYPE_USER, [])

    def web_vault_note(self) -> str:
        reason = self.token_error or "no token"
        return f"Setup could not use the organization API ({reason}). Do this in the web vault:"

    # Lookups

    def collections(self) -> list[dict]:
        rows = self.bw.json(["list", "collections"])
        return [c for c in rows or [] if isinstance(c, dict) and isinstance(c.get("id"), str)]

    def scope_collection(self) -> dict | None:
        return next((c for c in self.collections() if c["id"] == self.account.scope_value), None)

    def members(self, org: str) -> list[dict]:
        rows = self.bw.json(["list", "org-members", "--organizationid", org])
        return [m for m in rows or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]

    def member(self, org: str, email: str) -> dict | None:
        email = email.strip().lower()
        return next((m for m in self.members(org) if str(m.get("email", "")).lower() == email), None)

    def folder_ids(self, name: str) -> list[str]:
        rows = self.bw.json(["list", "folders"])
        return [f["id"] for f in rows or [] if isinstance(f, dict) and f.get("name") == name
                and isinstance(f.get("id"), str)]

    def target(self) -> Target:
        kind, value = self.account.scope_kind, self.account.scope_value
        if kind == "folder":
            ids = self.folder_ids(value)
            if not ids:
                raise SetupError(f'The folder "{value}" does not exist. Run create first.')
            return Target("folder", ids[0], value)
        if kind == "collection":
            coll = self.scope_collection()
            if coll is None:
                raise SetupError(f"Collection {value} is not visible to {self.args.email}. Run create first.")
            return Target("collection", coll["id"], coll.get("name") or value, coll.get("organizationId"))
        raise SetupError("This account's scope is all; there is no folder or collection to move items into.")

    def items(self, extra: tuple[str, ...] = ()) -> list[dict]:
        """Login-item metadata from the user's vault; passwords, TOTP seeds and notes are dropped here."""
        raw = self.bw.json(["list", "items", *extra])
        out = []
        try:
            for obj in raw or []:
                meta = sanitize_item(obj)
                if meta is None or meta["type"] != 1:
                    continue
                org = obj.get("organizationId")
                meta["organizationId"] = org if isinstance(org, str) else None
                out.append(meta)
        finally:
            raw = None
        return out

    # Moves

    def in_target(self, item: dict, target: Target) -> bool:
        if target.kind == "folder":
            return item["folderId"] == target.id
        return target.id in item["collectionIds"]

    def move(self, item: dict, target: Target) -> bool:
        if target.kind == "folder":
            full = encoded = None
            try:
                full = json.loads(self.bw.run(["get", "item", item["id"]]))
                full["folderId"] = target.id
                encoded = _b64(full)
                full = None
                self.bw.run(["edit", "item", item["id"]], stdin=encoded)
            except (BwError, ValueError, TypeError):
                print(f'Could not move "{item["name"]}".')
                return False
            finally:
                full = encoded = None
            item["folderId"] = target.id
        elif item["organizationId"] is None:
            if not self.notice_shown:
                print(NOTICE)
                self.notice_shown = True
            try:
                self.bw.run(["move", item["id"], target.org], stdin=_b64([target.id]))
            except BwError as exc:
                print(f'Could not move "{item["name"]}" ({exc.error_class}).')
                return False
            item["organizationId"], item["collectionIds"] = target.org, [target.id]
        elif item["organizationId"] == target.org:
            ids = [*item["collectionIds"], target.id]
            try:
                self.bw.run(["edit", "item-collections", item["id"], "--organizationid", target.org],
                            stdin=_b64(ids))
            except BwError as exc:
                print(f'Could not add "{item["name"]}" to the collection ({exc.error_class}).')
                return False
            item["collectionIds"] = ids
        else:
            print(f'"{item["name"]}" belongs to another organization; skipped.')
            return False
        print(f'Moved "{item["name"]}" into {target.label}.')
        return True

    def show(self, matches: list[dict], host: str) -> None:
        for number, item in enumerate(matches, 1):
            print(f"  {number}. {item['name']}  {item['username'] or '-'}  {_matching_uri(item, host)}")

    def pick(self, count: int) -> list[int]:
        while True:
            answer = input("Move which? Numbers separated by spaces, blank for none: ").replace(",", " ").split()
            try:
                picked = [int(a) for a in answer]
            except ValueError:
                picked = None
            if picked is not None and all(1 <= p <= count for p in picked):
                return list(dict.fromkeys(picked))
            print(f"Enter numbers from 1 to {count}.")

    def offer(self, items: list[dict], target: Target, site: str) -> int:
        """Show the items matching ``site`` outside the target and move the picked ones. Returns moves."""
        host = normalize_host(site)
        if host is None:
            print(f"{site!r} is not a host name.")
            return 0
        candidates = [i for i in items if not self.in_target(i, target)]
        matches = match_items(candidates, host)
        already = len(match_items([i for i in items if self.in_target(i, target)], host))
        if already:
            print(f"{already} item(s) for {host} already in {target.label}.")
        if not matches:
            print(f"No other login items for {host}.")
            return 0
        self.show(matches, host)
        return sum(self.move(matches[n - 1], target) for n in self.pick(len(matches)))

    # Commands

    def cmd_seed(self) -> int:
        target = self.target()
        items = self.items()
        while True:
            site = input("Site to give the agent (blank to finish): ").strip()
            if not site:
                return 0
            self.offer(items, target, site)

    def cmd_create(self) -> int:
        if self.account.scope_kind == "folder":
            name = self.account.scope_value
            if self.folder_ids(name):
                print(f'The folder "{name}" already exists.')
            else:
                self.bw.json(["create", "folder"], stdin=_b64({"name": name}))
                print(f'Created the folder "{name}".')
            return 0
        return self.create_collection()

    def create_collection(self) -> int:
        agent_email = self.args.agent_email
        existing = self.scope_collection()
        if existing is not None:
            org, name = existing.get("organizationId"), existing.get("name") or existing["id"]
        else:
            org, name = self.args.org, self.args.name or DEFAULT_COLLECTION
            if not org:
                raise SetupError(f"Collection {self.account.scope_value} does not exist. "
                                 "Pass --org with the organization id to create one.")
        if not isinstance(org, str) or not ID_RE.fullmatch(org):
            raise SetupError("The collection has no usable organization id.")

        member = self.member(org, agent_email)
        invite_steps = False
        if member is None:
            if self.token is None:
                invite_steps = True
            else:
                try:
                    self.invite(org, agent_email)
                    print(f"Invited {agent_email} to the organization.")
                    member = self.member(org, agent_email)
                except SetupError as exc:
                    self.token_error = str(exc)
                    invite_steps = True
        access = ({"id": member["id"], "readOnly": False, "hidePasswords": False, "manage": True}
                  if member is not None else None)

        if existing is None:
            created = self.bw.json(["create", "org-collection", "--organizationid", org], stdin=_b64({
                "organizationId": org, "name": name, "externalId": None, "groups": [],
                "users": [access] if access else []}))
            cid = created.get("id") if isinstance(created, dict) else None
            if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
                raise SetupError("bw create org-collection returned no id.")
            print(f'Created the collection "{name}" ({cid}).')
            self.set_scope(cid)
        elif access is not None:
            cid = existing["id"]
            current = self.bw.json(["get", "org-collection", cid, "--organizationid", org])
            users = [u for u in (current or {}).get("users") or [] if isinstance(u, dict)]
            if access not in users:
                users = [u for u in users if u.get("id") != access["id"]] + [access]
                self.bw.json(["edit", "org-collection", cid, "--organizationid", org], stdin=_b64({
                    "organizationId": org, "name": current.get("name"), "externalId": current.get("externalId"),
                    "groups": current.get("groups") or [], "users": users}))
            print(f'Collection "{name}" exists; {agent_email} has Manage on it.')

        if invite_steps:
            print(self.web_vault_note())
            print(f"  1. Admin Console > Members > Invite member: {agent_email}, role User.")
            print(f'  2. Give it "Can manage" on the collection "{name}".')
        if member is None or member.get("status") == MEMBER_INVITED:
            print(f"Left to do: sign in as {agent_email} (sign up first if the account does not exist yet) "
                  "and accept the invite to the organization. Once it shows as accepted, confirm it in "
                  "Admin Console > Members.")
        elif member.get("status") == MEMBER_ACCEPTED:
            print(f"Left to do: confirm {agent_email} in Admin Console > Members.")
        return 0

    def set_scope(self, cid: str) -> None:
        """Point the instance file's scope at the new collection, keeping every other key."""
        path = self.account.path
        scope = f"collection:{cid}"
        fix = (f"set it with: printf '%s' '{{\"scope\":\"{scope}\"}}' | "
               f"scripts/addon.sh configure bitwarden --instance {self.account.slug}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            data["scope"] = scope
            st = path.stat()
            # The add-on store's own temp-file pattern and mode (scripts/lib/addon_store.py _write).
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            owner_note = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2) + "\n")
                    if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()):
                        try:
                            os.fchown(f.fileno(), st.st_uid, st.st_gid)
                        except PermissionError:
                            owner_note = True
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
        except (OSError, ValueError):
            print(f"Could not update {path.name}; {fix}")
            return
        print(f"Set scope {scope} in {path.name}.")
        if owner_note:
            print(f"Note: {path} now belongs to this container's user; its owner could not be kept.")

    def cmd_role(self) -> int:
        agent_email = self.args.agent_email or LOCAL_AGENT_EMAIL
        agent_coll = self.scope_collection()
        if agent_coll is None:
            raise SetupError(f"Collection {self.account.scope_value} is not visible to {self.args.email}.")
        org = agent_coll.get("organizationId")
        member = self.member(org, agent_email)
        if member is None:
            raise SetupError(f"{agent_email} is not a member of the organization.")
        access = [{"id": agent_coll["id"], "readOnly": False, "hidePasswords": False, "manage": True}]
        names = f'Manage on "{agent_coll.get("name")}"'
        if self.token is not None:
            try:
                self.api("PUT", f"/organizations/{org}/users/{member['id']}",
                         {"type": TYPE_USER, "groups": [], "permissions": {}, "collections": access}, "member edit")
            except SetupError as exc:
                self.token_error = str(exc)
                self.token = None
        if self.token is None:
            print(self.web_vault_note())
            print(f"  Admin Console > Members > {agent_email} > Role: User; collections: {names}; nothing else.")
            return EXIT_FAILED
        print(f"Changed {agent_email} to the User role with {names}.")
        return 0

    def cmd_list_unassigned(self) -> int:
        coll = self.scope_collection()
        if coll is None:
            raise SetupError(f"Collection {self.account.scope_value} is not visible to {self.args.email}.")
        loose = [i for i in self.items(("--organizationid", coll["organizationId"])) if not i["collectionIds"]]
        if not loose:
            print("No unassigned items in the organization.")
            return 0
        print("Unassigned organization items (no collection):")
        for item in loose:
            hosts = []
            for uri in item["uris"]:
                try:
                    host = urlsplit(uri["uri"]).hostname
                except ValueError:
                    host = None
                if host and host not in hosts:
                    hosts.append(host)
            print(f"  {item['name']}  {', '.join(hosts) or '-'}")
        return 0


# Entry

def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(prog="bitwarden-setup")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--server")
    parser.add_argument("--agent-email")
    parser.add_argument("--org")
    parser.add_argument("--name")
    return parser.parse_args(argv)


def _is_loopback_url(server: str) -> bool:
    try:
        host = urlsplit(server).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_server(server: str) -> str | None:
    try:
        parts = urlsplit(server)
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.username or parts.password or parts.query or parts.fragment or not host:
        return None
    if parts.scheme == "https":
        return server.rstrip("/")
    if parts.scheme == "http" and _is_loopback_url(server):
        return server.rstrip("/")
    return None


def preflight(args, account: Account) -> str | None:
    """A reason to refuse before asking for the password, or None."""
    if args.org and not ID_RE.fullmatch(args.org):
        return "--org is not a Bitwarden organization id."
    kind = account.scope_kind
    if args.command == "create":
        if account.mode == "local":
            return "create does not handle local accounts; make vault-init sets them up."
        if kind == "all":
            return "This account's scope is all; there is nothing to create."
        if kind == "collection" and not args.agent_email:
            return "create for a collection: scope needs --agent-email, the agent member account's email."
    if args.command in ("role", "list-unassigned") and kind != "collection":
        return f"{args.command} needs a collection: scope."
    if args.command == "seed" and kind == "all":
        return "This account's scope is all; there is no folder or collection to move items into."
    if args.command == "role" and not args.agent_email and account.mode != "local":
        return "role needs --agent-email for an account that is not local."
    return None


def ask_password(bw: UserBw, email: str) -> list[str]:
    """Log bw in, three tries. Returns a one-element list holding the password, for the token call."""
    for _ in range(PASSWORD_TRIES):
        holder = [getpass.getpass(f"Master password for {email}: ")]
        if not holder[0]:
            print("No password entered.")
            continue
        try:
            bw.login(email, holder)
            return holder
        except BwError as exc:
            holder.clear()
            if exc.error_class != "auth":
                raise SetupError(f"bw login failed ({exc.error_class}).") from None
            print("Bitwarden did not accept that email and password.")
    raise PasswordTriesExhausted


# Local-mode files

def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_admin_token(dir: Path, rotate: bool = False) -> str:
    """Write the admin token and its Argon2id hash under ``dir``; returns what happened, without either value."""
    from argon2 import PasswordHasher, Type
    from argon2.exceptions import InvalidHash, VerificationError

    plain_path, hash_path = dir / ADMIN_TOKEN_FILE, dir / ADMIN_HASH_FILE
    hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID)
    plain = None
    if not rotate and plain_path.exists():
        plain = plain_path.read_text(encoding="utf-8").strip()
        if not plain:
            raise SetupError(f"{plain_path} is empty; pass --rotate to make a new token.")
        if hash_path.exists():
            try:
                hasher.verify(hash_path.read_text(encoding="utf-8").strip(), plain)
            except (InvalidHash, VerificationError):
                raise SetupError(f"{hash_path} does not match {plain_path}; pass --rotate to replace both.") from None
            return f"Kept {plain_path} and {hash_path}."
        _write_secret(hash_path, hasher.hash(plain))
        return f"Kept {plain_path}; wrote its hash to {hash_path}."
    if not rotate and hash_path.exists():
        raise SetupError(f"{hash_path} exists without {plain_path}; pass --rotate to replace it.")
    plain = secrets.token_urlsafe(48)
    try:
        _write_secret(plain_path, plain)
        _write_secret(hash_path, hasher.hash(plain))
    finally:
        plain = None
    note = " Restart vaultwarden to use it." if rotate else ""
    return f"Wrote {plain_path} and {hash_path}.{note}"


def local_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bitwarden-setup")
    parser.add_argument("command", choices=LOCAL_COMMANDS)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--ca-copy", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "tls":
            import tls
            tls_dir = args.dir or Path(LOCAL_DIR) / "tls"
            ca_copy = args.ca_copy or Path(CA_COPY)
            try:
                tls.make_local_tls(tls_dir, args.name or tls.LOCAL_NAME, rotate=args.rotate)
                tls.copy_ca(tls_dir, ca_copy)
            except tls.TlsError as exc:
                raise SetupError(str(exc)) from None
            print(f"TLS files for {args.name or tls.LOCAL_NAME} are in {tls_dir}; the CA certificate is "
                  f"also at {ca_copy}.")
        else:
            if args.name or args.ca_copy:
                raise SetupError("--name and --ca-copy apply to tls only.")
            print(make_admin_token(args.dir or Path(LOCAL_DIR), rotate=args.rotate))
    except SetupError as exc:
        print(exc)
        return EXIT_FAILED
    except OSError as exc:
        print(f"Could not write the files in {args.dir} ({exc.strerror or 'error'}).")
        return EXIT_FAILED
    return 0


# Local mode steps 2-5 (local-init)

def read_state(local_dir: Path) -> dict[str, str]:
    """``KEY=value`` pairs from ``local_dir/state``; {} when there is none."""
    try:
        text = (local_dir / STATE_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    state = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in STATE_KEYS:
            state[key] = value.strip()
    return state


def write_state(local_dir: Path, state: dict[str, str]) -> None:
    lines = []
    for key in STATE_KEYS:
        value = state.get(key)
        if value:
            if any(c in value for c in "\r\n"):
                raise SetupError(f"{key} holds a line break; not recorded.")
            lines.append(f"{key}={value}")
    _write_secret(local_dir / STATE_FILE, "\n".join(lines))


def mark_role_done(local_dir: Path) -> None:
    """After ``role`` on the local account: STEP=role, but only once local-init got to confirmed."""
    state = read_state(local_dir)
    if state.get("STEP") == "confirmed":
        state["STEP"] = "role"
        write_state(local_dir, state)


def _duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{seconds / 60:g} minutes"
    return f"{seconds:g} seconds"


class RegistrationFailed(Exception):
    pass


class LocalInit:
    """One local-init run. The agent's password, token and bw session stay on this object."""

    def __init__(self, server: str, signup: str, rotate: bool = False):
        self.server = server
        self.signup = signup
        self.rotate = rotate
        self.local_dir = Path(os.environ.get("BITWARDEN_LOCAL_DIR") or LOCAL_DIR)
        self.secrets_dir = Path(os.environ.get("BITWARDEN_SECRETS_DIR") or "/secrets") / "local"
        self.broker_dir = Path(os.environ.get("BITWARDEN_BROKER_DIR") or "/broker")
        self.poll = float(os.environ.get("BITWARDEN_LOCAL_POLL_SECONDS") or 5)
        self.limit_wait = float(os.environ.get("BITWARDEN_RATE_LIMIT_WAIT") or 60)
        self.state = read_state(self.local_dir)
        self.token: str | None = None
        self.keys: vw_register.Keys | None = None
        self.bw: UserBw | None = None
        self.appdata: str | None = None

    # State

    def done(self, step: str) -> bool:
        current = self.state.get("STEP")
        return current in LOCAL_STEPS and LOCAL_STEPS.index(current) >= LOCAL_STEPS.index(step)

    def record(self, step: str | None = None, **values: str) -> None:
        for key, value in values.items():
            pattern = {"OWNER_EMAIL": EMAIL_RE, "CLIENT_ID": CLIENT_ID_RE}.get(key, ID_RE)
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise SetupError(f"Vaultwarden returned an unusable {key.lower()}.")
            self.state[key] = value
        if step is not None and not self.done(step):
            self.state["STEP"] = step
        write_state(self.local_dir, self.state)

    # Sign-ins, each at most once per run

    def _once_more(self, what: str) -> None:
        print(f"Vaultwarden's login rate limit was hit ({what}); waiting {_duration(self.limit_wait)} "
              "and trying once more.")
        time.sleep(self.limit_wait)

    def get_token(self, password: str) -> str:
        if self.token is None:
            try:
                self.token = vw_register.token(self.server, LOCAL_AGENT_EMAIL, password)
            except vw_register.TokenError as exc:
                if exc.status != 429:
                    raise
                self._once_more("API sign-in")
                self.token = vw_register.token(self.server, LOCAL_AGENT_EMAIL, password)
        return self.token

    def bw_login(self, password: str) -> UserBw:
        if self.bw is not None:
            return self.bw
        self.appdata = tempfile.mkdtemp(dir="/tmp", prefix="bitwarden-setup-")
        self.bw = UserBw(os.environ.get("BITWARDEN_BW") or "bw", self.appdata)
        self.bw.configure(self.server)
        holder = [password]
        try:
            try:
                self.bw.login(LOCAL_AGENT_EMAIL, holder)
            except BwError as exc:
                if exc.error_class != "rate_limited":
                    raise
                self._once_more("bw login")
                self.bw.login(LOCAL_AGENT_EMAIL, holder)
            self.bw.run(["sync"])
        except BwError as exc:
            raise SetupError(f"bw could not sign in as {LOCAL_AGENT_EMAIL} ({exc.error_class}).") from None
        finally:
            holder.clear()
        return self.bw

    def close(self) -> None:
        self.token = None
        try:
            if self.bw is not None:
                self.bw.close()
        finally:
            if self.appdata is not None:
                shutil.rmtree(self.appdata, ignore_errors=True)

    # Steps

    def agent_password(self) -> str:
        path = self.secrets_dir / "master_password"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        if not value:
            value = secrets.token_urlsafe(32)
            _write_secret(path, value)
            print(f"Generated a master password for {LOCAL_AGENT_EMAIL} (secrets/local/master_password).")
        return value

    def register(self, password: str) -> None:
        failure = None
        try:
            admin = (self.local_dir / ADMIN_TOKEN_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            raise SetupError(f"No {ADMIN_TOKEN_FILE} in local/; setup.py admin-token writes it.") from None
        try:
            vw_register.admin_invite(self.server, admin, LOCAL_AGENT_EMAIL)
            self.keys = vw_register.register(self.server, LOCAL_AGENT_EMAIL, password)
        except vw_register.VwError as exc:
            failure = str(exc)
        finally:
            admin = None
        try:
            self.get_token(password)
        except vw_register.VwError as exc:
            # Only a refused sign-in means the account is missing; a 429 or an
            # unreachable server says nothing about the registration.
            if failure is None or exc.status not in (400, 401):
                raise
            print(f"Could not register {LOCAL_AGENT_EMAIL} ({failure}).")
            print(f"Register it in the web vault instead: open {self.signup}, choose Create account, and use "
                  f"{LOCAL_AGENT_EMAIL} with the password in secrets/local/master_password under "
                  "BITWARDEN_HOST_DATA_DIR (default data/addons/bitwarden). Then run make vault-init again.")
            raise RegistrationFailed from None
        if failure is not None:
            print(f"{LOCAL_AGENT_EMAIL} is already registered; signed in to it.")
        else:
            print(f"Registered {LOCAL_AGENT_EMAIL}.")
        self.record("registered")

    def members(self, password: str) -> list[dict]:
        path = f"/organizations/{self.state['ORG_ID']}/users"
        try:
            data = vw_register.api(self.server, self.get_token(password), "GET", path)
        except vw_register.VwError as exc:
            if exc.status != 401:
                raise
            self.token = None  # expired during the wait
            data = vw_register.api(self.server, self.get_token(password), "GET", path)
        rows = data.get("data") if isinstance(data, dict) else None
        return [m for m in rows or [] if isinstance(m, dict)]

    def member(self, password: str, email: str) -> dict | None:
        return next((m for m in self.members(password) if str(m.get("email", "")).lower() == email), None)

    def existing_org(self, token: str) -> str | None:
        """The id of the agent's organization named "Assistant", if it has one."""
        profile = vw_register.api(self.server, token, "GET", "/accounts/profile")
        orgs = profile.get("organizations") if isinstance(profile, dict) else None
        ids = [o.get("id") for o in orgs or [] if isinstance(o, dict) and o.get("name") == LOCAL_ORG_NAME
               and isinstance(o.get("id"), str) and ID_RE.fullmatch(o["id"])]
        return ids[0] if ids else None

    def collection_named(self, password: str, name: str, create: bool = True) -> str:
        """The id of the organization's collection called ``name``; bw decrypts the names."""
        bw = self.bw_login(password)
        org = self.state["ORG_ID"]
        bw.run(["sync"])
        rows = bw.json(["list", "collections", "--organizationid", org])
        found = next((c for c in rows or [] if isinstance(c, dict) and c.get("name") == name
                      and c.get("organizationId") == org and isinstance(c.get("id"), str)), None)
        if found is not None:
            return found["id"]
        if not create:
            raise SetupError(f'The organization has no collection "{name}".')
        created = bw.json(["create", "org-collection", "--organizationid", org], stdin=_b64({
            "organizationId": org, "name": name, "externalId": None, "groups": [], "users": []}))
        cid = created.get("id") if isinstance(created, dict) else None
        if not isinstance(cid, str):
            raise SetupError("bw create org-collection returned no id.")
        print(f'Created the collection "{name}".')
        return cid

    def local_account_written(self) -> bool:
        return (self.broker_dir / LOCAL_FILE).exists() and (self.secrets_dir / "client_id").exists()

    def write_account_file(self, rotate: bool = False) -> None:
        """The local account: broker/local-account.json and secrets/local/client_id (the step "account_file")."""
        path = self.broker_dir / LOCAL_FILE
        id_path = self.secrets_dir / "client_id"
        scope = f"collection:{self.state['AGENT_COLLECTION']}"
        if not rotate:
            conflict = False
            if id_path.exists():
                try:
                    have = read_client_id(self.secrets_dir.parent, LOCAL_SLUG, LOCAL_FILE)
                    conflict = have != self.state["CLIENT_ID"]
                except AccountError:
                    conflict = True
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    conflict = conflict or not isinstance(data, dict) or data.get("scope") != scope
                except (OSError, UnicodeDecodeError, ValueError):
                    conflict = True
            if conflict:
                raise SetupError(f"{path.name} or secrets/local/client_id names other ids than local/state. "
                                 "Move them aside, or run local-init --rotate to write them again from "
                                 "local/state.")
            if path.exists() and id_path.exists():
                print(f"Kept {path.name}.")
                return
        _write_secret(id_path, self.state["CLIENT_ID"])
        _write_secret(path, json.dumps({"scope": scope}, indent=2))
        print(f"Wrote {path.name} and secrets/local/client_id.")

    def run(self, owner: str, wait: float) -> int:
        recorded = self.state.get("OWNER_EMAIL")
        if recorded and recorded != owner:
            raise SetupError(f"make vault-init already invited {recorded}. Run it with that address "
                             f"(VAULT_OWNER_EMAIL={recorded}).")
        if self.done("confirmed") and not self.rotate:
            # Set up before the broker read the local account from these files.
            if not self.local_account_written():
                self.write_account_file()
            print("Local mode steps 2 to 5 are already done.")
            return 0
        password = self.agent_password()
        try:
            if not self.done("registered"):
                self.register(password)
            if not self.done("api_key"):
                client_id, secret = vw_register.api_key(self.server, self.get_token(password), LOCAL_AGENT_EMAIL,
                                                        password)
                try:
                    _write_secret(self.secrets_dir / "client_secret", secret)
                finally:
                    secret = None
                self.record("api_key", CLIENT_ID=client_id)
                print(f"Stored the API key of {LOCAL_AGENT_EMAIL}.")
            if not self.done("org"):
                token = self.get_token(password)
                org = self.existing_org(token)
                if org is not None:
                    # A run that created it stopped before recording it.
                    self.state["ORG_ID"] = org
                    coll = self.collection_named(password, DEFAULT_COLLECTION)
                    print(f'Reusing the organization "{LOCAL_ORG_NAME}".')
                else:
                    keys = self.keys or vw_register.public_key(self.server, token)
                    org, coll = vw_register.create_org(self.server, token, keys, LOCAL_ORG_NAME, DEFAULT_COLLECTION)
                    print(f'Created the organization "{LOCAL_ORG_NAME}" with the collection "{DEFAULT_COLLECTION}".')
                self.record("org", ORG_ID=org, AGENT_COLLECTION=coll)
            if not self.done("account_file") or self.rotate or not self.local_account_written():
                self.write_account_file(self.rotate)
                self.record("account_file")
            if self.done("confirmed"):
                return 0
            if not self.done("invited"):
                if self.member(password, owner) is None:
                    vw_register.invite_member(self.server, self.get_token(password), self.state["ORG_ID"], owner,
                                              TYPE_OWNER, [])
                    print(f"Invited {owner} to the organization as an Owner.")
                self.record("invited", OWNER_EMAIL=owner)
            return self.confirm_owner(password, owner, wait)
        except RegistrationFailed:
            return EXIT_FAILED
        finally:
            password = None

    def confirm_owner(self, password: str, owner: str, wait: float) -> int:
        print(f"Now register {owner} in the web vault: open {self.signup}, choose Create account and use that "
              "address. You choose its master password; the stack never sees or stores it. "
              f"Waiting up to {_duration(wait)}.")
        deadline = time.monotonic() + wait
        while True:
            m = self.member(password, owner)
            status = m.get("status") if m else None
            if status == MEMBER_CONFIRMED:
                break
            if status == MEMBER_ACCEPTED and isinstance(m.get("id"), str) and ID_RE.fullmatch(m["id"]):
                bw = self.bw_login(password)
                try:
                    bw.run(["sync"])
                    bw.run(["confirm", "org-member", m["id"], "--organizationid", self.state["ORG_ID"]])
                except BwError as exc:
                    raise SetupError(f"bw confirm org-member failed ({exc.error_class}).") from None
                print(f"Confirmed {owner} in the organization.")
                break
            if time.monotonic() >= deadline:
                print(f"{owner} has not registered yet. Register at {self.signup}, then run make vault-init "
                      "again; it picks up from here.")
                return EXIT_WAITING
            time.sleep(self.poll)
        self.record("confirmed")
        return 0


def local_init_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bitwarden-setup")
    parser.add_argument("command", choices=("local-init",))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--server")
    parser.add_argument("--signup-url", default=LOCAL_SIGNUP_URL)
    parser.add_argument("--wait", type=float, default=600.0)
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args(argv)
    owner = args.owner.strip().lower()
    if not EMAIL_RE.fullmatch(owner):
        print("--owner must be an email address.")
        return EXIT_FAILED
    server = _check_server(args.server or LOCAL_SERVER)
    if server is None or (server != LOCAL_SERVER and not _is_loopback_url(server)):
        print(f"--server must be {LOCAL_SERVER}.")
        return EXIT_FAILED
    run = LocalInit(server, args.signup_url, args.rotate)
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run.run(owner, max(args.wait, 0.0))
    except SetupError as exc:
        print(exc)
    except vw_register.VwError as exc:
        print(f"Vaultwarden refused a step ({exc}). make vault-init picks up from the last finished step.")
    except OSError as exc:
        print(f"Could not write a file ({exc.strerror or 'error'}).")
    except EOFError:
        print("\nNo input; stopped.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_INTERRUPTED
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            run.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return EXIT_FAILED


def main(argv: list[str]) -> int:
    if argv and argv[0] in LOCAL_COMMANDS:
        return local_main(argv)
    if argv and argv[0] == "local-init":
        return local_init_main(argv)
    args = parse_args(argv)
    try:
        account = load_account(
            args.slug,
            Path(os.environ.get("BITWARDEN_INSTANCES_DIR") or "/store/instances"),
            Path(os.environ.get("BITWARDEN_BROKER_DIR") or "/broker"),
            Path(os.environ.get("BITWARDEN_SECRETS_DIR") or "/secrets"),
        )
    except AccountError as exc:
        print(exc)
        return EXIT_FAILED
    args.server = _check_server(args.server or account.server)
    if args.server is None:
        print("--server must be an https:// URL.")
        return EXIT_FAILED
    # Only a test serves a loopback URL from inside this container.
    if args.server != account.server and not _is_loopback_url(args.server):
        print(f"--server must match the server of {args.slug} ({account.path.name}).")
        return EXIT_FAILED
    refusal = preflight(args, account)
    if refusal:
        print(refusal)
        return EXIT_FAILED

    needs_token = args.command == "role" or (args.command == "create" and account.scope_kind == "collection")
    holder: list[str] = []
    run = bw = appdata = None
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        appdata = tempfile.mkdtemp(dir="/tmp", prefix="bitwarden-setup-")
        bw = UserBw(os.environ.get("BITWARDEN_BW") or "bw", appdata)
        bw.configure(args.server)
        holder = ask_password(bw, args.email)
        run = Run(args, account, bw)
        if needs_token:
            try:
                run.token = vw_register.token(args.server, args.email, holder[0])
            except vw_register.TokenError as exc:
                run.token_error = str(exc)
        holder.clear()
        code = getattr(run, "cmd_" + args.command.replace("-", "_"))()
        if code == 0 and args.command == "role" and account.mode == "local":
            local_dir = Path(os.environ.get("BITWARDEN_LOCAL_DIR") or LOCAL_DIR)
            if local_dir.is_dir():
                try:
                    mark_role_done(local_dir)
                except (OSError, SetupError):
                    print("Could not record the finished step in local/state; make vault-init runs role again.")
        return code
    except PasswordTriesExhausted:
        print(f"{PASSWORD_TRIES} tries failed. Nothing was changed.")
        return EXIT_PASSWORD
    except SetupError as exc:
        print(exc)
        return EXIT_FAILED
    except EOFError:
        print("\nNo input; stopped.")
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_INTERRUPTED
    finally:
        holder.clear()
        if run is not None:
            run.token = None
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if bw is not None:
                bw.close()
        finally:
            if appdata is not None:
                shutil.rmtree(appdata, ignore_errors=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
