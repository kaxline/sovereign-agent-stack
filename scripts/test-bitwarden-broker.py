#!/usr/bin/env python3
"""Checks for the Bitwarden broker's accounts, bw wrapper and item listing.

Stdlib only. Runs scripts/fixtures/fake-bw.py as the bw executable against a
synthetic vault (example.com sites, generated passwords). Every test searches
the captured log records, exception text, fake-bw log and files the broker
writes for the run's synthetic secrets.

The CDP fill tests need websockets for the fake DevTools server, so they run
inside the pinned Hermes image (scripts/lib/hermes_image.py) with scripts/ and
compose/bitwarden-broker/ mounted read-only, and skip when the image is absent.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import secrets
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
from broker import accounts, bwcli, cdp, origins, vault  # noqa: E402

FAKE_BW = ROOT / "scripts" / "fixtures" / "fake-bw.py"

MASTER = secrets.token_urlsafe(18)
CLIENT_SECRET = secrets.token_urlsafe(24)
WRONG_PASSWORD = "wrong-" + secrets.token_urlsafe(18)
ITEM_PASSWORDS = {name: secrets.token_urlsafe(16) for name in "ABCDEFGH"}
ITEM_PASSWORDS["H"] += "\n"  # a password that really ends in a newline
LEAKY_ENV = "leaky-" + secrets.token_urlsafe(18)
TOTP_SEED = base64.b32encode(secrets.token_bytes(10)).decode("ascii")
SECRETS = [MASTER, CLIENT_SECRET, WRONG_PASSWORD, TOTP_SEED, LEAKY_ENV, *(v.strip() for v in ITEM_PASSWORDS.values())]
BROKER_DIR = str(ROOT / "compose" / "bitwarden-broker" / "broker")
NODE_WARNING = (
    "(node:4014290) [DEP0040] DeprecationWarning: The `punycode` module is deprecated. "
    "Please use a userland alternative instead.\n"
    "(Use `node --trace-deprecation ...` to show where the warning was created)\n"
)

FOLDER_AGENT = "0f0a0000-0000-4000-8000-00000000000a"
FOLDER_OTHER = "0f0b0000-0000-4000-8000-00000000000b"
COLLECTION = "0c0c0000-0000-4000-8000-00000000000c"
ORG = "0e0e0000-0000-4000-8000-00000000000e"


def item_id(name: str) -> str:
    return f"{name.lower() * 8}-0000-4000-8000-000000000000"


def fail(msg: str) -> None:
    # sys.__stderr__, because captured() redirects sys.stderr.
    print(f"FAIL {msg}", file=sys.__stderr__)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def login_item(name, folder=None, collections=(), uris=(), totp=None, kind=1):
    item = {
        "object": "item",
        "id": item_id(name),
        "organizationId": ORG if collections else None,
        "folderId": folder,
        "collectionIds": list(collections),
        "type": kind,
        "name": f"Item {name}",
        "notes": None,
        "creationDate": "2026-11-01T00:00:00.000Z",
    }
    if kind == 1:
        item["login"] = {
            "username": f"user-{name.lower()}@example.com",
            "password": ITEM_PASSWORDS[name],
            "totp": totp,
            "uris": None if uris is None else [{"match": m, "uri": u} for u, m in uris],
        }
    return item


def vault_data(**extra) -> dict:
    data = {
        "email": "agent@example.com",
        "client_secret": CLIENT_SECRET,
        "master_password": MASTER,
        "folders": [{"id": FOLDER_AGENT, "name": "Agent"}, {"id": FOLDER_OTHER, "name": "Other"}],
        "collections": [{"id": COLLECTION, "name": "Agent", "organizationId": ORG}],
        "items": [
            login_item("A", FOLDER_AGENT, uris=[("https://login.example.com/signin", None)], totp=TOTP_SEED),
            login_item("B", FOLDER_OTHER, uris=[("https://other.example.com", None)]),
            login_item("C", None, collections=[COLLECTION], uris=[("https://shop.example.com", 0)]),
            # No http(s) URI at all.
            login_item("D", FOLDER_AGENT, uris=[("androidapp://com.example.app", None), ("example.net", None)]),
            # Review Focus 1: only the last URI is a fill target.
            login_item("E", FOLDER_AGENT, uris=[
                ("androidapp://com.example.mixed", None),
                ("https://never.example.com", 5),
                ("mixed.example.org", None),
                ("https://good.example.com:443/login", None),
            ]),
            login_item("F", FOLDER_AGENT, kind=2),
            login_item("G", FOLDER_AGENT, uris=None),
            login_item("H", FOLDER_OTHER, uris=[]),
        ],
        "rate_limit_after": None,
        "retry_after": None,
    }
    data.update(extra)
    return data


class Run:
    """A temp layout (accounts, bw data, secrets, fake vault) plus capture of everything emitted."""

    def __init__(self, tmp: Path, **vault_extra):
        self.tmp = tmp
        self.data_root = tmp / "bw"
        self.secrets_root = tmp / "secrets"
        self.vault_file = tmp / "vault.json"
        self.log_file = tmp / "fake-bw.log"
        self.stderr_file = tmp / "fake-bw-stderr.log"
        self.vault_file.write_text(json.dumps(vault_data(**vault_extra)), encoding="utf-8")
        self.extra_env = {"FAKE_BW_VAULT": str(self.vault_file), "FAKE_BW_LOG": str(self.log_file),
                          "FAKE_BW_STDERR_LOG": str(self.stderr_file)}
        self.errors: list[str] = []

    def account(self, slug, scope="folder:Agent", unlock="stored", master=MASTER):
        kind, _, value = scope.partition(":")
        secret_dir = self.secrets_root / slug
        secret_dir.mkdir(parents=True, exist_ok=True)
        (secret_dir / "client_secret").write_text(CLIENT_SECRET + "\n", encoding="utf-8")
        if unlock == "stored":
            (secret_dir / "master_password").write_text(master + "\n", encoding="utf-8")
        return accounts.Account(
            slug=slug, mode="cloud", server="https://vault.bitwarden.com", scope_kind=kind,
            scope_value=value or None, unlock=unlock, client_id="user.00000000-0000-4000-8000-000000000001",
            secrets_item_scope=None, path=self.tmp / f"{slug}.env",
        )

    def bw(self, account) -> bwcli.Bw:
        return bwcli.Bw(account, self.data_root, self.secrets_root, bw_path=str(FAKE_BW), extra_env=self.extra_env)

    def offered_stderr(self) -> str:
        return self.stderr_file.read_text(encoding="utf-8") if self.stderr_file.exists() else ""

    def log_lines(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def record(self, exc: BaseException) -> None:
        self.errors.append(str(exc))
        self.errors.append(repr(exc))
        self.errors.append("".join(traceback.format_exception(exc)))
        self.errors.append(reachable_text(exc))

    def expect_clean(self, extra_text: str = "", extra_secrets=()) -> None:
        texts = [extra_text, *self.errors]
        if self.log_file.exists():
            texts.append(self.log_file.read_text(encoding="utf-8"))
        for path in self.tmp.rglob("*"):
            # The fake's own state and vault stand in for Bitwarden's servers, and
            # secrets/ holds the inputs.
            if (path.is_file() and path.name not in ("vault.json", "fake-bw-state.json", "fake-bw-stderr.log")
                    and "secrets" not in path.parts):
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        blob = "\n".join(texts)
        for value in (*SECRETS, *extra_secrets):
            expect(value not in blob, "a synthetic secret appeared in captured output")


def reachable_text(exc: BaseException) -> str:
    """repr of everything a handler can reach from ``exc``: the chain, each exception's
    attributes, and the locals of every broker frame in each traceback."""
    out, seen, todo = [], set(), [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(repr(e.args))
        out.append(repr(vars(e)))
        for attr in ("stdout", "stderr", "output", "doc"):
            out.append(repr(getattr(e, attr, None)))
        tb = e.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if frame.f_code.co_filename.startswith(BROKER_DIR):
                out.append(repr(dict(frame.f_locals)))
            tb = tb.tb_next
        todo.extend((e.__context__, e.__cause__))
    return "\n".join(out)


def expect_no_chain(exc: BaseException, msg: str) -> None:
    expect(exc.__context__ is None and exc.__cause__ is None, f"{msg}: BwError has a chained exception")


@contextlib.contextmanager
def captured():
    """Capture stdout, stderr and every log record at DEBUG."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def expect_bw_error(run: Run, fn, error_class: str, msg: str) -> bwcli.BwError:
    try:
        fn()
    except bwcli.BwError as exc:
        run.record(exc)
        expect(exc.error_class == error_class, f"{msg}: got {exc.error_class}, want {error_class}")
        expect(str(exc) == error_class, f"{msg}: BwError text is only its class")
        expect_no_chain(exc, msg)
        return exc
    fail(f"{msg}: no BwError")


