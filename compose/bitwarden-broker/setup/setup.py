#!/usr/bin/env python3
"""bitwarden-setup: the steps that need the user's own vault key, in one run.

    setup.py <command> --slug S [--server URL] --email EMAIL [command flags]

Commands:

``create``           Creates the account's folder (``folder:`` scope) or collection
                     (``collection:`` scope, ``--agent-email`` required). For a
                     collection id that does not exist yet, ``--org ORG`` and
                     ``--name NAME`` (default Agent) say where to create it, and
                     the new id replaces ``BW_SCOPE`` in the account file. The
                     agent member is invited if it is not one yet and gets Manage
                     on the collection. ``local`` accounts are refused here.
``seed``             Asks for sites and moves the matching items the user picks.
``grant``            ``--site H`` and/or ``--item ID``; moves one or more items and
                     prints what moved. The broker's next sync marks the request done.
``role``             Changes the agent member to the User role with Manage on the
                     scope collection and view on "Stack secrets" (Local mode step 6).
                     ``--agent-email`` defaults to agent@vault.localhost for ``local``.
``list-unassigned``  Organization items in no collection, by name and URI host.

The account file is ``$BITWARDEN_ACCOUNTS_DIR/<slug>.env``. ``--server`` is the
user's server and defaults to the account's ``BW_SERVER``; it must be https,
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
import shutil
import signal
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vw_register  # noqa: E402
from broker.accounts import ID_RE, Account, AccountError, parse_file  # noqa: E402
from broker.bwcli import BwError, classify, run_bw, sanitize_item  # noqa: E402
from broker.vault import fill_origins  # noqa: E402

NOTICE = ("Moving an item into an organization collection transfers it from your personal vault to the "
          "organization. Members with access to the collection can see it.")
PASSWORD_TRIES = 3
EXIT_FAILED, EXIT_PASSWORD, EXIT_INTERRUPTED = 1, 2, 130
LOCAL_AGENT_EMAIL = "agent@vault.localhost"
STACK_SECRETS = "Stack secrets"
DEFAULT_COLLECTION = "Agent"
MEMBER_INVITED, MEMBER_ACCEPTED = 0, 1
TYPE_USER = 2
API_TIMEOUT_SECONDS = 30
COMMANDS = ("create", "seed", "grant", "role", "list-unassigned")


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
    """A host from what the user typed: a bare host, host:port or a URL. None if it is not one."""
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        host = urlsplit(value if "://" in value else f"https://{value}").hostname
    except ValueError:
        return None
    host = (host or "").rstrip(".")
    if not host or any(c.isspace() for c in host):
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


def _matching_uri(item: dict, host: str) -> str | None:
    for item_host, uri in _hosts(item):
        if item_host == host or item_host.endswith("." + host):
            return uri
    return None


def match_items(items: list[dict], host: str) -> list:
    """Items with a fill URI whose host is ``host`` or ends with ``.`` + ``host``."""
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
        error_class, _ = classify(err)
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

    def login(self, server: str, email: str, holder: list[str]) -> None:
        """Log in with ``holder[0]``; raises BwError("auth") on a wrong password."""
        if self._session is None:
            try:
                self.run(["config", "server", server])
            except BwError:
                pass  # a fresh data dir; login reports a real problem
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

    def api(self, method: str, path: str, body: dict, what: str) -> None:
        """Call the organization API with the token; raises SetupError naming ``what`` and the status."""
        _, api_base = vw_register.endpoints(self.args.server)
        req = urllib.request.Request(f"{api_base}{path}", data=json.dumps(body).encode("utf-8"), method=method,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        failure = None
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS, context=vw_register.ssl_context()):
                pass
        except urllib.error.HTTPError as exc:
            failure = f"HTTP {exc.code}"
            exc.close()
        except (urllib.error.URLError, OSError):
            failure = "unreachable"
        finally:
            req = None
        if failure is not None:
            raise SetupError(f"{what} refused: {failure}")

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

    def cmd_grant(self) -> int:
        target = self.target()
        items = self.items()
        if self.args.item:
            item = next((i for i in items if i["id"] == self.args.item), None)
            if item is None:
                raise SetupError("No login item with that id in your vault.")
            if self.args.site:
                host = normalize_host(self.args.site)
                if host is None or not match_items([item], host):
                    raise SetupError(f'"{item["name"]}" has no URI for {self.args.site}; nothing moved.')
            if self.in_target(item, target):
                print(f'"{item["name"]}" is already in {target.label}.')
            elif not self.move(item, target):
                return EXIT_FAILED
        else:
            site = self.args.site or input("Site to give the agent: ").strip()
            host = normalize_host(site)
            if host is not None and not match_items([i for i in items if not self.in_target(i, target)], host):
                print(f"No login items for {host} outside {target.label}.")
                return EXIT_FAILED
            self.offer(items, target, site)
        print("The broker marks a pending request for this site done on its next sync.")
        return 0

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
                    self.api("POST", f"/organizations/{org}/users/invite",
                             {"emails": [agent_email], "groups": [], "type": TYPE_USER, "collections": [],
                              "permissions": {}}, "invite")
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
        """Point the account file's BW_SCOPE at the new collection, keeping every other line."""
        path = self.account.path
        line = f"BW_SCOPE=collection:{cid}"
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
            out, done = [], False
            for old in lines:
                if old.partition("=")[0].strip() == "BW_SCOPE" and not old.lstrip().startswith("#"):
                    out.append(line + ending)
                    done = True
                else:
                    out.append(old)
            if not done:
                if out and not out[-1].endswith("\n"):
                    out[-1] += ending
                out.append(line + ending)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    f.write("".join(out))
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            print(f"Could not update {path.name}; set {line} in it.")
            return
        print(f"Set {line} in {path.name}.")

    def cmd_role(self) -> int:
        agent_email = self.args.agent_email or LOCAL_AGENT_EMAIL
        agent_coll = self.scope_collection()
        if agent_coll is None:
            raise SetupError(f"Collection {self.account.scope_value} is not visible to {self.args.email}.")
        org = agent_coll.get("organizationId")
        colls = self.collections()
        secrets_scope = self.account.secrets_item_scope
        if secrets_scope and secrets_scope[0] == "collection":
            stack = next((c for c in colls if c["id"] == secrets_scope[1]), None)
        else:
            stack = next((c for c in colls if c.get("organizationId") == org and c.get("name") == STACK_SECRETS), None)
        member = self.member(org, agent_email)
        if member is None:
            raise SetupError(f"{agent_email} is not a member of the organization.")
        access = [{"id": agent_coll["id"], "readOnly": False, "hidePasswords": False, "manage": True}]
        if stack is not None:
            access.append({"id": stack["id"], "readOnly": True, "hidePasswords": False, "manage": False})
        names = f'Manage on "{agent_coll.get("name")}"' + (f' and view on "{stack.get("name")}"' if stack else "")
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
    parser.add_argument("--site")
    parser.add_argument("--item")
    return parser.parse_args(argv)


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
    if parts.scheme == "http":
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_loopback:
                return server.rstrip("/")
        except ValueError:
            pass
    return None


