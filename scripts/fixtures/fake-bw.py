#!/usr/bin/env python3
"""A stand-in for the bw CLI, for tests. Synthetic data only.

FAKE_BW_VAULT names a JSON file: ``items``, ``folders``, ``collections``,
``master_password``, optional ``client_secret`` (checked on login when set),
``email``, ``rate_limit_after`` (server calls allowed before every further one
gets a 429), ``retry_after`` (seconds printed with the 429), ``stderr_prefix``
(printed before every error, for Node warning lines), ``hang_sync`` (sync
writes this text to stdout and stderr, then sleeps 30 s) and ``truncate_lists``
(list output is cut short, so it is not valid JSON), ``edit_gate`` (a path:
edit waits until that file exists, up to 30 s) and ``refuse_edit`` (edit fails
with this text), ``members`` (organization members for ``list org-members``),
``user_email`` and ``user_password`` (the account ``login <email> --passwordenv``
signs in to, as bitwarden-setup does), ``refuse_config`` (config server fails
with this text) and ``ignore_config`` (config server succeeds but keeps the old
server, so ``status`` shows it). Commands that write to the vault rewrite that file. Per-data-dir state (server, login,
session keys, server call count) lives in
``$BITWARDENCLI_APPDATA_DIR/fake-bw-state.json``.

FAKE_BW_LOG, when set, gets one JSON line per call: argv with session keys and
encoded JSON masked, the names (never values) of the secret variables and of
every variable in the environment, the data dir, and whether BW_NOINTERACTION
was set. FAKE_BW_STDERR_LOG, when set, gets a copy of everything written to
stderr, so a test can show a leak was really offered before checking it was
dropped.

Exit codes and stderr follow bw's shape: 0 and output on stdout, or 1 and a
one-line message on stderr. A failed unlock echoes the password it was given
into stderr, so tests can prove the broker drops bw's text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import time
import uuid
from pathlib import Path

SECRET_ENV = ("BW_CLIENTID", "BW_CLIENTSECRET", "BW_PW", "BW_PASSWORD", "BW_SESSION")
VALUE_FLAGS = ("--session", "--passwordenv", "--folderid", "--collectionid", "--organizationid", "--search")
SERVER_CMDS = {"login", "sync", "create", "edit", "move", "confirm"}


class Fail(Exception):
    pass


def log(argv: list[str]) -> None:
    path = os.environ.get("FAKE_BW_LOG")
    if not path:
        return
    masked, hide_next = [], False
    for arg in argv:
        if hide_next:
            masked.append("<masked>")
            hide_next = False
            continue
        hide_next = arg == "--session"
        masked.append("<encoded>" if len(masked) >= 2 and masked[0] in ("create", "edit", "move") and _is_encoded(arg)
                      else arg)
    entry = {
        "argv": masked,
        "env": sorted(k for k in SECRET_ENV if k in os.environ),
        "env_names": sorted(os.environ),
        "appdata": os.environ.get("BITWARDENCLI_APPDATA_DIR"),
        "nointeraction": os.environ.get("BW_NOINTERACTION") == "true",
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def emit_stderr(message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()
    if os.environ.get("FAKE_BW_STDERR_LOG"):
        with open(os.environ["FAKE_BW_STDERR_LOG"], "a", encoding="utf-8") as f:
            f.write(message)


def _is_encoded(arg: str) -> bool:
    try:
        json.loads(base64.b64decode(arg, validate=True))
        return True
    except ValueError:
        return False


def parse(argv: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    pos, opts, i = [], {}, 0
    while i < len(argv):
        arg = argv[i]
        if arg in VALUE_FLAGS and i + 1 < len(argv):
            opts[arg] = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            opts[arg] = True
        else:
            pos.append(arg)
        i += 1
    return pos, opts


class Ctx:
    def __init__(self, opts):
        self.opts = opts
        self.vault_path = Path(os.environ["FAKE_BW_VAULT"])
        self.vault = json.loads(self.vault_path.read_text(encoding="utf-8"))
        appdata = os.environ.get("BITWARDENCLI_APPDATA_DIR")
        if not appdata:
            raise Fail("fake-bw: BITWARDENCLI_APPDATA_DIR is required")
        self.state_path = Path(appdata) / "fake-bw-state.json"
        self.state = (json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists()
                      else {"server": None, "logged_in": False, "sessions": [], "server_calls": 0})

    def save(self, vault: bool = False) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        if vault:
            tmp = self.vault_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.vault), encoding="utf-8")
            os.replace(tmp, self.vault_path)

    def server_call(self) -> None:
        self.state["server_calls"] += 1
        self.save()
        limit = self.vault.get("rate_limit_after")
        if limit is not None and self.state["server_calls"] > limit:
            after = self.vault.get("retry_after")
            raise Fail("Too Many Requests (429)" + (f". Retry-After: {after}" if after is not None else ""))

    def need_session(self) -> None:
        if not self.state["logged_in"]:
            raise Fail("You are not logged in.")
        key = self.opts.get("--session") or os.environ.get("BW_SESSION")
        if key not in self.state["sessions"]:
            raise Fail("Vault is locked.")

    def payload(self, pos: list[str], index: int):
        raw = pos[index] if len(pos) > index else sys.stdin.read().strip()
        try:
            return json.loads(base64.b64decode(raw, validate=True))
        except ValueError:
            raise Fail("Error parsing the encoded request data.") from None

    def find(self, kind: str, ident: str) -> dict:
        for obj in self.vault.get(kind, []):
            if obj.get("id") == ident:
                return obj
        raise Fail("Not found.")


def totp(seed: str, now: float | None = None) -> str:
    key = base64.b32decode(seed.replace(" ", "").upper() + "=" * (-len(seed) % 8))
    counter = int((now or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)


def config_server(ctx, pos):
    if ctx.state["logged_in"]:
        raise Fail("Logout required before server config update.")
    if ctx.vault.get("refuse_config"):
        raise Fail(ctx.vault["refuse_config"])
    if ctx.vault.get("ignore_config"):
        return "Saved setting `config`."
    ctx.state["server"] = pos[2]
    ctx.save()
    return "Saved setting `config`."


def status(ctx, pos):
    sessions = ctx.state["sessions"]
    key = ctx.opts.get("--session") or os.environ.get("BW_SESSION")
    state = "unlocked" if key in sessions else ("locked" if ctx.state["logged_in"] else "unauthenticated")
    return json.dumps({"serverUrl": ctx.state["server"], "status": state})


def login(ctx, pos):
    if ctx.state["logged_in"]:
        raise Fail(f"You are already logged in as {ctx.vault.get('email', 'agent@example.com')}.")
    if len(pos) >= 2 and isinstance(ctx.opts.get("--passwordenv"), str):
        return password_login(ctx, pos)
    if ctx.opts.get("--apikey") is not True:
        raise Fail("fake-bw: only --apikey and <email> --passwordenv login are implemented")
    cid, csecret = os.environ.get("BW_CLIENTID"), os.environ.get("BW_CLIENTSECRET")
    if not cid or not csecret:
        raise Fail("fake-bw: BW_CLIENTID and BW_CLIENTSECRET are required with BW_NOINTERACTION")
    ctx.server_call()
    if "client_secret" in ctx.vault and csecret != ctx.vault["client_secret"]:
        raise Fail("client_id or client_secret is incorrect. Try again.")
    ctx.state["logged_in"] = True
    ctx.save()
    return "You are logged in!"


def password_login(ctx, pos):
    password = os.environ.get(ctx.opts["--passwordenv"])
    if password is None:
        raise Fail("Master password is required.")
    ctx.server_call()
    if pos[1] != ctx.vault.get("user_email") or password != ctx.vault.get("user_password"):
        raise Fail(f"Username or password is incorrect. Try again. (got {password})")
    key = base64.b64encode(secrets.token_bytes(64)).decode("ascii")
    ctx.state["logged_in"] = True
    ctx.state["sessions"].append(key)
    ctx.save()
    return key if ctx.opts.get("--raw") else f'You are logged in!\n\nexport BW_SESSION="{key}"'


def unlock(ctx, pos):
    if not ctx.state["logged_in"]:
        raise Fail("You are not logged in.")
    name = ctx.opts.get("--passwordenv")
    password = os.environ.get(name) if isinstance(name, str) else None
    if password is None:
        raise Fail("Master password is required.")
    if password != ctx.vault.get("master_password"):
        raise Fail(f"Invalid master password. (got {password})")
    key = base64.b64encode(secrets.token_bytes(64)).decode("ascii")
    ctx.state["sessions"].append(key)
    ctx.save()
    return key if ctx.opts.get("--raw") else f'Your vault is now unlocked!\n\nexport BW_SESSION="{key}"'


def lock(ctx, pos):
    ctx.state["sessions"] = []
    ctx.save()
    return "Your vault is locked."


def logout(ctx, pos):
    if not ctx.state["logged_in"]:
        raise Fail("You are not logged in.")
    ctx.state.update(logged_in=False, sessions=[])
    ctx.save()
    return "You have logged out."


def sync(ctx, pos):
    ctx.need_session()
    ctx.server_call()
    if ctx.vault.get("hang_sync"):
        sys.stdout.write(ctx.vault["hang_sync"] + "\n")
        sys.stdout.flush()
        emit_stderr(ctx.vault["hang_sync"] + "\n")
        time.sleep(30)
    return "Syncing complete."


def list_cmd(ctx, pos):
    ctx.need_session()
    kind = {"items": "items", "folders": "folders", "org-collections": "collections", "collections": "collections",
            "org-members": "members"}.get(pos[1])
    if kind is None:
        raise Fail("Unknown object.")
    rows = ctx.vault.get(kind, [])
    if (folder := ctx.opts.get("--folderid")) is not None:
        rows = [r for r in rows if r.get("folderId") == folder]
    if (coll := ctx.opts.get("--collectionid")) is not None:
        rows = [r for r in rows if coll in (r.get("collectionIds") or [])]
    if (org := ctx.opts.get("--organizationid")) is not None:
        rows = [r for r in rows if r.get("organizationId") == org]
    return json.dumps(rows)[:-3] if ctx.vault.get("truncate_lists") else json.dumps(rows)


def get_cmd(ctx, pos):
    ctx.need_session()
    if pos[1] == "org-collection":
        return json.dumps(ctx.find("collections", pos[2]))
    item = ctx.find("items", pos[2])
    login_data = item.get("login") or {}
    if pos[1] == "item":
        return json.dumps(item)
    if pos[1] == "password":
        if not login_data.get("password"):
            raise Fail("No password available for this login.")
        return login_data["password"]
    if pos[1] == "totp":
        if not login_data.get("totp"):
            raise Fail("No TOTP available for this login.")
        return totp(login_data["totp"])
    raise Fail("Unknown object.")


def create_cmd(ctx, pos):
    ctx.need_session()
    ctx.server_call()
    kind = {"folder": "folders", "org-collection": "collections", "item": "items"}.get(pos[1])
    if kind is None:
        raise Fail("Unknown object.")
    obj = dict(ctx.payload(pos, 2), id=str(uuid.uuid4()))
    if kind == "collections":
        obj.setdefault("organizationId", ctx.opts.get("--organizationid"))
    ctx.vault.setdefault(kind, []).append(obj)
    ctx.save(vault=True)
    return json.dumps(obj)


def edit_cmd(ctx, pos):
    ctx.need_session()
    ctx.server_call()
    gate = ctx.vault.get("edit_gate")
    if gate:
        deadline = time.monotonic() + 30
        while not os.path.exists(gate) and time.monotonic() < deadline:
            time.sleep(0.05)
        ctx = Ctx(ctx.opts)  # the test may have changed the vault while edit waited
    if ctx.vault.get("refuse_edit"):
        raise Fail(ctx.vault["refuse_edit"])
    if pos[1] == "item":
        obj = ctx.find("items", pos[2])
        obj.update({k: v for k, v in ctx.payload(pos, 3).items() if k != "id"})
    elif pos[1] == "item-collections":
        obj = ctx.find("items", pos[2])
        obj["collectionIds"] = list(ctx.payload(pos, 3))
    elif pos[1] == "org-collection":
        obj = ctx.find("collections", pos[2])
        obj.update({k: v for k, v in ctx.payload(pos, 3).items() if k != "id"})
    else:
        raise Fail("Unknown object.")
    ctx.save(vault=True)
    return json.dumps(obj)


def move_cmd(ctx, pos):
    ctx.need_session()
    ctx.server_call()
    obj = ctx.find("items", pos[1])
    obj["organizationId"] = pos[2]
    obj["collectionIds"] = list(ctx.payload(pos, 3))
    ctx.save(vault=True)
    return json.dumps(obj)


def confirm_cmd(ctx, pos):
    ctx.need_session()
    ctx.server_call()
    if pos[1] != "org-member" or not isinstance(ctx.opts.get("--organizationid"), str):
        raise Fail("--organizationid <organizationid> required.")
    ctx.vault.setdefault("confirmed", []).append({"id": pos[2], "organizationId": ctx.opts["--organizationid"]})
    ctx.save(vault=True)
    return ""


# (command, subcommand or None, minimum positional count) -> handler
COMMANDS = {
    ("config", "server", 3): config_server,
    ("status", None, 1): status,
    ("login", None, 1): login,
    ("unlock", None, 1): unlock,
    ("lock", None, 1): lock,
    ("logout", None, 1): logout,
    ("sync", None, 1): sync,
    ("list", None, 2): list_cmd,
    ("get", None, 3): get_cmd,
    ("create", None, 2): create_cmd,
    ("edit", None, 3): edit_cmd,
    ("move", None, 3): move_cmd,
    ("confirm", None, 3): confirm_cmd,
}


def main(argv: list[str]) -> int:
    log(argv)
    pos, opts = parse(argv)
    handler = None
    for (cmd, sub, minimum), fn in COMMANDS.items():
        if pos[:1] == [cmd] and (sub is None or pos[1:2] == [sub]) and len(pos) >= minimum:
            handler = fn
            break
    try:
        if handler is None:
            raise Fail(f"fake-bw: unsupported command {' '.join(pos[:2])}")
        out = handler(Ctx(opts), pos)
    except Fail as exc:
        prefix = ""
        try:
            prefix = json.loads(Path(os.environ["FAKE_BW_VAULT"]).read_text(encoding="utf-8")).get("stderr_prefix") or ""
        except (KeyError, OSError, ValueError):
            pass
        emit_stderr(prefix + str(exc) + "\n")
        return 1
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