def write_env(directory: Path, name: str, text: str, newline: str = "\n") -> None:
    (directory / name).write_bytes(text.replace("\n", newline).encode("utf-8"))


def test_account_file_tolerance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_env(d, "personal.env", (
            "# Personal vault, edited by hand\n"
            'BW_MODE="cloud"   \n'
            "BW_SERVER=https://vault.bitwarden.eu    # EU region\n"
            "\n"
            "  BW_SCOPE = folder:Agent  \n"
            "BW_UNLOCK=stored\n"
            "BW_CLIENTID='user.00000000-0000-4000-8000-000000000001'\n"
            'BW_SECRETS_ITEM_SCOPE="folder:Stack secrets"\n'
        ), newline="\r\n")
        write_env(d, "home-lab.env", (
            "BW_MODE=selfhosted\nBW_SERVER=https://vault.example.com/\nBW_SCOPE=collection:" + COLLECTION + "\n"
            "BW_UNLOCK=manual\nBW_CLIENTID=user.00000000-0000-4000-8000-000000000002\n"
        ))
        write_env(d, "local.env", (
            "BW_MODE=local\nBW_SERVER=https://vaultwarden.vault.internal\nBW_SCOPE=all\n"
            "BW_UNLOCK=stored\nBW_CLIENTID=user.00000000-0000-4000-8000-000000000003\n"
        ))
        good = "BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.com\nBW_UNLOCK=stored\nBW_CLIENTID=user.x\n"
        write_env(d, "auto.env", good.replace("BW_UNLOCK=stored", "BW_UNLOCK=auto"))
        write_env(d, "unknown.env", good + "BW_FOO=1\n")
        write_env(d, "bad_slug.env", good)
        write_env(d, "plain-http.env", good.replace("BW_MODE=cloud", "BW_MODE=selfhosted").replace(
            "https://vault.bitwarden.com", "http://vault.example.com"))
        write_env(d, "wrong-cloud.env", good.replace("https://vault.bitwarden.com", "https://vault.example.com"))
        write_env(d, "noid.env", good.replace("BW_CLIENTID=user.x\n", ""))
        write_env(d, "dup.env", good + "BW_MODE=local\n")
        write_env(d, "bad-scope.env", good + "BW_SCOPE=everything\n")
        write_env(d, "bad-secrets-scope.env", good + "BW_SECRETS_ITEM_SCOPE=all\n")
        write_env(d, "bad-collection.env", good + "BW_SCOPE=collection:--organizationid\n")
        write_env(d, "bad-secrets-collection.env", good + "BW_SECRETS_ITEM_SCOPE='collection:a b'\n")
        write_env(d, "server-query.env", good.replace("BW_MODE=cloud", "BW_MODE=selfhosted").replace(
            "https://vault.bitwarden.com", "https://vault.example.com/?next=x"))
        write_env(d, "server-fragment.env", good.replace("BW_MODE=cloud", "BW_MODE=selfhosted").replace(
            "https://vault.bitwarden.com", "https://vault.example.com/#x"))
        (d / "README").write_text("not an account\n", encoding="utf-8")

        loaded, errors = accounts.load_dir(d)
        by_slug = {a.slug: a for a in loaded}
        expect(sorted(by_slug) == ["home-lab", "local", "personal"], f"loaded {sorted(by_slug)}")

        p = by_slug["personal"]
        expect(p.mode == "cloud" and p.server == "https://vault.bitwarden.eu", "quoted mode, inline comment")
        expect((p.scope_kind, p.scope_value) == ("folder", "Agent"), "spaces around key and value")
        expect(p.unlock == "stored" and p.client_id == "user.00000000-0000-4000-8000-000000000001", "single quotes")
        expect(p.secrets_item_scope == ("folder", "Stack secrets"), f"secrets scope {p.secrets_item_scope}")
        expect(p.path == d / "personal.env", "path")
        h = by_slug["home-lab"]
        expect(h.server == "https://vault.example.com" and h.unlock == "manual", "selfhosted, trailing slash")
        expect((h.scope_kind, h.scope_value) == ("collection", COLLECTION) and h.secrets_item_scope is None, "collection")
        expect((by_slug["local"].scope_kind, by_slug["local"].scope_value) == ("all", None), "all")

        want = {
            "auto.env": "BW_UNLOCK", "unknown.env": "BW_FOO", "bad_slug.env": "slug",
            "plain-http.env": "BW_SERVER", "wrong-cloud.env": "BW_SERVER", "noid.env": "BW_CLIENTID",
            "dup.env": "BW_MODE", "bad-scope.env": "BW_SCOPE", "bad-secrets-scope.env": "BW_SECRETS_ITEM_SCOPE",
            "bad-collection.env": "BW_SCOPE", "bad-secrets-collection.env": "BW_SECRETS_ITEM_SCOPE",
            "server-query.env": "BW_SERVER", "server-fragment.env": "BW_SERVER",
        }
        expect(len(errors) == len(want), f"{len(errors)} errors: {errors}")
        for name, key in want.items():
            expect(any(name in e and key in e for e in errors), f"no error naming {name} and {key}: {errors}")

        expect(accounts.load_dir(d / "missing") == ([], []), "missing directory")

        defaults = d / "defaults"
        defaults.mkdir()
        write_env(defaults, "plain.env", good)
        (only,), errs = accounts.load_dir(defaults)
        expect(not errs and (only.scope_kind, only.scope_value) == ("folder", "Agent"), "default scope folder:Agent")