def preflight(args, account: Account) -> str | None:
    """A reason to refuse before asking for the password, or None."""
    if args.item and not ID_RE.fullmatch(args.item):
        return "--item is not a Bitwarden item id."
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
    if args.command in ("seed", "grant") and kind == "all":
        return "This account's scope is all; there is no folder or collection to move items into."
    if args.command == "role" and not args.agent_email and account.mode != "local":
        return "role needs --agent-email for an account that is not local."
    return None


def ask_password(bw: UserBw, server: str, email: str) -> list[str]:
    """Log bw in, three tries. Returns a one-element list holding the password, for the token call."""
    for _ in range(PASSWORD_TRIES):
        holder = [getpass.getpass(f"Master password for {email}: ")]
        if not holder[0]:
            print("No password entered.")
            continue
        try:
            bw.login(server, email, holder)
            return holder
        except BwError as exc:
            holder.clear()
            if exc.error_class != "auth":
                raise SetupError(f"bw login failed ({exc.error_class}).") from None
            print("Bitwarden did not accept that email and password.")
    raise PasswordTriesExhausted


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    accounts_dir = Path(os.environ.get("BITWARDEN_ACCOUNTS_DIR") or "/accounts")
    try:
        account = parse_file(accounts_dir / f"{args.slug}.env")
    except AccountError as exc:
        print(exc)
        return EXIT_FAILED
    args.server = _check_server(args.server or account.server)
    if args.server is None:
        print("--server must be an https:// URL.")
        return EXIT_FAILED
    refusal = preflight(args, account)
    if refusal:
        print(refusal)
        return EXIT_FAILED

    needs_token = args.command == "role" or (args.command == "create" and account.scope_kind == "collection")
    appdata = tempfile.mkdtemp(dir="/tmp", prefix="bitwarden-setup-")
    bw = UserBw(os.environ.get("BITWARDEN_BW") or "bw", appdata)
    previous = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    holder: list[str] = []
    run = None
    try:
        holder = ask_password(bw, args.server, args.email)
        run = Run(args, account, bw)
        if needs_token:
            try:
                run.token = vw_register.token(args.server, args.email, holder[0])
            except vw_register.TokenError as exc:
                run.token_error = str(exc)
        holder.clear()
        return getattr(run, "cmd_" + args.command.replace("-", "_"))()
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
            bw.close()
        finally:
            shutil.rmtree(appdata, ignore_errors=True)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