def test_normalize_origin() -> None:
    cases = {
        "https://Example.COM:443/path?q=1": "https://example.com",
        "http://example.com:80": "http://example.com",
        "https://example.com:8443/": "https://example.com:8443",
        "  https://example.com  ": "https://example.com",
        "androidapp://com.example.app": "androidapp://com.example.app",
        "example.com": None,
        "": None,
        "https://": None,
        "https://example.com:99999": None,
        "https://[::1": None,
    }
    for url, want in cases.items():
        got = origins.normalize_origin(url)
        expect(got == want, f"normalize_origin({url!r}) = {got!r}, want {want!r}")
    expect(origins.normalize_origin(None) is None, "None input")


def test_bw_invocation() -> None:
    planted = {"LEAKY_TOKEN": LEAKY_ENV, "BW_SESSION": LEAKY_ENV, "BW_PASSWORD": LEAKY_ENV,
               "FAKE_BW_LOG": "/nonexistent/broker-env.log", "LC_ALL": "C.UTF-8"}
    saved = {k: os.environ.get(k) for k in planted}
    os.environ.update(planted)
    try:
        _test_bw_invocation()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _test_bw_invocation() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        acct = run.account("personal")
        bw = run.bw(acct)
        expect(bw.state == "locked" and bw.error_class == "", "starts locked")
        bw.login()
        bw.unlock()
        expect(bw.state == "unlocked", f"state {bw.state}")
        state = repr(vars(bw))
        expect(MASTER not in state and CLIENT_SECRET not in state, "a credential is kept on the Bw object")
        bw.sync()
        password = bw.get_password(item_id("A"))
        expect(password == ITEM_PASSWORDS["A"], "get_password")
        expect(bw.get_password(item_id("H")) == ITEM_PASSWORDS["H"], "get_password strips only bw's newline")
        code = bw.get_totp(item_id("A"))
        expect(len(code) == 6 and code.isdigit(), "get_totp")
        expect_bw_error(run, lambda: bw.get_totp(item_id("B")), "unknown", "no TOTP on item B")
        expect_bw_error(run, lambda: bw.get_password("--help"), "unknown", "item id that looks like a flag")

        lines = run.log_lines()
        cmds = [line["argv"][:2] for line in lines]
        expect(cmds[0] == ["config", "server"] and lines[0]["argv"][2] == acct.server, "config server first")
        login = next(line for line in lines if line["argv"][:1] == ["login"])
        expect(login["argv"] == ["login", "--apikey"], f"login argv {login['argv']}")
        expect(set(login["env"]) == {"BW_CLIENTID", "BW_CLIENTSECRET"}, f"login env {login['env']}")
        unlock = next(line for line in lines if line["argv"][:1] == ["unlock"])
        expect(unlock["argv"] == ["unlock", "--passwordenv", "BW_PW", "--raw"], f"unlock argv {unlock['argv']}")
        expect(unlock["env"] == ["BW_PW"], f"unlock env {unlock['env']}")
        allowed = bwcli.ENV_ALLOW | {"BITWARDENCLI_APPDATA_DIR", "BW_NOINTERACTION", "BW_CLIENTID",
                                     "BW_CLIENTSECRET", "BW_PW", "BW_SESSION", *run.extra_env}
        for line in lines:
            names = set(line["env_names"])
            # The interpreter running the fake may add a few of its own (macOS adds __CF_USER_TEXT_ENCODING).
            extra = {n for n in names - allowed if not n.startswith(("LC_", "__CF", "__PYVENV"))}
            expect(not extra, f"bw saw variables outside the allowlist: {sorted(extra)}")
            expect("LEAKY_TOKEN" not in names and "BW_PASSWORD" not in names, "broker env leaked into bw")
            expect("LC_ALL" in names, "LC_* passed through")
            expect(line["appdata"] == str(run.data_root / "personal"), "per-account BITWARDENCLI_APPDATA_DIR")
            expect(line["nointeraction"], "BW_NOINTERACTION set")
            if line["argv"][:1] in (["sync"], ["get"], ["list"]):
                expect(line["env"] == ["BW_SESSION"], f"session by env only: {line}")

        session = bw._session
        expect(session and session not in run.log_file.read_text(encoding="utf-8"), "session key never logged")
        twin = run.bw(acct)
        expect(twin._lock is bw._lock, "two Bw objects for one account share a lock")
        twin.lock()  # bw lock drops every session for this data dir, as bw does
        bw.lock()
        expect(session not in repr(vars(bw)), "session key kept after lock")

        # A second instance (broker restart) reuses the data dir: no second config, login is a no-op.
        before = len(lines)
        again = run.bw(acct)
        again.login()
        again.unlock()
        expect({i["id"] for i in again.cached_items()} == {item_id(n) for n in "ADEG"}, "unlock lists the folder's logins")
        later = [line["argv"][:2] for line in run.log_lines()[before:]]
        expect(["config", "server"] not in later, f"config server ran again: {later}")
        expect(again.state == "unlocked", "restart unlocks again with the stored password")

        expect_bw_error(run, lambda: run.bw(run.account("nosecret", unlock="manual")).unlock(), "locked",
                        "manual unlock with no password")
        fresh = run.bw(run.account("other"))
        (run.secrets_root / "other" / "client_secret").unlink()
        expect_bw_error(run, fresh.login, "auth", "missing client_secret")
        run.expect_clean(out.getvalue(), extra_secrets=[session, again._session, password, code])


def test_scope_filtering() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        bws = {}
        for slug, scope in (("folder", "folder:Agent"), ("coll", f"collection:{COLLECTION}"), ("every", "all"),
                            ("nofolder", "folder:Missing")):
            bws[slug] = run.bw(run.account(slug, scope=scope))
            bws[slug].login()
            bws[slug].unlock()
        v = vault.Vault(bws, vault.Denylist(run.tmp / "broker" / "denylist.json"))
        calls = len(run.log_lines())
        got: dict[str, set] = {}
        for meta in v.items():
            got.setdefault(meta.account, set()).add(meta.item_id)
        expect(got.get("folder") == {item_id("A"), item_id("E")}, f"folder scope {got.get('folder')}")
        expect(got.get("coll") == {item_id("C")}, f"collection scope {got.get('coll')}")
        expect(got.get("every") == {item_id(n) for n in "ABCE"}, f"all scope {got.get('every')}")
        expect("nofolder" not in got, "missing folder lists nothing")
        v.items()
        v.get(f"bwbroker:folder/{item_id('A')}")
        expect(len(run.log_lines()) == calls, "Vault.items() or get() ran bw")

        a = v.get(f"bwbroker:folder/{item_id('A')}")
        expect(a is not None and a.handle == f"bwbroker:folder/{item_id('A')}", "get by handle")
        expect(a.label == "Item A" and a.username == "user-a@example.com", "label and username")
        expect(a.origins == ("https://login.example.com",) and a.has_totp, "origins and has_totp")
        expect(v.get(f"bwbroker:folder/{item_id('B')}") is None, "out-of-scope item not returned by get")
        expect(v.get(f"bwbroker:nosuch/{item_id('A')}") is None and v.get("vault_x") is None, "bad handles")
        expect(not v.get(f"bwbroker:every/{item_id('B')}").has_totp, "has_totp false without a seed")
        run.expect_clean(out.getvalue() + repr(v.items()))


def test_mixed_uris() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        v = vault.Vault({"personal": bw}, vault.Denylist(run.tmp / "denylist.json"))
        meta = v.get(f"bwbroker:personal/{item_id('E')}")
        expect(meta is not None, "mixed-URI item listed")
        expect(meta.origins == ("https://good.example.com",), f"origins {meta.origins}")
        for other in ("androidapp://com.example.mixed", "https://never.example.com", "https://mixed.example.org",
                      "http://good.example.com", "https://good.example.com:8443"):
            expect(origins.normalize_origin(other) not in meta.origins, f"{other} is a fill target")
        run.expect_clean(out.getvalue())


def test_no_uri_items_hidden() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        bw = run.bw(run.account("personal", scope="all"))
        bw.login()
        bw.unlock()
        v = vault.Vault({"personal": bw}, vault.Denylist(run.tmp / "denylist.json"))
        ids = {m.item_id for m in v.items()}
        for name in "DFG":
            expect(item_id(name) not in ids, f"item {name} listed")
            expect(v.get(f"bwbroker:personal/{item_id(name)}") is None, f"item {name} returned by get")
        run.expect_clean(out.getvalue())


def test_429_backoff() -> None:
    for retry_after, want, good_syncs in ((120, 120, 1), (None, 60, 0)):
        with tempfile.TemporaryDirectory() as tmp, captured() as out:
            run = Run(Path(tmp), rate_limit_after=1 + good_syncs, retry_after=retry_after)
            bw = run.bw(run.account("personal"))
            bw.login()  # server call 1
            bw.unlock()
            for _ in range(good_syncs):
                bw.sync()
            v = vault.Vault({"personal": bw}, vault.Denylist(run.tmp / "denylist.json"))
            first = v.items()
            expect(len(first) == 2, f"listed {len(first)}")
            cached = bw.cached_items()
            start = time.monotonic()
            expect_bw_error(run, bw.sync, "rate_limited", "429 on sync")
            expect(want - 5 <= bw.retry_at - start <= want + 1, f"retry_at {bw.retry_at - start:.0f}s, want {want}")
            calls = len(run.log_lines())
            expect_bw_error(run, bw.sync, "rate_limited", "sync during backoff")
            expect(len(run.log_lines()) == calls, "sync during backoff ran bw")
            expect(v.items() == first, "items served from the last list during backoff")
            expect(bw.list_items() == cached, "list_items during backoff")
            expect(len(run.log_lines()) == calls, "listing during backoff ran bw")
            expect(bw.state == "unlocked" and bw.error_class == "rate_limited", f"{bw.state}/{bw.error_class}")
            run.expect_clean(out.getvalue())


def test_errors_carry_no_text() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        stored = run.bw(run.account("stored", master=WRONG_PASSWORD))
        stored.login()
        expect_bw_error(run, stored.unlock, "auth", "wrong stored password")
        expect(WRONG_PASSWORD in run.offered_stderr(), "the fake did not put the password in its stderr")
        expect(stored.state == "error" and stored.error_class == "auth", f"{stored.state}/{stored.error_class}")

        manual = run.bw(run.account("manual", unlock="manual"))
        manual.login()
        expect_bw_error(run, lambda: manual.unlock(WRONG_PASSWORD), "auth", "wrong manual password")

        expect_bw_error(run, lambda: run.bw(run.account("nologin", unlock="manual")).sync(), "locked",
                        "sync before unlock")
        broken = bwcli.Bw(run.account("broken"), run.data_root, run.secrets_root, bw_path=str(run.tmp / "no-bw"))
        try:
            bwcli.Bw(run.account("bad-extra"), run.data_root, run.secrets_root, extra_env={"BW_PW": "x"})
            fail("extra_env accepted a BW_* variable")
        except ValueError:
            pass
        expect_bw_error(run, broken.login, "unknown", "missing bw executable")
        run.expect_clean(out.getvalue())


def test_exception_context_holds_no_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp), hang_sync=WRONG_PASSWORD)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        old_timeout, bwcli.TIMEOUT_SECONDS = bwcli.TIMEOUT_SECONDS, 2
        try:
            exc = expect_bw_error(run, bw.sync, "network", "bw timing out")
        finally:
            bwcli.TIMEOUT_SECONDS = old_timeout
        expect(WRONG_PASSWORD in run.offered_stderr(), "the fake did not write before hanging")
        text = reachable_text(exc)
        expect(WRONG_PASSWORD not in text, "timeout output reachable from BwError")
        expect(bw._session not in text, "the timed-out call's session env reachable from BwError")
        expect(bw.state == "unlocked" and len(bw.cached_items()) == 4, "a failed sync keeps the last list")

        data = json.loads(run.vault_file.read_text(encoding="utf-8"))
        data["truncate_lists"] = True
        run.vault_file.write_text(json.dumps(data), encoding="utf-8")
        exc = expect_bw_error(run, bw.list_items, "unknown", "truncated list JSON")
        expect(ITEM_PASSWORDS["A"] not in reachable_text(exc), "bw list output reachable from BwError")

        failed = run.bw(run.account("wrong", master=WRONG_PASSWORD))
        failed.login()
        exc = expect_bw_error(run, failed.unlock, "auth", "wrong password")
        text = reachable_text(exc)
        expect(WRONG_PASSWORD not in text and CLIENT_SECRET not in text, "a credential is reachable from BwError")
        run.expect_clean(out.getvalue())


def test_node_warning_noise() -> None:
    expect(bwcli.classify(NODE_WARNING + "Not found.") == ("unknown", None), "PID read as a status code")
    expect(bwcli.classify(NODE_WARNING + "Too Many Requests (429). Retry-After: 120") == ("rate_limited", 120), "429")
    expect(bwcli.classify("Request failed with status code 429") == ("rate_limited", 60), "status code 429")
    expect(bwcli.classify("Request failed with status code 401") == ("auth", None), "status code 401")
    expect(bwcli.classify("connect ECONNREFUSED 10.0.0.1:443") == ("network", None), "network")
    expect(bwcli.classify("Item 401 is locked in a drawer") == ("unknown", None), "bare words")
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp), stderr_prefix=NODE_WARNING)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        expect_bw_error(run, lambda: bw.get_totp(item_id("B")), "unknown", "no TOTP behind a Node warning")
        expect_bw_error(run, lambda: bw.get_password("ffffffff-0000-4000-8000-000000000000"), "unknown",
                        "not found behind a Node warning")
        expect("4014290" in run.offered_stderr(), "the fake did not print the warning")
        expect(bw.state == "unlocked" and bw.error_class == "" and bw.retry_at is None, "account state changed")
        expect(bw.get_password(item_id("A")) == ITEM_PASSWORDS["A"], "session kept")
        manual = run.bw(run.account("manual", unlock="manual"))
        manual.login()
        expect_bw_error(run, lambda: manual.unlock(WRONG_PASSWORD), "auth", "wrong password behind a warning")
        run.expect_clean(out.getvalue())


def test_manual_unlock_and_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        bw = run.bw(run.account("manual", unlock="manual"))
        v = vault.Vault({"manual": bw}, vault.Denylist(run.tmp / "denylist.json"))
        expect(bw.state == "locked", f"starts {bw.state}")
        expect(v.items() == [], "locked account lists nothing")
        bw.login()
        bw.unlock(MASTER)
        expect(bw.state == "unlocked", f"after unlock {bw.state}")
        expect(len(v.items()) == 2, "unlocked account lists its items")
        bw.lock()
        expect(bw.state == "locked", f"after lock {bw.state}")
        expect(v.items() == [] and v.get(f"bwbroker:manual/{item_id('A')}") is None, "items vanish after lock")
        expect(run.log_lines()[-1]["argv"] == ["lock"], "bw lock ran")
        run.expect_clean(out.getvalue())


def test_denylist() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        path = run.tmp / "broker" / "denylist.json"
        deny = vault.Denylist(path)
        v = vault.Vault({"personal": bw}, deny)
        handle = f"bwbroker:personal/{item_id('A')}"
        deny.add(handle)
        expect(handle in deny and handle not in {m.handle for m in v.items()}, "denylisted handle listed")
        expect(v.get(handle) is None, "denylisted handle returned by get")
        expect(stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
        expect(handle in vault.Denylist(path), "denylist persists")
        expect([p.name for p in path.parent.iterdir()] == ["denylist.json"], "no temp files left")
        deny.remove(handle)
        expect(handle not in vault.Denylist(path) and v.get(handle) is not None, "remove")
        deny.remove(handle)
        run.expect_clean(out.getvalue())


# --- CDP fill -------------------------------------------------------------

CDP_TOKEN = secrets.token_urlsafe(16)
# Review Focus 3 characters, so the JSON transport has to carry them intact.
CDP_VALUE = f"pw+/%é{CDP_TOKEN}\nend"
TARGET = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
LOGIN_URL = "https://login.example.com/session?next=/home"
SESSION = "5E55104D"


class FakeCdp:
    """A DevTools endpoint on one port: /json/list and /json/version over HTTP, CDP over websocket.

    ``page`` picks what the fill function reports: ok, navigated, no_field, exception or hang.
    Runs in the Hermes image, which has websockets 15; the broker code itself keeps to 10.4.
    """

    def __init__(self, targets, page="ok"):
        self.targets = targets
        self.page = page
        self.messages: list[dict] = []
        self.connections = 0
        self.port = 0
        self.closed_on_attach = False

    async def start(self):
        import websockets
        self.server = await websockets.serve(self.handle, "127.0.0.1", 0, process_request=self.http)
        self.port = self.server.sockets[0].getsockname()[1]

    def http(self, connection, request):
        if request.path == "/json/list":
            body = [{"id": t["id"], "type": t.get("type", "page"), "url": t["url"], "title": "Sign in",
                     "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{t['id']}"}
                    for t in self.targets]
        elif request.path == "/json/version":
            # Chromium reports whatever host it was asked on; the broker must ignore it.
            body = {"Browser": "HeadlessChrome/140.0.0.0",
                    "webSocketDebuggerUrl": "ws://chromium.invalid:9222/devtools/browser/0f1e2d3c-4b5a-6978"}
        else:
            return None
        return connection.respond(200, json.dumps(body))

    async def handle(self, ws):
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            # An unrelated event before every reply: the client must match replies by id.
            await ws.send(json.dumps({"method": "Target.targetInfoChanged", "params": {"targetInfo": {}}}))
            reply = {"id": msg["id"]}
            if msg.get("sessionId"):
                reply["sessionId"] = msg["sessionId"]
            method, params = msg.get("method"), msg.get("params", {})
            if method == "Target.attachToTarget":
                if not self.closed_on_attach and any(t["id"] == params.get("targetId") for t in self.targets):
                    reply["result"] = {"sessionId": SESSION}
                else:
                    reply["error"] = {"code": -32602, "message": "No target with given id found"}
            elif method == "Runtime.evaluate":
                reply["result"] = {"result": {"type": "object", "className": "HTMLDocument", "objectId": "1.2.3"}}
            elif method == "Runtime.callFunctionOn":
                values = " ".join(str(a.get("value")) for a in params.get("arguments", []))
                if self.page == "hang":
                    continue
                if self.page == "exception":
                    reply["result"] = {
                        "result": {"type": "object", "subtype": "error", "description": f"Error: rejected {values}"},
                        "exceptionDetails": {"text": f"Uncaught Error: rejected {values}",
                                             "exception": {"description": f"Error: rejected {values}"}},
                    }
                else:
                    outcome = {"ok": {"filled": 1}, "no_field": {"filled": 0},
                               "navigated": {"refused": "origin_changed"}}[self.page]
                    reply["result"] = {"result": {"type": "object", "value": outcome}}
            else:
                reply["result"] = {}
            await ws.send(json.dumps(reply))

    def methods(self) -> list[str]:
        return [m.get("method") for m in self.messages]


@contextlib.contextmanager
def fake_cdp(targets, page="ok"):
    import asyncio
    import threading

    fake = FakeCdp(targets, page)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(fake.start())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    expect(ready.wait(10), "fake CDP server started")
    try:
        yield fake
    finally:
        async def stop():
            fake.server.close()
            await fake.server.wait_closed()
        asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)


def expect_no_value(texts, msg: str) -> None:
    expect(CDP_TOKEN not in "\n".join(texts), f"{msg}: the fill value appeared in captured output")


def expect_detached(fake: FakeCdp, msg: str) -> None:
    methods = fake.methods()
    expect("Target.attachToTarget" in methods, f"{msg}: never attached")
    detach = [m for m in fake.messages if m.get("method") == "Target.detachFromTarget"]
    expect(len(detach) == 1 and detach[0].get("params", {}).get("sessionId") == SESSION,
           f"{msg}: no detach of the attached session")
    expect(methods[-1] == "Target.detachFromTarget", f"{msg}: detach is not the last command")


def run_fill(fake, allowed=("https://login.example.com",), target=TARGET, timeout=10.0):
    with captured() as out:
        result = cdp.fill(fake.port, target, tuple(allowed), CDP_VALUE, timeout=timeout)
    return result, out.getvalue()


def test_fill_writes_value() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}]) as fake:
        result, out = run_fill(fake, allowed=("https://LOGIN.example.com:443/", "https://other.example.com"))
    expect(result == cdp.FillResult(filled=True, origin="https://login.example.com", error=None),
           f"fill result {result!r}")
    calls = [m for m in fake.messages if m.get("method") == "Runtime.callFunctionOn"]
    expect(len(calls) == 1, "one callFunctionOn")
    args = calls[0]["params"].get("arguments", [])
    expect([a.get("value") for a in args] == ["https://login.example.com", CDP_VALUE],
           "the expected origin and the value arrive as callFunctionOn arguments")
    expect(calls[0]["params"].get("objectId") == "1.2.3", "called on the evaluated document")
    attach = fake.messages[0]
    expect(attach.get("method") == "Target.attachToTarget"
           and attach["params"] == {"targetId": TARGET, "flatten": True}, "attaches flattened")
    expect(all(m.get("sessionId") == SESSION for m in fake.messages
               if m.get("method", "").startswith("Runtime.")), "Runtime commands use the session")
    for m in fake.messages:
        params = dict(m.get("params", {}))
        for key in ("expression", "functionDeclaration"):
            expect(CDP_TOKEN not in str(params.get(key, "")), f"value in {key}")
        if m.get("method") == "Runtime.callFunctionOn":
            params.pop("arguments", None)
        expect_no_value([json.dumps({**m, "params": params}, ensure_ascii=False)], "a command outside the arguments")
    expect_detached(fake, "fill")
    expect_no_value([out, repr(result)], "fill")


def test_origin_refused() -> None:
    with fake_cdp([{"id": TARGET, "url": "https://evil.example.com/login"}]) as fake:
        result, out = run_fill(fake)
    expect(result == cdp.FillResult(filled=False, origin="https://evil.example.com", error="origin_mismatch"),
           f"refused result {result!r}")
    expect("Runtime.callFunctionOn" not in fake.methods(), "no callFunctionOn on a refused origin")
    expect(fake.connections == 0, "no CDP websocket for a refused origin")
    expect_no_value([out, repr(result)], "origin refused")


def test_target_navigated() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="navigated") as fake:
        result, out = run_fill(fake)
    expect(not result.filled and result.error == "origin_mismatch", f"navigated result {result!r}")
    expect_detached(fake, "navigated")
    expect_no_value([out, repr(result)], "navigated")


def test_target_closed() -> None:
    with fake_cdp([{"id": "0FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "url": LOGIN_URL}]) as fake:
        result, out = run_fill(fake)
    expect(result == cdp.FillResult(filled=False, origin="", error="target_not_found"), f"closed result {result!r}")
    expect(fake.connections == 0, "no CDP websocket for a missing target")
    # Closed after /json/list was read: attach fails, and there is no session to detach.
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}]) as fake:
        fake.closed_on_attach = True
        result, out2 = run_fill(fake)
    expect(not result.filled and result.error == "target_not_found", f"closed-on-attach result {result!r}")
    expect(fake.methods() == ["Target.attachToTarget"], f"only the attach was sent: {fake.methods()}")
    expect_no_value([out, out2, repr(result)], "target closed")


def test_no_value_in_errors() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="exception") as fake:
        result, out = run_fill(fake)
    expect(result == cdp.FillResult(filled=False, origin="https://login.example.com", error="page_error"),
           f"page exception result {result!r}")
    expect_detached(fake, "page exception")
    expect_no_value([out, repr(result), str(result.error)], "page exception")


def test_no_field() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="no_field") as fake:
        result, out = run_fill(fake)
    expect(not result.filled and result.error == "no_field", f"no field result {result!r}")
    expect_detached(fake, "no field")
    expect_no_value([out, repr(result)], "no field")


def test_fill_timeout() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="hang") as fake:
        start = time.monotonic()
        result, out = run_fill(fake, timeout=2.0)
        elapsed = time.monotonic() - start
    expect(not result.filled and result.error == "cdp_unavailable", f"timeout result {result!r}")
    expect(elapsed < 2.5, f"fill took {elapsed:.2f}s with a 2s timeout")
    expect_detached(fake, "timeout")
    expect_no_value([out, repr(result)], "timeout")


def run_cdp_tests() -> None:
    """The fake-server tests. Run inside the Hermes image, which has websockets."""
    test_fill_writes_value()
    test_origin_refused()
    test_target_navigated()
    test_target_closed()
    test_no_value_in_errors()
    test_no_field()
    test_fill_timeout()


def test_fill_rejects_bad_arguments() -> None:
    """Port and target id are checked before any I/O, so these need no server."""
    def must_not_connect(*a, **k):
        fail("fill did I/O before validating its arguments")
    real = cdp._get_json
    cdp._get_json = must_not_connect
    try:
        for port in (0, 65536, -1, "9222", True, 9222.0):
            got = cdp.fill(port, TARGET, ("https://login.example.com",), CDP_VALUE)
            expect(got == cdp.FillResult(False, "", "cdp_unavailable"), f"port {port!r}: {got!r}")
        for target in ("", "A" * 65, "../json", "abc-def", "abc\n", None, 7):
            got = cdp.fill(9222, target, ("https://login.example.com",), CDP_VALUE)
            expect(got == cdp.FillResult(False, "", "target_not_found"), f"target {target!r}: {got!r}")
    finally:
        cdp._get_json = real


def test_fill_cdp_down() -> None:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with captured() as out:
        got = cdp.fill(port, TARGET, ("https://login.example.com",), CDP_VALUE, timeout=2.0)
    expect(got == cdp.FillResult(False, "", "cdp_unavailable"), f"closed port: {got!r}")
    expect_no_value([out.getvalue(), repr(got)], "closed port")


CDP_DRIVER = r"""
import runpy, sys
sys.dont_write_bytecode = True
ns = runpy.run_path("/work/scripts/test-bitwarden-broker.py", run_name="cdp_driver")
ns["run_cdp_tests"]()
print("image ok")
"""


def main() -> None:
    test_account_file_tolerance()
    test_normalize_origin()
    test_bw_invocation()
    test_scope_filtering()
    test_mixed_uris()
    test_no_uri_items_hidden()
    test_429_backoff()
    test_errors_carry_no_text()
    test_exception_context_holds_no_secrets()
    test_node_warning_noise()
    test_manual_unlock_and_lock()
    test_denylist()
    test_fill_rejects_bad_arguments()
    test_fill_cdp_down()
    hermes_image.maybe_run(CDP_DRIVER, mounts=[
        (ROOT / "scripts", "/work/scripts"),
        (ROOT / "compose" / "bitwarden-broker", "/work/compose/bitwarden-broker"),
    ])
    print("ok")


if __name__ == "__main__":
    main()
