#!/usr/bin/env python3
"""Checks for the Bitwarden broker: accounts, bw wrapper, item listing, CDP fill and the HTTP API.

Stdlib only. Runs scripts/fixtures/fake-bw.py as the bw executable against a
synthetic vault (example.com sites, generated passwords). Every test searches
the captured log records, exception text, fake-bw log and files the broker
writes for the run's synthetic secrets.

The CDP fill tests need websockets for the fake DevTools server, so they run
inside the pinned Hermes image (scripts/lib/hermes_image.py) with scripts/ and
compose/bitwarden-broker/ mounted read-only, and skip when the image is absent.
BITWARDEN_TEST_IMAGE runs them in the broker image instead (Python 3.11, websockets 10.4).
The API tests run ``python3 -m broker.server`` as a subprocess; scripts/test-bitwarden-access.py
reuses their helpers for access requests and release.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import secrets
import socketserver
import stat
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect, fail  # noqa: E402

import hermes_image  # noqa: E402
from broker import accounts, bwcli, cdp, origins, signin, vault  # noqa: E402

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


_KEEP = object()  # login_item: "leave the generated password alone"


def item_id(name: str) -> str:
    return f"{name.lower() * 8}-0000-4000-8000-000000000000"


def login_item(name, folder=None, collections=(), uris=(), totp=None, kind=1, in_page=True, fields=None,
               password=_KEEP):
    """in_page sets agent-in-page-fill (true by default, so /fill tests exercise the in-page path).

    ``password=None`` builds a login with no password, as Bitwarden returns for an item in a
    collection the account holds with Hide Passwords: username and item readable, password not.
    """
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
            "password": ITEM_PASSWORDS[name] if password is _KEEP else password,
            "totp": totp,
            "uris": None if uris is None else [{"match": m, "uri": u} for u, m in uris],
        }
        item["fields"] = ([{"name": "agent-in-page-fill", "value": "true", "type": 2}] if in_page else []) + list(
            fields or [])
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
    """A temp layout (instances, bw data, secrets, fake vault) plus capture of everything emitted."""

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
            path=self.tmp / f"{slug}.json",
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


def write_instance(directory: Path, slug: str, values, secrets_dir: Path | None = None,
                   client_id: str | None = "user.syn") -> None:
    """instances/<slug>.json as addon_store writes it, and secrets/<slug>/client_id unless client_id is None."""
    text = values if isinstance(values, str) else json.dumps(values, indent=2) + "\n"
    (directory / f"{slug}.json").write_text(text, encoding="utf-8")
    if secrets_dir is not None and client_id is not None:
        (secrets_dir / slug).mkdir(parents=True, exist_ok=True)
        (secrets_dir / slug / "client_id").write_text(client_id + "\n", encoding="utf-8")


def test_instance_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "instances"
        sec = Path(tmp) / "secrets"
        broker_dir = Path(tmp) / "broker"
        for x in (d, sec, broker_dir):
            x.mkdir()
        write_instance(d, "personal", {"mode": "cloud", "region": "eu", "scope": "folder:Agent", "unlock": "stored",
                                       "note": "an unknown key"}, sec, "user.00000000-0000-4000-8000-000000000001")
        write_instance(d, "us-default", {"mode": "cloud"}, sec)
        write_instance(d, "home-lab", {"mode": "selfhosted", "url": "https://vault.example.com/",
                                       "scope": "collection:" + COLLECTION, "unlock": "manual"}, sec)
        write_instance(d, "wide", {"mode": "cloud", "scope": "all"}, sec)
        good = {"mode": "cloud", "unlock": "stored"}
        write_instance(d, "not-json", "{mode: cloud", sec)
        write_instance(d, "a-list", "[1, 2]", sec)
        write_instance(d, "auto", {**good, "unlock": "auto"}, sec)
        write_instance(d, "mars", {**good, "region": "mars"}, sec)
        write_instance(d, "no-mode", {"unlock": "stored"}, sec)
        write_instance(d, "mode-local", {**good, "mode": "local"}, sec)
        write_instance(d, "number-scope", {**good, "scope": 5}, sec)
        write_instance(d, "no-url", {"mode": "selfhosted"}, sec)
        write_instance(d, "plain-http", {"mode": "selfhosted", "url": "http://vault.example.com"}, sec)
        write_instance(d, "url-query", {"mode": "selfhosted", "url": "https://vault.example.com/?next=x"}, sec)
        write_instance(d, "url-fragment", {"mode": "selfhosted", "url": "https://vault.example.com/#x"}, sec)
        write_instance(d, "bad-scope", {**good, "scope": "everything"}, sec)
        write_instance(d, "bad-collection", {**good, "scope": "collection:--organizationid"}, sec)
        write_instance(d, "no-id", good, sec, client_id=None)
        write_instance(d, "empty-id", good, sec, client_id="")
        write_instance(d, "spaced-id", good, sec, client_id="user.a b")
        # Slugs outside the store's rule, and the reserved local.
        write_instance(d, "9abc", good, sec)
        write_instance(d, "a" * 32, good, sec)
        write_instance(d, "Upper", good, sec)
        write_instance(d, "local", good, sec)
        (d / "README").write_text("not an account\n", encoding="utf-8")
        (d / ".personal.json.x.tmp").write_text("{}", encoding="utf-8")
        # An account file from before the instance store, in either folder, is not read.
        old_env = "BW_MODE=cloud\nBW_SERVER=https://vault.bitwarden.com\nBW_UNLOCK=stored\nBW_CLIENTID=user.x\n"
        (d / "old.env").write_text(old_env, encoding="utf-8")
        (broker_dir / "local.env").write_text(old_env, encoding="utf-8")

        def load():
            return accounts.load_all(d, broker_dir, sec)

        loaded, refused = load()
        by_slug = {a.slug: a for a in loaded}
        expect(sorted(by_slug) == ["home-lab", "personal", "us-default", "wide"], f"loaded {sorted(by_slug)}")
        p = by_slug["personal"]
        expect(p.mode == "cloud" and p.server == "https://vault.bitwarden.eu", "cloud eu")
        expect((p.scope_kind, p.scope_value) == ("folder", "Agent") and p.unlock == "stored", "scope and unlock")
        expect(p.client_id == "user.00000000-0000-4000-8000-000000000001", "client_id from secrets/<slug>/client_id")
        expect(p.path == d / "personal.json", "path")
        u = by_slug["us-default"]
        expect(u.server == "https://vault.bitwarden.com", "region defaults to us")
        expect((u.scope_kind, u.scope_value, u.unlock) == ("folder", "Agent", "manual"),
               "scope defaults to folder:Agent, unlock to manual")
        h = by_slug["home-lab"]
        expect(h.mode == "selfhosted" and h.server == "https://vault.example.com" and h.unlock == "manual",
               "selfhosted, trailing slash dropped")
        expect((h.scope_kind, h.scope_value) == ("collection", COLLECTION), "collection")
        expect((by_slug["wide"].scope_kind, by_slug["wide"].scope_value) == ("all", None), "all")

        want = {
            "not-json.json": ("bad_file", "JSON"), "a-list.json": ("bad_file", "object"),
            "auto.json": ("bad_file", "unlock"), "mars.json": ("bad_file", "region"),
            "no-mode.json": ("bad_file", "mode"), "mode-local.json": ("bad_file", "mode"),
            "number-scope.json": ("bad_file", "scope"), "no-url.json": ("bad_file", "url"),
            "plain-http.json": ("bad_file", "url"), "url-query.json": ("bad_file", "url"),
            "url-fragment.json": ("bad_file", "url"), "bad-scope.json": ("bad_file", "scope"),
            "bad-collection.json": ("bad_file", "scope"),
            "no-id.json": ("missing_client_id", "client_id"), "empty-id.json": ("missing_client_id", "client_id"),
            "spaced-id.json": ("bad_file", "client_id"),
            "9abc.json": ("bad_slug", "slug"), f"{'a' * 32}.json": ("bad_slug", "slug"),
            "Upper.json": ("bad_slug", "slug"), "local.json": ("bad_slug", "reserved"),
        }
        got = {r.file: r for r in refused}
        expect(sorted(got) == sorted(want), f"refused {sorted(got)}")
        for name, (cause, word) in want.items():
            r = got[name]
            expect(r.cause == cause, f"{name}: cause {r.cause}, want {cause}")
            expect(r.message.startswith(f"{name}: ") and word in r.message, f"{name}: message {r.message!r}")
        expect("user.a b" not in " ".join(r.message for r in refused), "no message repeats a value")

        # The local account: broker/local-account.json plus secrets/local/client_id.
        (sec / "local").mkdir(exist_ok=True)
        (sec / "local" / "client_id").write_text("user.local\n", encoding="utf-8")
        (broker_dir / "local-account.json").write_text(json.dumps({"scope": "collection:" + COLLECTION}),
                                                       encoding="utf-8")
        loaded, refused = load()
        local = {a.slug: a for a in loaded}["local"]
        expect((local.mode, local.server, local.unlock) == ("local", accounts.LOCAL_SERVER, "stored"), "local")
        expect((local.scope_kind, local.scope_value, local.client_id) == ("collection", COLLECTION, "user.local"),
               "local scope and client_id")
        expect("local.json" in {r.file for r in refused}, "an instance named local stays refused")
        (broker_dir / "local-account.json").write_text(json.dumps({"scope": "folder:Agent"}), encoding="utf-8")
        loaded, refused = load()
        expect("local" not in {a.slug for a in loaded}, "a local account without a collection scope is refused")
        expect(any(r.file == "broker/local-account.json" and r.cause == "bad_file" for r in refused),
               f"local refusal: {refused}")
        expect(len(loaded) == 4, "the instances still load beside a refused local account")

        expect(accounts.load_all(Path(tmp) / "missing", Path(tmp) / "missing", sec) == ([], []),
               "missing folders")
        if os.getuid() != 0:
            d.chmod(0)
            try:
                loaded, refused = load()
            finally:
                d.chmod(0o700)
            expect([a.slug for a in loaded] == [] and [(r.file, r.cause) for r in refused] == [
                ("instances/", "bad_file"), ("broker/local-account.json", "bad_file")],
                f"an unreadable instances/ is one refusal: {loaded} {refused}")
        account = accounts.load_account("home-lab", d, broker_dir, sec)
        expect(account.slug == "home-lab", "load_account finds an instance")
        for slug, cause in (("nope", "bad_file"), ("9abc", "bad_slug"), ("local", "bad_file")):
            try:
                accounts.load_account(slug, d, broker_dir, sec)
            except accounts.AccountError as exc:
                expect(exc.cause == cause, f"load_account {slug}: {exc.cause}")
            else:
                fail(f"load_account {slug} loaded")


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


def test_single_use_uri() -> None:
    single = ("https://x.test/invite/abc", "https://x.test/accept", "https://x.test/activate/1",
              "https://x.test/password/reset", "https://x.test/verify/9", "https://x.test/confirm",
              "https://x.test/magic", "https://x.test/token/zz",
              "https://x.test/login?token=AAAAAAAAAAAA", "https://x.test/signin?code=99",
              "https://x.test/go#AAAAAAAAAAAAAAAAAAAAAA", "https://x.test/s/AAAAAAAAAAAAAAAAAAAAAA",
              # The hyphenated forms these flows actually use.
              "https://app.example.com/accept-invite/7f3a", "https://app.example.com/reset-password?t=ab12",
              "https://app.example.com/verify-email/9f2", "https://app.example.com/magic-link/ab12",
              "https://app.example.com/invite_accept/7f3a")
    for uri in single:
        expect(origins.single_use_uri(uri), f"single-use: {uri}")
    reusable = ("https://x.test/login", "https://x.test/", "https://x.test",
                "https://x.test/users/sign_in", "https://x.test/auth/login",
                "https://x.test/static/app.bundle.min.js",
                "https://x.test/login?next=/home", "https://x.test/login?returnUrl=%2Fa",
                "https://x.test/login#form",
                # An opaque segment with more path after it is a tenant, realm or
                # workspace id, not a token: this is a real SSO login page.
                "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47/oauth2/v2.0/authorize",
                "https://acme.okta.com/oauth2/ausWb3m0kaIq7Tfj8x7/v1/authorize")
    for uri in reusable:
        expect(not origins.single_use_uri(uri), f"reusable: {uri}")


def test_login_candidates_order_and_cap() -> None:
    got = origins.login_candidates(("https://x.test/invite/abc", "https://x.test/signin"), "https://x.test")
    expect(got[0] == "https://x.test/signin", f"stored reusable URI first: {got}")
    expect(got[1] == "https://x.test/", f"origin root second: {got}")
    expect("https://x.test/login" in got, f"well-known paths: {got}")
    expect(not any("invite" in uri for uri in got), f"no single-use URI: {got}")
    expect(len(got) <= 6, f"cap: {got}")
    expect(len(set(got)) == len(got), f"no duplicates: {got}")

    stored_login = origins.login_candidates(("https://x.test/login",), "https://x.test")
    expect(stored_login.count("https://x.test/login") == 1, f"stored path not repeated: {stored_login}")

    none_usable = origins.login_candidates(("https://x.test/invite/abc",), "https://x.test")
    expect(none_usable[0] == "https://x.test/", f"root first when every URI is single-use: {none_usable}")
    expect(none_usable == ["https://x.test/"] + ["https://x.test" + p for p in origins.WELL_KNOWN_LOGIN_PATHS],
           f"every well-known path is reachable under the cap: {none_usable}")

    long_uri = "https://x.test/" + "a" * 9000
    expect(long_uri not in origins.login_candidates((long_uri,), "https://x.test"), "URL over MAX_URL dropped")


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
        expect_bw_error(run, lambda: bw.get_totp(item_id("B")), "absent", "no TOTP on item B")
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


class StubSigner:
    """A signer that answers every call 200 with an empty body and records the paths only.

    The bodies carry the password, so keeping them would put a secret in the test's own
    memory and in the text expect_clean searches.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, method, path, body):
        self.calls.append(path)
        return 200, {}


def signin_over(run, bw, resync=None):
    """A SignIn on the run's fake vault with a stub signer; returns (vault, signin, signer)."""
    v = vault.Vault({"personal": bw}, vault.Denylist(run.tmp / "denylist.json"))
    signer = StubSigner()
    return v, signin.SignIn(v, {"personal": bw}, signer, resync=resync), signer


def hidden_password_run(tmp):
    """A run whose only item is a login with no readable password, as Hide Passwords returns."""
    return Run(Path(tmp), items=[login_item("A", FOLDER_AGENT, password=None,
                                            uris=[("https://a.example.com", None)])])


def reveal_password(run) -> None:
    """Put the password back in the fake vault, as a sync from the server would."""
    data = json.loads(run.vault_file.read_text(encoding="utf-8"))
    data["items"][0]["login"]["password"] = ITEM_PASSWORDS["A"]
    run.vault_file.write_text(json.dumps(data), encoding="utf-8")


def test_fill_syncs_once_when_the_password_is_not_readable() -> None:
    """The local vault lags the server by up to a sync interval, so a password a sync reveals still fills."""
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = hidden_password_run(tmp)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        syncs: list[str] = []

        def resync(slug):
            syncs.append(slug)
            reveal_password(run)  # the server held it all along; the local copy was stale
            bw.sync()
            return True

        v, si, signer = signin_over(run, bw, resync=resync)
        item = v.get(f"bwbroker:personal/{item_id('A')}")
        expect(item is not None, "the item is listed")
        si._fill(item, 3, "password")
        expect(syncs == ["personal"], f"one sync of the item's account, got {syncs}")
        expect(signer.calls == ["/session/fill"], f"the fill reached the signer: {signer.calls}")
        run.expect_clean(out.getvalue())


def test_fill_refuses_a_password_a_sync_does_not_reveal() -> None:
    """Hide Passwords is a permission, not a transient failure, so the agent is not told to retry."""
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = hidden_password_run(tmp)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        syncs: list[str] = []

        def resync(slug):
            syncs.append(slug)
            bw.sync()  # a real sync that changes nothing: the server will not yield it either
            return True

        v, si, signer = signin_over(run, bw, resync=resync)
        item = v.get(f"bwbroker:personal/{item_id('A')}")
        try:
            si._fill(item, 3, "password")
        except signin.SignInError as exc:
            run.record(exc)
            expect(exc.code == "no_password", f"got {exc.code}, want no_password")
            expect(exc.status != 503, f"no_password must not read as a transient 503, got {exc.status}")
        else:
            fail("a password the account cannot read was filled")
        expect(syncs == ["personal"], f"exactly one sync, not a loop: {syncs}")
        expect(signer.calls == [], f"nothing reached the signer: {signer.calls}")
        expect(bw.state == "unlocked", f"the account moved to {bw.state}")
        run.expect_clean(out.getvalue())


def test_fill_does_not_sync_without_a_hook() -> None:
    """SignIn is built without resync in tests and by bitwarden-setup; the refusal must still be the honest one."""
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = hidden_password_run(tmp)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        v, si, signer = signin_over(run, bw)
        item = v.get(f"bwbroker:personal/{item_id('A')}")
        try:
            si._fill(item, 3, "password")
        except signin.SignInError as exc:
            run.record(exc)
            expect(exc.code == "no_password", f"got {exc.code}, want no_password")
        else:
            fail("a password the account cannot read was filled")
        expect(signer.calls == [], f"nothing reached the signer: {signer.calls}")
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


def test_real_bw_stderr() -> None:
    """bw 2026.10.1's own text against Vaultwarden 1.37.4, from scripts/test-vw-register.py."""
    for text, want in (
        ("Username or password is incorrect. Try again", ("auth", None)),  # bw login, wrong password
        ("Incorrect client_secret", ("auth", None)),  # bw login --apikey, wrong secret
        ("Rate limit exceeded. Try again later.", ("rate_limited", 60)),  # any 429; no Retry-After printed
        ('Syncing failed: {"response":{"error":{"code":401,"reason":"Unauthorized","description":"The request '
         'requires user authentication."}},"statusCode":401}', ("auth", None)),
        ("Not found.", ("unknown", None)),
        ("Vault is locked.", ("locked", None)),
        ("FetchError: request to https://vaultwarden.vault.internal/identity/connect/token failed, "
         "reason: unable to verify the first certificate", ("network", None)),
    ):
        expect(bwcli.classify(NODE_WARNING + text) == want, f"{text[:40]!r}: {bwcli.classify(text)}")
    # bw unlock with a wrong password. Only unlock reads it as auth: elsewhere it means an item that
    # does not decrypt, which must not lock the account.
    decrypt = NODE_WARNING + "Cryptography error, The decryption operation failed"
    expect(bwcli.classify(decrypt, unlock=True) == ("auth", None), "unlock decryption failure is not auth")
    expect(bwcli.classify(decrypt) == ("unknown", None), "decryption failure outside unlock is not unknown")


def test_node_warning_noise() -> None:
    expect(bwcli.classify(NODE_WARNING + "Not found.") == ("unknown", None), "PID read as a status code")
    expect(bwcli.classify(NODE_WARNING + "Too Many Requests (429). Retry-After: 120") == ("rate_limited", 120), "429")
    expect(bwcli.classify("Request failed with status code 429") == ("rate_limited", 60), "status code 429")
    expect(bwcli.classify("Request failed with status code 401") == ("auth", None), "status code 401")
    expect(bwcli.classify("connect ECONNREFUSED 10.0.0.1:443") == ("network", None), "network")
    expect(bwcli.classify("Item 401 is locked in a drawer") == ("unknown", None), "bare words")
    # bw's first line when NODE_EXTRA_CA_CERTS names a missing file (no local CA yet).
    ca_warning = ("Warning: Ignoring extra certs from `/secrets/local/tls/ca.crt`, load failed: "
                  "error:80000002:system library::No such file or directory\n")
    expect(bwcli.classify(ca_warning + "Not found.") == ("unknown", None), "missing CA warning read as an error")
    expect(bwcli.classify(ca_warning + "Vault is locked.") == ("locked", None), "locked behind the CA warning")
    expect(bwcli.classify(ca_warning + "Request failed with status code 401") == ("auth", None),
           "401 behind the CA warning")
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp), stderr_prefix=NODE_WARNING)
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        expect_bw_error(run, lambda: bw.get_totp(item_id("B")), "absent", "no TOTP behind a Node warning")
        # 99999999-... is in no fixture vault. ffffffff-... used to stand in for it, but that is item F,
        # a secure note, so the read answered "No password available" and passed as unknown by accident.
        expect_bw_error(run, lambda: bw.get_password("99999999-0000-4000-8000-000000000000"), "unknown",
                        "not found behind a Node warning")
        expect_bw_error(run, lambda: bw.get_password(item_id("F")), "absent",
                        "a non-login item holds no password")
        expect("4014290" in run.offered_stderr(), "the fake did not print the warning")
        expect(bw.state == "unlocked" and bw.error_class == "" and bw.retry_at is None, "account state changed")
        expect(bw.get_password(item_id("A")) == ITEM_PASSWORDS["A"], "session kept")
        manual = run.bw(run.account("manual", unlock="manual"))
        manual.login()
        expect_bw_error(run, lambda: manual.unlock(WRONG_PASSWORD), "auth", "wrong password behind a warning")
        run.expect_clean(out.getvalue())


def test_absent_is_its_own_class() -> None:
    """bw's "this item holds no such value" text is classified, so a caller can tell it from a broken call."""
    expect(bwcli.classify("No password available for this login.") == ("absent", None),
           "a login whose password the account cannot read")
    expect(bwcli.classify(NODE_WARNING + "No TOTP available for this login.") == ("absent", None),
           "a login with no TOTP seed, behind a Node warning")
    expect(bwcli.classify("Not found.") == ("unknown", None), "an id bw does not have stays unknown")


def test_absent_leaves_the_account_alone() -> None:
    """A value one item does not hold says nothing about the account, so neither state nor error_class moves."""
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp), items=[login_item("A", FOLDER_AGENT, password=None,
                                               uris=[("https://a.example.com", None)])])
        bw = run.bw(run.account("personal"))
        bw.login()
        bw.unlock()
        expect_bw_error(run, lambda: bw.get_password(item_id("A")), "absent",
                        "a password the account cannot read")
        expect(bw.state == "unlocked", f"state moved to {bw.state}")
        expect(bw.error_class == "", f"error_class moved to {bw.error_class!r}")
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

    ``page`` picks what the fill function reports: ok, navigated, no_field, cleared (a setter
    ran and a page handler emptied the field), write_failed, exception or hang. Before each real reply it sends an event and two decoy replies that
    claim success: one with the right id and another session, and a stale one with an older id.
    Runs in the Hermes image (websockets 15) or, with BITWARDEN_TEST_IMAGE, in the broker image
    (Debian's websockets 10.4), so it serves HTTP through either process_request signature.
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

    def http(self, *args):
        # websockets 13+ passes (connection, request); 10.4 to 12 pass (path, headers).
        legacy = isinstance(args[0], str)
        path = args[0] if legacy else args[1].path
        if path == "/json/list":
            body = [{"id": t["id"], "type": t.get("type", "page"), "url": t["url"], "title": "Sign in",
                     "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{t['id']}"}
                    for t in self.targets]
        elif path == "/json/version":
            # Chromium reports whatever host it was asked on; the broker must ignore it.
            body = {"Browser": "HeadlessChrome/140.0.0.0",
                    "webSocketDebuggerUrl": "ws://chromium.invalid:9222/devtools/browser/0f1e2d3c-4b5a-6978"}
        else:
            return None
        if legacy:
            from http import HTTPStatus
            return HTTPStatus.OK, [("Content-Type", "application/json")], json.dumps(body).encode()
        return args[0].respond(200, json.dumps(body))

    async def handle(self, ws):
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            # An unrelated event before every reply: the client must match replies by id.
            await ws.send(json.dumps({"method": "Target.targetInfoChanged", "params": {"targetInfo": {}}}))
            decoy = {"sessionId": "DEC0Y", "result": {"type": "object", "objectId": "9.9.9", "value": {"filled": 1}}}
            await ws.send(json.dumps({"id": msg["id"], "sessionId": "DEC0Y" if not msg.get("sessionId") else "0THER",
                                      "result": decoy}))
            await ws.send(json.dumps({"id": msg["id"] - 1, **({"sessionId": msg["sessionId"]} if msg.get("sessionId")
                                      else {}), "result": decoy}))
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
                    outcome = {"ok": {"filled": 1, "wrote": 1}, "no_field": {"filled": 0},
                               "cleared": {"filled": 0, "wrote": 1},
                               "navigated": {"refused": "origin_changed"},
                               "write_failed": {"refused": "write_failed"}}[self.page]
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


def test_write_unconfirmed() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="cleared") as fake:
        result, out = run_fill(fake)
    expect(result == cdp.FillResult(False, "https://login.example.com", "write_unconfirmed"),
           f"cleared result {result!r}")
    expect_detached(fake, "cleared")
    expect_no_value([out, repr(result)], "cleared")


def test_write_failed() -> None:
    with fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="write_failed") as fake:
        result, out = run_fill(fake)
    expect(not result.filled and result.error == "page_error", f"write failed result {result!r}")
    expect_detached(fake, "write failed")


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
    test_write_unconfirmed()
    test_write_failed()
    test_fill_timeout()


def test_fill_rejects_bad_arguments() -> None:
    """Port and target id are checked before any I/O, so these need no server."""
    async def must_not_connect(*a, **k):
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


@contextlib.contextmanager
def drip_endpoint(drip_path: str):
    """An HTTP endpoint on 127.0.0.1 that sends headers for ``drip_path`` and then one body byte
    every 0.9 s; ``/json/list`` otherwise answers at once with the login target."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def answer(conn):
        with conn:
            path = conn.recv(4096).split(b" ")[1].decode("ascii", "replace")
            if path == drip_path:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                for _ in range(8):
                    if stop.wait(0.9):
                        return
                    conn.sendall(b" ")
            else:
                body = json.dumps([{"id": TARGET, "type": "page", "url": LOGIN_URL}]).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=answer, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        srv.close()


def test_fill_bounded_by_timeout() -> None:
    """A slow body on either GET, through fill and fill_async, ends at the deadline."""
    import asyncio
    calls = {
        "fill": lambda port: cdp.fill(port, TARGET, ("https://login.example.com",), CDP_VALUE, timeout=2.0),
        "fill_async": lambda port: asyncio.run(
            cdp.fill_async(port, TARGET, ("https://login.example.com",), CDP_VALUE, timeout=2.0)),
    }
    for drip_path in ("/json/list", "/json/version"):
        for name, call in calls.items():
            with drip_endpoint(drip_path) as port, captured() as out:
                start = time.monotonic()
                got = call(port)
                elapsed = time.monotonic() - start
            want_origin = "" if drip_path == "/json/list" else "https://login.example.com"
            expect(got == cdp.FillResult(False, want_origin, "cdp_unavailable"), f"{name} drip {drip_path}: {got!r}")
            expect(elapsed < 2.5, f"{name} drip {drip_path}: took {elapsed:.2f}s with a 2s timeout")
            expect_no_value([out.getvalue(), repr(got)], f"{name} drip {drip_path}")


# --- Broker server ----------------------------------------------------------

BROKER_PKG = ROOT / "compose" / "bitwarden-broker"
CLIENT_ID = "user.00000000-0000-4000-8000-000000000001"
STACK_FOLDER = "0f0d0000-0000-4000-8000-00000000000d"
STACK_VALUE = "stack-" + secrets.token_urlsafe(18)
BASE = 1_000_003


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def handle(slug: str, name: str) -> str:
    return f"bwbroker:{slug}/{item_id(name)}"


def stack_vault(**extra) -> dict:
    """vault_data plus a "Stack" folder holding one stack secret and one secure note."""
    data = vault_data(**extra)
    data["folders"].append({"id": STACK_FOLDER, "name": "Stack"})
    data["items"].append({"object": "item", "id": "5555aaaa-0000-4000-8000-000000000000", "type": 1,
                          "name": "llm-proxy/openai", "folderId": STACK_FOLDER, "collectionIds": [],
                          "organizationId": None, "login": {"username": None, "password": STACK_VALUE,
                                                            "totp": None, "uris": None}})
    data["items"].append({"object": "item", "id": "5555bbbb-0000-4000-8000-000000000000", "type": 2,
                          "name": "a note", "folderId": STACK_FOLDER, "collectionIds": [], "notes": "n"})
    return data


class Broker:
    """``python3 -m broker.server`` in a subprocess, with the fake bw behind a wrapper script.

    Every TCP response body is kept in ``bodies`` so a test can search them, and the
    server's stdout and stderr go to ``broker-stderr.log`` under the run's temp dir,
    which ``Run.expect_clean`` scans.
    """

    def __init__(self, run: Run, sync_minutes: str = "10", idle_minutes: str = "0", vault: dict | None = None):
        self.run = run
        if vault is not None:
            run.vault_file.write_text(json.dumps(vault), encoding="utf-8")
        self.instances_dir = run.tmp / "instances"
        self.instances_dir.mkdir(exist_ok=True)
        self.socket = run.tmp / "sync" / "sync.sock"
        self.signin_socket = run.tmp / "signin" / "signin.sock"
        self.stderr_path = run.tmp / "broker-stderr.log"
        self.port = free_port()
        self.bodies: list[str] = []
        self.proc = None
        wrapper = run.tmp / "bw-wrapper"
        exports = "".join(f"export {k}='{v}'\n" for k, v in run.extra_env.items())
        wrapper.write_text(f'#!/bin/sh\n{exports}exec "{sys.executable}" "{FAKE_BW}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(run.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BITWARDEN_INSTANCES_DIR": str(self.instances_dir),
            "BITWARDEN_DATA_DIR": str(run.tmp),
            "BITWARDEN_SECRETS_DIR": str(run.secrets_root),
            "BITWARDEN_SYNC_MINUTES": sync_minutes,
            "BITWARDEN_IDLE_LOCK_MINUTES": idle_minutes,
            "BITWARDEN_LISTEN": f"127.0.0.1:{self.port}",
            "BITWARDEN_SYNC_SOCKET": str(self.socket),
            "BITWARDEN_SIGNIN_SOCKET": str(self.signin_socket),
            "BITWARDEN_BW": str(wrapper),
            "BITWARDEN_SYNC_MIN_SECONDS": "0",  # test_sync_coalesced sets 30
        }

    def add_account(self, slug, scope="folder:Agent", unlock="stored", extra=None) -> None:
        """An instance file as addon_store writes it, and secrets/<slug>/ with the client_id."""
        self.run.account(slug, scope=scope, unlock=unlock)
        write_instance(self.instances_dir, slug, {"mode": "cloud", "region": "us", "scope": scope,
                                                  "unlock": unlock, **(extra or {})},
                       self.run.secrets_root, CLIENT_ID)

    def start(self, unlocked=(), expect_exit=False) -> None:
        import subprocess
        log = open(self.stderr_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "broker.server"], cwd=BROKER_PKG, env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        log.close()
        if expect_exit:
            try:
                code = self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.stop()
                fail("broker kept running when it should have refused to start")
            expect(code != 0, "broker exited 0 when it should have refused to start")
            self.proc = None
            return
        self.wait_for(lambda: self.try_call("GET", "/health") is not None, "broker answered /health")
        for slug in unlocked:
            # The state turns unlocked before the first listing ends; every test scope has items.
            self.wait_for(lambda: self.state(slug) == "unlocked"
                          and any(h.startswith(f"bwbroker:{slug}/") for h in self.handles()), f"{slug} unlocked")

    def stop(self) -> None:
        import subprocess
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            fail("broker did not stop on SIGTERM")
        self.proc = None

    def wait_for(self, predicate, msg: str, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                fail(f"{msg}: broker exited with {self.proc.returncode}:\n{self.stderr()}")
            if predicate():
                return
            time.sleep(0.05)
        fail(f"timed out waiting: {msg}")

    def try_call(self, method, path, body=None):
        try:
            return self.call(method, path, body)
        except OSError:
            return None

    def call(self, method, path, body=None, raw: bytes | None = None, headers=None, timeout=30):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
            conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
        finally:
            conn.close()
        self.bodies.append(text)
        try:
            return resp.status, json.loads(text) if text else None
        except ValueError:
            fail(f"{method} {path}: response is not JSON")

    def sock_call(self, method, path, body=None):
        from broker.unlock import UnixHTTPConnection
        conn = UnixHTTPConnection(str(self.socket), timeout=30)
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
        finally:
            conn.close()
        return resp.status, json.loads(text)

    def state(self, slug: str) -> str | None:
        got = self.try_call("GET", "/accounts")
        if got is None or got[0] != 200:
            return None
        return next((a["state"] for a in got[1] if a["slug"] == slug), None)

    def handles(self) -> list[str]:
        status, items = self.call("GET", "/items")
        expect(status == 200, f"/items {status}")
        return [i["handle"] for i in items]

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8") if self.stderr_path.exists() else ""

    def expect_clean(self, extra_secrets=()) -> None:
        expect("Traceback" not in self.stderr(), f"broker printed a traceback:\n{self.stderr()}")
        self.run.expect_clean("\n".join(self.bodies), extra_secrets)


@contextlib.contextmanager
def broker_run(**kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp))
        b = Broker(run, **kwargs)
        try:
            yield b
        finally:
            b.stop()


def fill_body(h, field="password", base=BASE, port=None, target=TARGET) -> dict:
    body = {"handle": h, "target_id": target, "port": port or free_port(), "field": field}
    if base is not None:
        body["base"] = base
    return body


def test_api_returns_no_values() -> None:
    with broker_run(vault=stack_vault()) as b:
        b.add_account("personal", extra={"secrets_item_scope": "folder:Stack"})
        b.start(unlocked=["personal"])
        a = handle("personal", "A")

        status, accts = b.call("GET", "/accounts")
        expect(status == 200 and accts == [{"slug": "personal", "mode": "cloud", "scope": "folder",
                                            "state": "unlocked", "error_class": "", "logged_in": True,
                                            "retry_after": None}], f"/accounts {accts}")
        status, items = b.call("GET", "/items")
        expect(status == 200 and sorted(i["handle"] for i in items) == [a, handle("personal", "E")], f"/items {items}")
        expect(next(i for i in items if i["handle"] == a) == {
            "handle": a, "label": "Item A", "username": "user-a@example.com",
            "origins": ["https://login.example.com"], "has_totp": True, "sign_in_only": False},
            "item A metadata")
        status, health = b.call("GET", "/health")
        expect(status == 200 and health["status"] == "ok", f"/health {health}")
        expect(b.call("POST", "/sync", {}) == (200, {"synced": ["personal"]}), "/sync")
        status, got = b.call("POST", "/requests", {"account": "personal", "site": "other.example.com",
                                                    "reason": "to read the feed"})
        expect(status == 200 and list(got) == ["request_id"], f"/requests POST {got}")
        status, got = b.call("GET", "/requests")
        expect(status == 200 and [r["site"] for r in got["pending"]] == ["other.example.com"], f"/requests {got}")

        for field in ("password", "totp"):
            status, got = b.call("POST", "/fill", fill_body(a, field))
            expect(status == 200 and got["filled"] is False and got["error"] == "cdp_unavailable",
                   f"/fill {field} on a closed port: {status} {got}")
            # A write may have started before the timeout, so the digests come back.
            expect(set(got["redaction"] or {}) == {"key", "base", "forms"} and got["redaction"]["base"] == BASE
                   and got["redaction"]["forms"], f"{field} redaction")
        argv = [line["argv"][:2] for line in b.run.log_lines()]
        expect(["get", "password"] in argv and ["get", "totp"] in argv, "the broker fetched both values")

        # Refusals: each is a fixed error code and fills nothing.
        refusals = [
            (fill_body(a, base=None), 400, "invalid_base"),
            (fill_body(a, base=255), 400, "invalid_base"),
            (fill_body(a, base=2**61 - 1), 400, "invalid_base"),
            (fill_body(a, base="1000003"), 400, "invalid_base"),
            (fill_body(a, base=True), 400, "invalid_base"),
            (fill_body(a, field="username"), 400, "invalid_field"),
            (fill_body(handle("personal", "E"), field="totp"), 400, "no_totp"),
            (fill_body(handle("personal", "B")), 404, "unknown_handle"),
            (fill_body("bwbroker:nobody/x"), 404, "unknown_handle"),
            ({**fill_body(a), "extra": 1}, 400, "bad_request"),
            ({**fill_body(a), "port": "9222"}, 400, "bad_request"),
        ]
        before = len(b.run.log_lines())
        for body, want_status, want_error in refusals:
            got = b.call("POST", "/fill", body)
            expect(got == (want_status, {"error": want_error}), f"refusal {want_error}: {got}")
        expect(len(b.run.log_lines()) == before, "a refused /fill ran bw")

        expect(b.call("GET", "/secrets") == (404, {"error": "not_found"}), "TCP /secrets")
        expect(b.call("POST", "/unlock", {"slug": "personal", "password": "x"}) == (404, {"error": "not_found"}),
               "TCP /unlock")
        expect(b.call("POST", "/lock", {"slug": "personal"}) == (404, {"error": "not_found"}), "TCP /lock")
        expect(b.call("GET", "/nope") == (404, {"error": "not_found"}), "unknown route")
        expect(b.call("GET", "/fill") == (405, {"error": "method_not_allowed"}), "GET /fill")
        expect(b.call("DELETE", "/items") == (405, {"error": "method_not_allowed"}), "DELETE /items")
        expect(b.call("POST", "/requests", raw=b"[1]") == (400, {"error": "bad_request"}), "a JSON list")
        expect(b.call("POST", "/requests", raw=b'{"site": "a.example.com", "site": "b.example.com"}')
               == (400, {"error": "bad_request"}), "a duplicate key")
        expect(b.call("POST", "/requests", raw=b'{"site": NaN}') == (400, {"error": "bad_request"}), "NaN")
        expect(b.call("POST", "/requests", raw=b"\xff") == (400, {"error": "bad_request"}), "not UTF-8")
        expect(b.call("POST", "/requests", raw=b"x" * (64 * 1024 + 1)) == (413, {"error": "too_large"}),
               "a body over 64 KiB")
        expect(b.call("GET", "/items?debug=1")[0] == 200, "a query string is ignored")

        expect(b.call("POST", "/release", {"handle": handle("personal", "E")})
               == (200, {"released": True}), "/release")

        stderr = b.stderr()
        expect(f"POST /fill 200 {a}" in stderr, f"the access log names the handle:\n{stderr}")
        expect("GET /nope" not in stderr and "debug" not in stderr, "the access log repeats an unknown path")
        b.stop()
        b.expect_clean(extra_secrets=[STACK_VALUE])


def test_sanitize_keeps_only_signin_fields() -> None:
    item = login_item("A", uris=[("https://login.example.com", None)], fields=[
        {"name": "agent-signin-idp", "value": "https://sso.example.com, ftp://x, junk", "type": 0},
        {"name": "other", "value": "CANARY", "type": 1},
    ])
    out = bwcli._sanitize(item)
    expect(out["in_page_fill"] is True, f"in_page_fill {out['in_page_fill']!r}")
    expect(out["idp_origins"] == ["https://sso.example.com"], f"idp_origins {out['idp_origins']!r}")
    expect("CANARY" not in json.dumps(out), "a custom field value survived _sanitize")
    for value, type_ in (("false", 2), ("true", 0), ("TRUE", 2), (True, 2)):
        item["fields"][0] = {"name": "agent-in-page-fill", "value": value, "type": type_}
        expect(bwcli._sanitize(item)["in_page_fill"] is False, f"in_page_fill for {value!r} type {type_}")
    item["fields"] = "junk"
    out = bwcli._sanitize(item)
    expect(out["in_page_fill"] is False and out["idp_origins"] == [], "malformed fields")
    del item["fields"]
    expect(bwcli._sanitize(item)["in_page_fill"] is False, "no fields")


def sign_in_vault() -> dict:
    data = vault_data()
    data["items"] = [
        login_item("A", FOLDER_AGENT, uris=[("https://login.example.com", None)]),
        login_item("E", FOLDER_AGENT, uris=[("https://good.example.com", None)], in_page=False, fields=[
            {"name": "agent-signin-idp", "value": "https://sso.example.com", "type": 0}]),
    ]
    return data


def test_items_sign_in_only_flag() -> None:
    with broker_run(vault=sign_in_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        status, items = b.call("GET", "/items")
        flags = {i["handle"]: i["sign_in_only"] for i in items}
        expect(status == 200 and flags == {handle("personal", "A"): False, handle("personal", "E"): True},
               f"/items sign_in_only {flags}")
        b.stop()
        b.expect_clean()


def test_fill_refuses_sign_in_only() -> None:
    with broker_run(vault=sign_in_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        e = handle("personal", "E")
        before = len(b.run.log_lines())
        for field in ("password", "totp"):
            got = b.call("POST", "/fill", fill_body(e, field))
            expect(got == (409, {"error": "sign_in_only"}), f"/fill {field} on a sign-in-only item: {got}")
        expect(len(b.run.log_lines()) == before, "a refused /fill ran bw")
        b.stop()
        b.expect_clean()


# --- Sign-in routes, with a fake signer on a temp socket --------------------

SIGNIN_ORIGIN = "https://login.example.com"
SIGNIN_URI = "https://login.example.com/signin?next=/home"
IDP_ORIGIN = "https://sso.example.com"


def ax_node(ref, role, name="", text="") -> dict:
    return {"ref": ref, "role": role, "name": name, "text": text}


def ax_page(origin, *nodes) -> dict:
    return {"origin": origin, "nodes": list(nodes)}


def ax_error(code, status) -> dict:
    return {"error": code, "status": status}


UNKNOWN_PAGE = ax_page(SIGNIN_ORIGIN, ax_node(1, "StaticText", "", "Nothing to sign in to"))


class FakeSigner:
    """The signer's socket API with scripted pages, on a Unix socket.

    ``pages`` is what the session shows: ``/session/open`` returns the first,
    ``/session/click`` and ``/session/navigate`` move to the next and return it,
    and the other session routes return the current one. A page answering
    ``timeout`` ends the session, as the signer's does. A page from ``ax_error`` answers with that
    code instead. ``refuse`` maps a session route other than open and end to a
    ``(code, status)`` it always answers with, for a route the page alone
    cannot make fail. Every request is kept in ``calls`` as ``(route, body)``.

    ``session_on_failed_open`` False models the signer's Chromium failing to
    start: the open answers its error with no session behind it, as
    signer/server.py:_open does when _start_browser raises.
    """

    def __init__(self, path, pages=(), health="ready", export=None, open_session=False, refuse=None,
                 session_on_failed_open=True):
        self.path = Path(path)
        self.pages = list(pages) or [UNKNOWN_PAGE]
        self.health = health
        self.export = export or {"origin": SIGNIN_ORIGIN, "cookies": [], "localStorage": [],
                                 "sessionStorage": []}
        self.calls: list[tuple[str, dict]] = []
        self.refuse = dict(refuse or {})
        self.session_on_failed_open = session_on_failed_open
        self.session = open_session
        self.index = 0
        self.server = None
        self.lock = threading.Lock()

    # --- the API -----------------------------------------------------------

    def reply(self, route, body) -> tuple[int, dict]:
        with self.lock:
            self.calls.append((route, body))
            if route == "/health":
                return 200, {"status": "busy" if self.session and self.health == "ready" else self.health}
            if route == "/session/end":
                ended, self.session, self.index = self.session, False, 0
                return 200, {"ended": ended}
            if route == "/session/open":
                if self.session:
                    return 409, {"error": "busy"}
                self.index = 0
                status, body = self._page()
                self.session = "error" not in body or self.session_on_failed_open
                return status, body
            if not self.session:
                return 409, {"error": "no_session"}
            if route in self.refuse:
                code, status = self.refuse[route]
                return status, {"error": code}
            if route == "/session/export":
                return 200, dict(self.export)
            if route in ("/session/click", "/session/navigate"):
                self.index = min(self.index + 1, len(self.pages) - 1)
            status, body = self._page()
            if body.get("error") == "timeout":
                self.session, self.index = False, 0
            return status, body

    def _page(self) -> tuple[int, dict]:
        page = self.pages[self.index]
        if "error" in page:
            return page["status"], {"error": page["error"]}
        return 200, json.loads(json.dumps(page))

    def routes(self) -> list[str]:
        return [route for route, _ in self.calls]

    def bodies(self, route: str) -> list[dict]:
        return [body for name, body in self.calls if name == route]

    # --- serving -----------------------------------------------------------

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.server = _SignerServer(str(self.path), _SignerHandler)
        self.server.fake = self
        threading.Thread(target=self.server.serve_forever, name="fake-signer", daemon=True).start()

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        try:
            os.unlink(self.path)
        except OSError:
            pass


class _SignerHandler(BaseHTTPRequestHandler):
    def address_string(self) -> str:
        return "unix"

    def log_message(self, format, *args) -> None:  # noqa: A002 - the stdlib's signature
        pass

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        status, payload = self.server.fake.reply(self.path.split("?")[0], body)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _dispatch


class _SignerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


@contextlib.contextmanager
def fake_signer(b: Broker, **kwargs):
    fake = FakeSigner(b.signin_socket, **kwargs)
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


def signin_vault() -> dict:
    """Item A on login.example.com with a TOTP key; item E on good.example.com with an IdP origin."""
    data = vault_data()
    data["items"] = [
        login_item("A", FOLDER_AGENT, uris=[(SIGNIN_URI, None)], totp=TOTP_SEED),
        login_item("E", FOLDER_AGENT, uris=[("https://good.example.com", None)], in_page=False, fields=[
            {"name": "agent-signin-idp", "value": IDP_ORIGIN, "type": 0}]),
    ]
    return data


def signin_body(**extra) -> dict:
    return {"target_id": TARGET, "port": free_port(), **extra}


def expect_no_canaries(texts, values, msg: str) -> None:
    """None of ``values`` appears in ``texts``, in any encoding the digest redaction scrubs."""
    from broker import digest_redaction
    blob = "\n".join(texts).encode("utf-8", "surrogatepass")
    for value in values:
        for form in digest_redaction.FORMS:
            expect(digest_redaction._encode(value, form) not in blob,
                   f"{msg}: the {form} form of a value appeared in a reply")


def test_signin_routes_keep_tcp_checks() -> None:
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b) as fake:
            for host in ("evil.example.com", f"evil.example.com:{b.port}", f"127.0.0.1:{b.port + 1}"):
                got = b.call("POST", "/signin/cancel", {}, headers={"Host": host})
                expect(got == (403, {"error": "bad_host"}), f"Host {host!r}: {got}")
            got = b.call("POST", "/signin/cancel", {}, headers={"Origin": "https://evil.example.com"})
            expect(got == (403, {"error": "origin_refused"}), f"a request with Origin: {got}")
            got = b.call("POST", "/signin/cancel", {}, headers={"Content-Type": "text/plain"})
            expect(got == (415, {"error": "unsupported_media_type"}), f"a POST without JSON: {got}")
            expect(b.call("GET", "/signin/cancel") == (405, {"error": "method_not_allowed"}), "GET /signin/cancel")
            for body, route in (({"ref": 1, "extra": 1}, "/signin/click"), ({}, "/signin/click"),
                                ({"ref": 0}, "/signin/click"), ({"ref": True}, "/signin/click"),
                                ({"ref": 1, "text": 7}, "/signin/type"),
                                ({"ref": 1, "field": "username"}, "/signin/fill"),
                                ({"handle": 7, "target_id": TARGET, "port": 8}, "/signin/start"),
                                ({"handle": "x", "target_id": TARGET, "port": "8"}, "/signin/start"),
                                ({"port": 8}, "/signin/finish"),
                                ({"target_id": TARGET, "port": 0}, "/signin/finish")):
                got = b.call("POST", route, body)
                expect(got[0] == 400 and got[1] == {"error": "bad_request"}, f"{route} {body}: {got}")
            expect(fake.calls == [], f"a refused request reached the signer: {fake.routes()}")
            expect(b.call("POST", "/signin/cancel", {}) == (200, {"cancelled": True}), "/signin/cancel")
            expect(fake.routes() == ["/session/end"], f"cancel sent {fake.routes()}")
        b.stop()
        b.expect_clean()


def test_no_route_returns_canary() -> None:
    """Every /signin/ route, with the fake signer scripted to need a password and a TOTP code."""
    pages = [ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Password"), ax_node(2, "button", "Sign in")),
             ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "One-time code"), ax_node(2, "button", "Verify")),
             # An IdP origin at the end, so the sequence stops there and the session stays
             # open for the routes the test drives by hand.
             ax_page(IDP_ORIGIN, ax_node(1, "StaticText", "", "Continue"))]
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        with fake_signer(b, pages=pages) as fake:
            b.call("POST", "/signin/start", signin_body(handle=a))
            b.call("POST", "/signin/snapshot", {})
            b.call("POST", "/signin/click", {"ref": 2})
            b.call("POST", "/signin/type", {"ref": 1, "text": "someone@example.com"})
            b.call("POST", "/signin/fill", {"ref": 1, "field": "password"})
            b.call("POST", "/signin/fill", {"ref": 1, "field": "totp"})
            b.call("POST", "/signin/finish", signin_body())
            b.call("POST", "/signin/cancel", {})
            written = [body["value"] for body in fake.bodies("/session/fill")]
        expect(ITEM_PASSWORDS["A"] in written, f"the password never reached the signer: {len(written)} fills")
        codes = [v for v in written if v != ITEM_PASSWORDS["A"]]
        expect(codes and all(c.isdigit() for c in codes), f"no TOTP code reached the signer: {len(codes)}")
        expect_no_canaries(b.bodies, written, "/signin routes")
        b.stop()
        b.expect_clean(extra_secrets=codes)


def test_sequence_opens_the_item_uri() -> None:
    """The item's URI names a path, so the sign-in opens that page, not the site root."""
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(got[0] == 200, f"/signin/start {got}")
            opened = fake.bodies("/session/open")[0]
            expect(opened["url"] == SIGNIN_URI, f"opened {opened['url']!r}, want {SIGNIN_URI!r}")
            # The signer loads a URL only when its normalized origin is in allowed
            # (signer/session.py:url_allowed), which the item's first origin is.
            expect(origins.normalize_origin(opened["url"]) == SIGNIN_ORIGIN
                   and opened["allowed"][0] == SIGNIN_ORIGIN,
                   f"the URI is not on an allowed origin: {opened}")
            status, items = b.call("GET", "/items")
            listed = next(i for i in items if i["handle"] == handle("personal", "A"))
            expect(listed["origins"] == [SIGNIN_ORIGIN], f"/items still lists origins: {listed}")
        b.stop()
        b.expect_clean()


LADDER_LOGIN = ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Username"),
                       ax_node(2, "textbox", "Password"), ax_node(3, "button", "Log in"))
LADDER_USERNAME_ONLY = ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Email"),
                               ax_node(2, "button", "Next"))
INVITE_URI = "https://login.example.com/invite/AAAAAAAAAAAAAAAAAAAA"


def invite_vault(extra_uris=()) -> dict:
    """Item A whose only saved URI is a single-use invitation link."""
    data = vault_data()
    uris = [(INVITE_URI, None)] + [(uri, None) for uri in extra_uris]
    data["items"] = [login_item("A", FOLDER_AGENT, uris=uris, in_page=False)]
    return data


def ladder_start(fake_kwargs, vault=None):
    """One /signin/start against ``vault``, returning (status, body, fake)."""
    with broker_run(vault=vault or invite_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b, **fake_kwargs) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            urls = [body["url"] for route, body in fake.calls
                    if route in ("/session/open", "/session/navigate")]
            routes = [r for r in fake.routes()
                      if r in ("/session/open", "/session/navigate", "/session/snapshot")]
            status_body = got
        b.stop()
        b.expect_clean()
    return status_body, urls, routes


def test_open_walks_to_the_first_page_with_a_login_form() -> None:
    """The item's only URI is an invitation, so the ladder walks on to the login page."""
    (status, body), urls, routes = ladder_start({"pages": [UNKNOWN_PAGE, LADDER_LOGIN]})
    expect(status == 200, f"/signin/start: {status} {body}")
    expect(not any("invite" in url for url in urls), f"a single-use URI was opened: {urls}")
    expect(urls[0] == SIGNIN_ORIGIN + "/", f"the origin root is first: {urls}")
    expect(urls[1] == SIGNIN_ORIGIN + "/login", f"then the first well-known path: {urls}")
    expect(routes == ["/session/open", "/session/navigate"], f"one open then navigates: {routes}")


def test_open_stops_at_a_username_only_page() -> None:
    """A two-step login shows the identifier field first; that is a login form."""
    (status, body), urls, routes = ladder_start({"pages": [LADDER_USERNAME_ONLY, LADDER_LOGIN]})
    expect(status == 200, f"/signin/start: {status} {body}")
    expect(routes == ["/session/open"], f"the ladder stopped at the first page: {routes}")


def test_open_carries_on_past_page_error_and_origin_mismatch() -> None:
    """A candidate that will not load is a miss, not the end of the sign-in."""
    pages = [ax_error("page_error", 502), ax_error("origin_mismatch", 403), LADDER_LOGIN]
    (status, body), urls, routes = ladder_start({"pages": pages})
    expect(status == 200, f"/signin/start after two bad candidates: {status} {body}")
    expect(len(routes) == 3, f"every candidate tried: {routes}")


def test_open_stops_the_ladder_on_timeout() -> None:
    """timeout ends the signer's session, so the ladder cannot navigate again."""
    pages = [UNKNOWN_PAGE, ax_error("timeout", 504), LADDER_LOGIN]
    (status, body), urls, routes = ladder_start({"pages": pages})
    expect((status, body) == (504, {"error": "timeout"}), f"timeout surfaces: {status} {body}")
    expect(len(routes) == 2, f"no navigate after timeout: {routes}")


def test_open_caps_navigations() -> None:
    """Six navigations at most, however many candidates an item has."""
    extra = tuple(f"https://login.example.com/p{n}" for n in range(8))
    (status, body), urls, routes = ladder_start({"pages": [UNKNOWN_PAGE]},
                                                vault=invite_vault(extra_uris=extra))
    expect(len(routes) == 6, f"capped at six navigations: {routes}")
    expect(status == 200, f"the last snapshot is returned: {status} {body}")


def test_open_returns_the_last_snapshot_when_nothing_matches() -> None:
    """No login form anywhere: the agent gets a snapshot to carry on from, not an error."""
    (status, body), urls, routes = ladder_start({"pages": [UNKNOWN_PAGE]})
    expect((status, body) == (200, UNKNOWN_PAGE), f"snapshot, not an error: {status} {body}")


def test_open_raises_the_real_error_when_the_browser_never_started() -> None:
    """Chromium failing to start is page_error; the next candidate must not turn it into no_session."""
    (status, body), urls, routes = ladder_start(
        {"pages": [ax_error("page_error", 502), LADDER_LOGIN], "session_on_failed_open": False})
    expect((status, body) == (502, {"error": "page_error"}), f"page_error surfaces: {status} {body}")
    expect(routes == ["/session/open"], f"no navigate without a session: {routes}")


def test_open_does_not_return_a_snapshot_of_a_page_the_signer_left() -> None:
    """A miss can leave the browser elsewhere, so the snapshot kept from an earlier
    candidate is stale: the broker asks what the signer shows now instead."""
    pages = [UNKNOWN_PAGE, ax_error("origin_mismatch", 403)]
    (status, body), urls, routes = ladder_start({"pages": pages})
    expect((status, body) == (403, {"error": "origin_mismatch"}),
           f"the real state surfaces instead of a stale 200: {status} {body}")
    expect("/session/snapshot" in routes, f"the signer was asked for the current page: {routes}")


def test_handover_keeps_the_signers_cookie_rule() -> None:
    """The handover applies the signer's cookie rule again, with the same outcome."""
    sys.path.insert(0, str(ROOT / "compose" / "bitwarden-signin"))
    from signer import cookies as signer_cookies  # the signer runs in its own container, so this is a copy

    item_origins = ("https://app.example.com", "https://login.example.com:8443")
    jar = [{"name": n, "value": "v", "domain": d} for n, d in (
        ("exact", "app.example.com"), ("parent", ".example.com"), ("ported", "login.example.com"),
        ("sibling", "other.example.com"), ("idp", "sso.idp.test"), ("idp-domain", ".idp.test"),
        ("hostonly-parent", "example.com"), ("tld", ".com"), ("empty", ""))]
    kept = [c["name"] for c in signer_cookies.select(jar, item_origins)]
    hosts = origins.origin_hosts(item_origins)
    mine = [c["name"] for c in cdp._cookie_params(jar, hosts)]
    expect(kept == ["exact", "parent", "ported"], f"the signer keeps {kept}")
    expect(mine == kept, f"the handover keeps {mine}, the signer {kept}")
    expect(cdp._cookie_params(jar, set()) == [], "no item host, no cookie")


def test_fill_uses_item_origins_only() -> None:
    """The IdP origin is allowed for a click and left out of a fill and the export."""
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        e = handle("personal", "E")
        with fake_signer(b, pages=[ax_page("https://good.example.com",
                                           ax_node(1, "StaticText", "", "Hello"))]) as fake:
            status, _ = b.call("POST", "/signin/start", signin_body(handle=e))
            expect(status == 200, f"/signin/start {status}")
            b.call("POST", "/signin/click", {"ref": 1})
            b.call("POST", "/signin/fill", {"ref": 1, "field": "password"})
            b.call("POST", "/signin/finish", signin_body())
            allowed = {route: body["allowed"] for route, body in fake.calls if "allowed" in body}
        both = ["https://good.example.com", IDP_ORIGIN]
        expect(allowed["/session/open"] == both, f"open allowed {allowed['/session/open']}")
        expect(allowed["/session/click"] == both, f"click allowed {allowed['/session/click']}")
        expect(allowed["/session/fill"] == ["https://good.example.com"],
               f"fill allowed {allowed['/session/fill']}")
        expect(allowed["/session/export"] == ["https://good.example.com"],
               f"export allowed {allowed['/session/export']}")
        b.stop()
        b.expect_clean()


def test_submit_prefers_the_form_button_over_the_navigation() -> None:
    """Real login pages: a one-word button name, and a navigation button above the form.

    practice.expandtesting.com/login puts a "Demos" menu button before the
    fields and names its submit "Login". The sequence used to match neither
    name and fall back to the first button on the page, so it clicked the menu.
    """
    nav_then_form = [ax_node(1, "button", "Demos"), ax_node(2, "textbox", "Username"),
                     ax_node(3, "textbox", "Password"), ax_node(4, "button", "Login")]
    expect(signin._last_field(nav_then_form) == 3, "the last field the sequence recognises")
    expect(signin._submit(nav_then_form, signin._last_field(nav_then_form)) == 4,
           "the submit button after the fields, not the navigation above them")
    for name in ("Login", "Log in", "Signin", "Sign in", "Sign On", "Submit", "Next",
                 "Continue", "Verify"):
        nodes = [ax_node(1, "textbox", "Password"), ax_node(2, "button", name)]
        expect(signin._submit(nodes, 1) == 2, f"a button named {name!r} is a submit")
    for name in ("Demos", "Sign up", "Register", "Help"):
        expect(not signin._SUBMIT.search(name), f"a button named {name!r} is not a submit")
    # No name matches: the first button after the fields, not the first on the page.
    unnamed = [ax_node(1, "button", "Demos"), ax_node(2, "textbox", "Password"),
               ax_node(3, "button", ""), ax_node(4, "button", "")]
    expect(signin._submit(unnamed, 2) == 3, "the first button below the fields")
    # Nothing below them: the page's buttons as a whole, named first.
    above = [ax_node(1, "button", "Demos"), ax_node(2, "button", "Log in"),
             ax_node(3, "textbox", "Password")]
    expect(signin._submit(above, 3) == 2, "a named button above the fields when none is below")
    expect(signin._submit([ax_node(1, "textbox", "Password")], 1) is None, "no button, no ref")


def test_auto_sequence_clicks_the_form_button_not_the_navigation() -> None:
    """End to end through the broker: the click lands on the form's own button."""
    login = ax_page(SIGNIN_ORIGIN, ax_node(1, "button", "Demos"),
                    ax_node(2, "textbox", "Username"), ax_node(3, "textbox", "Password"),
                    ax_node(4, "button", "Login"))
    # Off the item's origins, so the sequence returns the snapshot and needs no handover.
    after = ax_page(IDP_ORIGIN, ax_node(1, "StaticText", "", "Signed in"))
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b, pages=[login, after]) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(got == (200, after), f"/signin/start: {got}")
            clicks = [body["ref"] for body in fake.bodies("/session/click")]
            expect(clicks == [4], f"the sequence clicked {clicks}, want the Login button (4)")
            expect(fake.bodies("/session/type")[0]["ref"] == 2, "the username field")
            expect(fake.bodies("/session/fill")[0]["ref"] == 3, "the password field")
        b.stop()
        b.expect_clean()


def test_auto_sequence_returns_snapshot_on_unknown_page() -> None:
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(got == (200, UNKNOWN_PAGE), f"/signin/start on an unknown page: {got}")
            # The ladder walks the candidates; what this test pins is that the
            # sequence itself took no action on a page it does not understand.
            acted = [r for r in fake.routes() if r not in ("/session/open", "/session/navigate")]
            expect(acted == [], f"the sequence acted: {acted}")
            # The session stays open, so the agent can drive it with the other routes.
            expect(b.call("POST", "/signin/snapshot", {}) == (200, UNKNOWN_PAGE), "/signin/snapshot")
        b.stop()
        b.expect_clean()


def test_auto_sequence_returns_snapshot_when_the_password_is_on_an_idp_origin() -> None:
    """A password field on the IdP origin ends the sequence with the snapshot, not a 403.

    The signer takes a secret only for an item origin, so the fill is refused with
    origin_mismatch. The sequence cannot continue, and the spec has it return the
    page it is on so the agent can carry on with the other routes.
    """
    page = ax_page(IDP_ORIGIN, ax_node(1, "textbox", "Password"), ax_node(2, "button", "Sign in"))
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b, pages=[page], refuse={"/session/fill": ("origin_mismatch", 403)}) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "E")))
            expect(got == (200, page), f"/signin/start with the password on the IdP origin: {got}")
            expect("/session/fill" in fake.routes(), f"the fill was not tried: {fake.routes()}")
            expect("/session/end" not in fake.routes(), f"the session was ended: {fake.routes()}")
            # The session stays open: the agent drives the rest with the other routes.
            expect(b.call("POST", "/signin/snapshot", {}) == (200, page), "/signin/snapshot")
            expect(b.call("POST", "/signin/cancel", {}) == (200, {"cancelled": True}), "/signin/cancel")
        b.stop()
        b.expect_clean()


def test_redirect_off_origin_then_cancel() -> None:
    """A click that lands off the item's and IdP's origins refuses, and cancel still ends the session."""
    pages = [ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Username"), ax_node(2, "button", "Next")),
             ax_error("origin_mismatch", 403)]
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b, pages=pages) as fake:
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(got == (403, {"error": "origin_mismatch"}), f"/signin/start after a redirect: {got}")
            expect("/session/end" not in fake.routes(), f"the session was ended: {fake.routes()}")
            expect(b.call("POST", "/signin/snapshot", {}) == (403, {"error": "origin_mismatch"}),
                   "/signin/snapshot off origin")
            expect(b.call("POST", "/signin/cancel", {}) == (200, {"cancelled": True}), "/signin/cancel")
            expect(fake.session is False, "cancel left the signer session open")
        b.stop()
        b.expect_clean()


def test_busy_and_cancel_from_any_caller() -> None:
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        with fake_signer(b) as fake:
            expect(b.call("POST", "/signin/start", signin_body(handle=a))[0] == 200, "the first sign-in")
            got = b.call("POST", "/signin/start", signin_body(handle=a))
            expect(got == (409, {"error": "busy"}), f"a second sign-in: {got}")
            expect(b.call("POST", "/signin/cancel", {}) == (200, {"cancelled": True}), "/signin/cancel")
            expect(fake.session is False, "the signer session is still open after cancel")
            expect(b.call("POST", "/signin/start", signin_body(handle=a))[0] == 200, "sign-in after cancel")
            expect(b.call("POST", "/signin/cancel", {})[0] == 200, "cancel again")
            # With no session open, the other routes refuse without calling the signer.
            before = len(fake.calls)
            for route, body in (("/signin/snapshot", {}), ("/signin/click", {"ref": 1}),
                                ("/signin/type", {"ref": 1, "text": "x"}),
                                ("/signin/fill", {"ref": 1, "field": "password"}),
                                ("/signin/finish", signin_body())):
                got = b.call("POST", route, body)
                expect(got == (409, {"error": "no_session"}), f"{route} with no session: {got}")
            expect(len(fake.calls) == before, f"a no_session route called the signer: {fake.routes()}")
        b.stop()
        b.expect_clean()


def test_startup_ends_stale_session() -> None:
    """A session left by a crashed turn is ended when the broker starts."""
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        fake = FakeSigner(b.signin_socket, open_session=True)
        fake.start()
        try:
            b.start(unlocked=["personal"])
            b.wait_for(lambda: "/session/end" in fake.routes(), "the broker ended the stale session")
            expect(fake.session is False, "the stale session is still open")
            got = b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(got[0] == 200, f"a sign-in after the stale session was ended: {got}")
        finally:
            fake.stop()
        b.stop()
        b.expect_clean()


def test_health_signer_down() -> None:
    with broker_run(vault=signin_vault()) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        status, health = b.call("GET", "/health")
        expect(status == 200 and health["signer"] == "down", f"/health without a signer: {health}")
        code, out, err = broker_cli(b, "signer")
        expect(code == 0 and out.strip() == "down", f"cli signer with no signer: {code} {out!r} {err}")
        with fake_signer(b):
            expect(b.call("GET", "/health")[1]["signer"] == "ready", "/health with the signer ready")
            code, out, err = broker_cli(b, "signer")
            expect(code == 0 and out.strip() == "ready", f"cli signer: {code} {out!r} {err}")
            b.call("POST", "/signin/start", signin_body(handle=handle("personal", "A")))
            expect(b.call("GET", "/health")[1]["signer"] == "busy", "/health with a session open")
            b.call("POST", "/signin/cancel", {})
        with fake_signer(b, health="sandbox_unavailable"):
            expect(b.call("GET", "/health")[1]["signer"] == "sandbox_unavailable",
                   "/health with no sandbox")
        b.stop()
        b.expect_clean()


def test_socket_serves_no_secrets() -> None:
    with broker_run(vault=stack_vault()) as b:
        # A key the broker does not know is ignored: the account lists its own scope as usual.
        b.add_account("personal", extra={"secrets_item_scope": "folder:Stack"})
        b.start(unlocked=["personal"])
        mode = stat.S_IMODE(b.socket.stat().st_mode)
        expect(mode == 0o600, f"socket mode {oct(mode)}")
        expect(b.sock_call("GET", "/secrets") == (404, {"error": "not_found"}), "socket /secrets")
        expect(b.call("GET", "/secrets") == (404, {"error": "not_found"}), "TCP /secrets")
        expect(sorted(b.handles()) == [handle("personal", "A"), handle("personal", "E")], f"handles {b.handles()}")
        expect(b.sock_call("GET", "/items") == (404, {"error": "not_found"}), "the socket serves no TCP routes")
        b.stop()
        expect(not b.socket.exists(), "the socket is removed on stop")
        b.expect_clean(extra_secrets=[STACK_VALUE])


def unlock_cli(b: Broker, *args, stdin: str = ""):
    import subprocess
    env = {"PATH": b.env["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "BITWARDEN_SYNC_SOCKET": str(b.socket)}
    out = subprocess.run([sys.executable, "-m", "broker.unlock", *args], cwd=BROKER_PKG, env=env,
                         input=stdin, capture_output=True, text=True, timeout=60)
    b.bodies.append(out.stdout + out.stderr)
    return out.returncode, out.stdout + out.stderr


def test_unlock_over_socket() -> None:
    with broker_run() as b:
        b.add_account("manual", unlock="manual")
        b.add_account("personal")
        b.start(unlocked=["personal"])
        expect(b.state("manual") == "locked", "a manual account starts locked")

        code, out = unlock_cli(b, "manual", stdin=WRONG_PASSWORD + "\n")
        expect(code == 2, f"wrong password exit {code}: {out}")
        expect(b.state("manual") == "locked", f"after a wrong password: {b.state('manual')}")
        code, out = unlock_cli(b, "nobody", stdin=MASTER + "\n")
        expect(code == 3, f"unknown slug exit {code}")
        code, out = unlock_cli(b, "personal", stdin=MASTER + "\n")
        expect(code == 3, f"stored account exit {code}")
        code, out = unlock_cli(b, "manual", stdin=MASTER + "\n")
        expect(code == 0, f"unlock exit {code}: {out}")
        expect(b.state("manual") == "unlocked", "unlocked over the socket")
        expect(handle("manual", "A") in b.handles(), "a manual account's items are listed once unlocked")
        code, out = unlock_cli(b, "--lock", "manual")
        expect(code == 0 and b.state("manual") == "locked", f"lock exit {code}, state {b.state('manual')}")
        expect(handle("manual", "A") not in b.handles(), "items vanish after lock")
        code, out = unlock_cli(b, "--lock", "personal")
        expect(code == 3, f"lock of a stored account exit {code}")
        status, _ = b.sock_call("POST", "/unlock", {"slug": "manual"})
        expect(status == 400, f"unlock with no password {status}")

        for entry in b.run.log_lines():
            expect(MASTER not in json.dumps(entry) and WRONG_PASSWORD not in json.dumps(entry), "password in bw argv")
        b.stop()
        code, out = unlock_cli(b, "manual", stdin=MASTER + "\n")
        expect(code == 1, f"broker down exit {code}")
        b.expect_clean()


def broker_cli(b: Broker, *args):
    import subprocess
    env = {"PATH": b.env["PATH"], "PYTHONDONTWRITEBYTECODE": "1", "BITWARDEN_LISTEN": b.env["BITWARDEN_LISTEN"]}
    out = subprocess.run([sys.executable, "-m", "broker.cli", *args], cwd=BROKER_PKG, env=env,
                         capture_output=True, text=True, timeout=60)
    b.bodies.append(out.stdout + out.stderr)
    return out.returncode, out.stdout, out.stderr


def account_row(b: Broker, slug: str) -> dict:
    status, rows = b.call("GET", "/accounts")
    expect(status == 200, f"GET /accounts {status}")
    return next(r for r in rows if r["slug"] == slug)


def test_manual_login_at_start() -> None:
    with broker_run() as b:
        b.add_account("manual", unlock="manual")
        b.start()
        b.wait_for(lambda: account_row(b, "manual")["logged_in"], "manual account signed in")
        row = account_row(b, "manual")
        expect(row["state"] == "locked" and row["error_class"] == "", f"manual account {row}")
        expect(row["retry_after"] is None, f"retry_after {row['retry_after']}")
        b.expect_clean()


def test_manual_login_refused_at_start() -> None:
    with broker_run(vault=vault_data(client_secret="wrong-" + secrets.token_urlsafe(18))) as b:
        b.add_account("manual", unlock="manual")
        b.start()
        b.wait_for(lambda: account_row(b, "manual")["state"] == "error", "manual sign-in refused")
        row = account_row(b, "manual")
        expect(row["error_class"] == "auth" and row["logged_in"] is False, f"refused account {row}")
        b.expect_clean()


def test_accounts_retry_after() -> None:
    with broker_run(vault=vault_data(rate_limit_after=1, retry_after=120)) as b:
        b.add_account("personal")
        b.start()
        b.wait_for(lambda: account_row(b, "personal")["state"] == "unlocked", "personal unlocked")
        expect(account_row(b, "personal")["retry_after"] is None, "retry_after before a 429")
        broker_cli(b, "sync")
        b.wait_for(lambda: account_row(b, "personal")["error_class"] == "rate_limited", "429 recorded")
        left = account_row(b, "personal")["retry_after"]
        expect(isinstance(left, int) and 30 < left <= 120, f"retry_after {left}")
        code, out, err = broker_cli(b, "accounts")
        cols = next(l for l in out.splitlines() if l.startswith("personal")).split("\t")
        expect(len(cols) == 7 and cols[5] == "1" and 30 < int(cols[6]) <= 120, f"accounts columns {cols}")
        b.stop()
    # The expiry is a time comparison on the Bw object, so it is checked in-process.
    with tempfile.TemporaryDirectory() as tmp:
        bw = Run(Path(tmp)).bw(Run(Path(tmp)).account("personal"))
        expect(bw.retry_after() is None, "retry_after without a backoff")
        bw.retry_at = time.monotonic() + 0.2
        expect(bw.retry_after() == 1, f"a sub-second wait rounds up to 1, got {bw.retry_after()}")
        bw.retry_at = time.monotonic() - 1
        expect(bw.retry_after() is None, "retry_after after retry_at passed")


def test_cli_for_vault_sh() -> None:
    with broker_run() as b:
        b.add_account("personal")
        b.add_account("manual", unlock="manual")
        write_instance(b.instances_dir, "broken", {"mode": "cloud"}, b.run.secrets_root, client_id=None)
        write_instance(b.instances_dir, "9bad", {"mode": "cloud"}, b.run.secrets_root)
        b.start(unlocked=["personal"])
        code, out, err = broker_cli(b, "accounts")
        expect(code == 0, f"accounts exit {code}: {err}")
        rows = [line.split("\t") for line in out.splitlines()]
        b.wait_for(lambda: ["manual", "cloud", "folder", "locked", "-", "1", ""] in
                   [l.split("\t") for l in broker_cli(b, "accounts")[1].splitlines()], "manual account signed in")
        code, out, err = broker_cli(b, "accounts")
        rows = [line.split("\t") for line in out.splitlines()]
        expect(["manual", "cloud", "folder", "locked", "-", "1", ""] in rows, f"accounts rows {rows}")
        expect(["personal", "cloud", "folder", "unlocked", "-", "1", ""] in rows, f"accounts rows {rows}")
        expect(["refused", "broken.json: client_id: secrets/broken/client_id is missing; connect the account again",
                "missing_client_id"] in rows, f"refused instance not listed with its cause: {rows}")
        expect(any(r[0] == "refused" and r[1].startswith("9bad.json: ") and r[2] == "bad_slug" for r in rows),
               f"bad slug not listed: {rows}")

        status, _ = b.call("POST", "/requests", {"site": "https://www.example.com/login", "account": "personal",
                                                 "reason": "needs\tto sign in\nnow"})
        expect(status == 200, f"POST /requests {status}")
        code, out, err = broker_cli(b, "requests", "--json")
        expect(code == 0, f"requests --json exit {code}: {err}")
        data = json.loads(out)
        expect([r["site"] for r in data["pending"]] == ["www.example.com"], f"requests --json {data}")
        expect(set(data) == {"pending", "recent"}, f"requests --json keys {sorted(data)}")
        code, out, err = broker_cli(b, "requests")
        expect(code == 0 and "www.example.com" in out and "agent's text" in out.lower(), f"requests text {out!r}")

        code, out, err = broker_cli(b, "sync")
        expect(code == 0 and "personal" in out, f"sync exit {code}: {out}{err}")
        code, out, err = broker_cli(b, "sync", "--json")
        expect(code == 0 and len(out.splitlines()) == 1, f"sync --json exit {code}: {out!r}{err}")
        reply = json.loads(out)
        expect(isinstance(reply, dict) and ("synced" in reply or "reason" in reply), f"sync --json reply {reply}")
        code, out, err = broker_cli(b, "items")
        expect(code == 2, f"an unknown command exit {code}")
        b.stop()
        code, out, err = broker_cli(b, "sync", "--json")
        expect(code == 1 and out == "" and "not reachable" in err, f"sync --json with the broker down: {code} {out!r} {err}")
        code, out, err = broker_cli(b, "accounts")
        expect(code == 1 and "not reachable" in err, f"broker down exit {code}: {err}")
        b.expect_clean()


def test_sync_loop_and_idle_lock() -> None:
    # 0.02 minutes is 1.2 s, so the loop runs a few times within the test.
    with broker_run(sync_minutes="0.02", idle_minutes="0.02") as b:
        b.add_account("personal")
        b.add_account("manual", unlock="manual")
        b.start(unlocked=["personal"])
        b_handle = handle("personal", "B")
        expect(b_handle not in b.handles(), "B starts out of scope")
        data = json.loads(b.run.vault_file.read_text(encoding="utf-8"))
        next(i for i in data["items"] if i["id"] == item_id("B"))["folderId"] = FOLDER_AGENT
        b.run.vault_file.write_text(json.dumps(data), encoding="utf-8")
        b.wait_for(lambda: b_handle in b.handles(), "the sync loop picks up B")

        code, out = unlock_cli(b, "manual", stdin=MASTER + "\n")
        expect(code == 0, f"unlock exit {code}")
        b.wait_for(lambda: b.state("manual") == "locked", "the idle lock locks the manual account")
        expect(b.state("personal") == "unlocked", "the idle lock leaves stored accounts alone")
        b.stop()
        b.expect_clean()


TOTP_JS_DRIVER = r"""
const [fnSource, cases] = JSON.parse(require("fs").readFileSync(0, "utf8"));
class Event { constructor(type) { this.type = type; } }
class InputEvent extends Event {}
globalThis.Event = Event;
globalThis.InputEvent = InputEvent;
globalThis.HTMLInputElement = class {};
globalThis.getComputedStyle = (el) => ({ display: el.hidden ? "none" : "block", visibility: "visible" });
// Only the two selectors the page functions use.
const select = (list) => (sel) => sel === "*" ? list
  : list.filter((e) => e.tagName === "INPUT" && sel === 'input[type="password"]' && e.type === "password");
function build(spec, all) {
  const el = {
    tagName: spec.tag || "INPUT", type: spec.type ?? "text", autocomplete: spec.autocomplete || "",
    name: spec.name || "", id: spec.id || "", maxLength: spec.maxLength || -1, disabled: false,
    readOnly: false, hidden: !!spec.hidden, value: "", events: [], labels: [], form: spec.form ?? null,
    attrs: spec.attrs || {}, children: [], shadowRoot: null,
    getAttribute(n) { return this.attrs[n] ?? null; },
    getClientRects() { return this.hidden ? [] : [1]; },
    getRootNode() { return document; },
    focus() { document.activeElement = this; },
    dispatchEvent(e) { this.events.push(e.type); if (spec.clearOnInput && e.type === "input") this.value = ""; },
  };
  all.push(el);
  if (spec.shadow) {
    const inner = [];
    spec.shadow.forEach((s) => build(s, inner));
    el.shadowRoot = { querySelectorAll: select(inner), activeElement: null };
  }
  return el;
}
const out = [];
for (const c of cases) {
  const top = [];
  c.dom.forEach((s) => build(s, top));
  globalThis.window = { location: { origin: c.origin } };
  globalThis.document = { querySelectorAll: select(top), activeElement: null, getElementById: () => null };
  if (c.focus !== undefined) document.activeElement = top[c.focus];
  const fn = eval("(" + fnSource + ")");
  const result = fn("https://login.example.com", c.value);
  const flat = [];
  (function walk(list) { for (const e of list) { flat.push(e); if (e.shadowRoot) walk(e.shadowRoot.querySelectorAll("*")); } })(top);
  out.push({ result, values: flat.map((e) => e.value), events: flat.map((e) => e.events.join(",")) });
}
process.stdout.write(JSON.stringify(out));
"""


def test_totp_page_write() -> None:
    """The TOTP and password page functions, run in node against a small DOM stand-in."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        print("skip test_totp_page_write (no node)")
        return
    origin = "https://login.example.com"
    cases = [
        # autocomplete=one-time-code wins over a field that only says "code" in its name.
        {"origin": origin, "value": "123456", "dom": [
            {"name": "email", "type": "email"}, {"name": "sms code"}, {"autocomplete": "one-time-code"}]},
        # Six adjacent one-character boxes in one form: one character each.
        {"origin": origin, "value": "123456", "dom": [
            {"name": "username"}, *({"maxLength": 1, "form": "f", "attrs": {"aria-label": "otp digit"}}
                                    for _ in range(6))]},
        # No code field: the focused text input takes it.
        {"origin": origin, "value": "123456", "focus": 1, "dom": [{"type": "search"}, {"name": "x"}]},
        # Nothing to write into.
        {"origin": origin, "value": "123456", "dom": [{"type": "checkbox"}, {"name": "x", "hidden": True}]},
        # The page moved: nothing written.
        {"origin": "https://evil.example.com", "value": "123456", "dom": [{"autocomplete": "one-time-code"}]},
        # Inside a shadow root.
        {"origin": origin, "value": "654321", "dom": [{"tag": "DIV", "type": "", "shadow": [
            {"autocomplete": "one-time-code"}]}]},
    ]
    # A page handler that empties the field after the write: written, but not confirmed.
    cases.append({"origin": origin, "value": "123456", "dom": [{"autocomplete": "one-time-code", "clearOnInput": True}]})

    def run_node(function, case_list):
        proc = subprocess.run([node, "-e", TOTP_JS_DRIVER], input=json.dumps([function, case_list]),
                              capture_output=True, text=True, timeout=60)
        expect(proc.returncode == 0, f"node driver failed:\n{proc.stderr}")
        return json.loads(proc.stdout)

    got = run_node(cdp._TOTP_FUNCTION, cases)
    ok = {"filled": 1, "wrote": 1}
    expect(got[0]["result"] == ok and got[0]["values"] == ["", "", "123456"], f"autocomplete: {got[0]}")
    expect(got[0]["events"][2] == "input,change", f"autocomplete events: {got[0]}")
    expect(got[1]["result"] == ok and got[1]["values"] == ["", *"123456"], f"split boxes: {got[1]}")
    expect(all(e == "input,change" for e in got[1]["events"][1:]), f"split box events: {got[1]}")
    expect(got[2]["result"] == ok and got[2]["values"] == ["", "123456"], f"focused: {got[2]}")
    expect(got[3]["result"] == {"filled": 0} and got[3]["values"] == ["", ""], f"no field: {got[3]}")
    expect(got[4]["result"] == {"refused": "origin_changed"} and got[4]["values"] == [""], f"moved: {got[4]}")
    expect(got[5]["result"] == ok and got[5]["values"] == ["", "654321"], f"shadow: {got[5]}")
    expect(got[6]["result"] == {"filled": 0, "wrote": 1} and got[6]["values"] == [""], f"cleared: {got[6]}")

    # The password function reports a cleared write the same way.
    pw = run_node(cdp._FILL_FUNCTION, [
        {"origin": origin, "value": "pw", "dom": [{"type": "password"}]},
        {"origin": origin, "value": "pw", "dom": [{"type": "password", "clearOnInput": True}]},
        {"origin": origin, "value": "pw", "dom": [{"type": "text"}]},
    ])
    expect(pw[0]["result"] == ok and pw[0]["values"] == ["pw"], f"password: {pw[0]}")
    expect(pw[1]["result"] == {"filled": 0, "wrote": 1}, f"password cleared: {pw[1]}")
    expect(pw[2]["result"] == {"filled": 0}, f"password no field: {pw[2]}")


def test_api_fill_through_cdp() -> None:
    """/fill end to end: the fake DevTools server gets the value, the caller gets digests only."""
    from broker import digest_redaction
    with broker_run() as b, fake_cdp([{"id": TARGET, "url": LOGIN_URL}]) as fake:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        status, got = b.call("POST", "/fill", fill_body(a, port=fake.port))
        expect(status == 200 and got["filled"] is True and got["origin"] == "https://login.example.com"
               and got["error"] is None, f"/fill password: {status} {got}")
        calls = [m for m in fake.messages if m.get("method") == "Runtime.callFunctionOn"]
        expect([x.get("value") for x in calls[0]["params"]["arguments"]] == ["https://login.example.com",
                                                                              ITEM_PASSWORDS["A"]],
               "the password reached the page as an argument")
        expect(got["redaction"]["base"] == BASE, "the redaction uses the caller's base")
        scrubbed = digest_redaction.scrub(f"page said {ITEM_PASSWORDS['A']}!", [got["redaction"]], "[REDACTED]")
        expect(scrubbed == "page said [REDACTED]!", f"the redaction entry scrubs the value: {scrubbed!r}")

        status, got = b.call("POST", "/fill", fill_body(a, field="totp", port=fake.port))
        expect(status == 200 and got["filled"] is True, f"/fill totp: {status} {got}")
        calls = [m for m in fake.messages if m.get("method") == "Runtime.callFunctionOn"]
        code = calls[-1]["params"]["arguments"][1]["value"]
        expect(isinstance(code, str) and len(code) == 6 and code.isdigit(), "a TOTP code reached the page")
        expect(calls[-1]["params"]["functionDeclaration"] == cdp._TOTP_FUNCTION, "the TOTP write function")
        expect(digest_redaction.scrub(code, [got["redaction"]], "[R]") == "[R]", "the TOTP redaction scrubs the code")

        status, got = b.call("POST", "/fill", fill_body(a, port=fake.port, target="0FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"))
        expect(status == 200 and got == {"filled": False, "origin": "", "error": "target_not_found",
                                         "redaction": None}, f"closed target: {got}")
        b.stop()
        b.expect_clean()


def test_api_fill_write_unconfirmed() -> None:
    """A page that empties the field after the write still gets a redaction entry back."""
    from broker import digest_redaction
    with broker_run() as b, fake_cdp([{"id": TARGET, "url": LOGIN_URL}], page="cleared") as fake:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        status, got = b.call("POST", "/fill", fill_body(handle("personal", "A"), port=fake.port))
        expect(status == 200 and got["filled"] is False and got["error"] == "write_unconfirmed",
               f"/fill on a cleared field: {status} {got}")
        expect(got["redaction"] is not None
               and digest_redaction.scrub(ITEM_PASSWORDS["A"], [got["redaction"]], "[R]") == "[R]",
               "write_unconfirmed carries a redaction entry that scrubs the value")
        b.stop()
        b.expect_clean()


def test_auto_sequence_two_step() -> None:
    """A username page, a password page, a TOTP page, then the account page: one handover."""
    pages = [ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Username"), ax_node(2, "button", "Next")),
             ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "Password"), ax_node(2, "button", "Sign in")),
             ax_page(SIGNIN_ORIGIN, ax_node(1, "textbox", "One-time code"), ax_node(2, "button", "Verify")),
             ax_page(SIGNIN_ORIGIN, ax_node(1, "StaticText", "", "Signed in as user-a"))]
    export = {"origin": SIGNIN_ORIGIN,
              "cookies": [{"name": "sess", "value": "s-1", "domain": "login.example.com", "path": "/",
                           "httpOnly": True, "secure": True, "sameSite": "Lax", "expires": -1},
                          # Not on an item origin: the handover drops it even though the signer sent it.
                          {"name": "idp", "value": "i-1", "domain": "sso.example.com", "path": "/"}],
              "localStorage": [["token", "t-1"]], "sessionStorage": [["tab", "x-1"]]}
    with broker_run(vault=signin_vault()) as b, fake_cdp([{"id": TARGET, "url": LOGIN_URL}]) as cdp_fake:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b, pages=pages, export=export) as fake:
            got = b.call("POST", "/signin/start", {"handle": handle("personal", "A"), "target_id": TARGET,
                                                   "port": cdp_fake.port})
            expect(got == (200, {"done": True, "origin": SIGNIN_ORIGIN, "cookies": 1}),
                   f"the automatic sequence: {got}")
            expect(fake.routes() == ["/session/open", "/session/type", "/session/click", "/session/fill",
                                     "/session/click", "/session/fill", "/session/click", "/session/export",
                                     "/session/end"], f"signer calls {fake.routes()}")
            expect(fake.bodies("/session/type")[0]["text"] == "user-a@example.com", "the username was typed")
            fills = fake.bodies("/session/fill")
            expect([f["field"] for f in fills] == ["password", "totp"], f"fills {[f['field'] for f in fills]}")
            expect(fills[0]["value"] == ITEM_PASSWORDS["A"], "the item's password reached the signer")
            values = [f["value"] for f in fills]
        # The handover: cookies, then both storages as arguments, then a reload.
        methods = cdp_fake.methods()
        expect("Network.setCookies" in methods and methods[-1] == "Target.detachFromTarget",
               f"handover commands {methods}")
        cookies = [m for m in cdp_fake.messages if m.get("method") == "Network.setCookies"][0]
        expect([c["name"] for c in cookies["params"]["cookies"]] == ["sess"],
               f"the IdP cookie reached the tab: {cookies['params']}")
        expect("expires" not in cookies["params"]["cookies"][0], "a session cookie keeps no expiry")
        call = [m for m in cdp_fake.messages if m.get("method") == "Runtime.callFunctionOn"][0]
        args = [a.get("value") for a in call["params"]["arguments"]]
        expect(args == [SIGNIN_ORIGIN, [["token", "t-1"]], [["tab", "x-1"]]], f"storage arguments {args}")
        expect(methods.index("Page.reload") > methods.index("Runtime.callFunctionOn"), "reload after the write")
        expect_no_canaries(b.bodies, values, "the automatic sequence")
        b.stop()
        b.expect_clean(extra_secrets=[v for v in values if v != ITEM_PASSWORDS["A"]])


def test_finish_refused_keeps_session() -> None:
    """The agent's tab is off the item's origin: handover_refused, and the session stays open."""
    with broker_run(vault=signin_vault()) as b, \
            fake_cdp([{"id": TARGET, "url": "https://evil.example.com/landing"}]) as cdp_fake:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        with fake_signer(b) as fake:
            start = b.call("POST", "/signin/start", {"handle": handle("personal", "A"), "target_id": TARGET,
                                                     "port": cdp_fake.port})
            expect(start == (200, UNKNOWN_PAGE), f"/signin/start: {start}")
            got = b.call("POST", "/signin/finish", {"target_id": TARGET, "port": cdp_fake.port})
            expect(got == (409, {"error": "handover_refused"}), f"/signin/finish off origin: {got}")
            expect(cdp_fake.connections == 0, "the broker opened a CDP connection to a refused tab")
            expect("/session/end" not in fake.routes(), f"the session was ended: {fake.routes()}")
            expect(b.call("POST", "/signin/snapshot", {}) == (200, UNKNOWN_PAGE),
                   "the session is gone after a refused handover")
            expect(b.call("POST", "/signin/cancel", {}) == (200, {"cancelled": True}), "/signin/cancel")
        b.stop()
        b.expect_clean()


def run_api_cdp_tests() -> None:
    """The /fill tests that need the fake DevTools server, so websockets. Run inside the Hermes image."""
    test_api_fill_through_cdp()
    test_api_fill_write_unconfirmed()
    test_auto_sequence_two_step()
    test_finish_refused_keeps_session()


CDP_DRIVER = r"""
import runpy, sys
sys.dont_write_bytecode = True
ns = runpy.run_path("/work/scripts/test-bitwarden-broker.py", run_name="cdp_driver")
ns["run_cdp_tests"]()
ns["run_api_cdp_tests"]()
print("image ok")
"""


# Chromium's namespace sandbox refuses to start as root and needs the clone and unshare
# cases the service's seccomp profile allows (scripts/test-bitwarden-signer.py says the same).
HANDOVER_DOCKER_ARGS = ("--network", "none", "--user", "10001:10001", "-e", "HOME=/tmp",
                        "--security-opt",
                        f"seccomp={ROOT / 'compose' / 'bitwarden-signin' / 'seccomp.json'}")

# cdp.handover against the image's Chromium, standing in for the agent's browser:
# the spike's two flows (a cookie session and a token in localStorage) and a tab
# that is not on an allowed origin.
HANDOVER_DRIVER = r'''
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/work/compose/bitwarden-broker")
sys.path.insert(0, "/signin-src")
from broker import cdp
from signer import browser


def fail(msg):
    sys.stdout.flush()
    print(f"FAIL {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def expect(cond, msg):
    if not cond:
        fail(msg)


APP = """<html><body><p id=s>%s</p></body></html>"""
TOKEN = """<html><body><p id=s>reading</p><script>
var t = localStorage.getItem("token");
document.getElementById("s").textContent = t ? "token " + t + " tab " + sessionStorage.getItem("tab")
                                             : "no token";
</script></body></html>"""


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        signed = "sess=s-1" in (self.headers.get("Cookie") or "")
        if path == "/token":
            body = TOKEN
        else:
            body = APP % ("signed in" if signed else "login required")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


PORTS = []
for _ in range(2):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    PORTS.append(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()

A = "http://127.0.0.1:%d" % PORTS[0]
B = "http://127.0.0.1:%d" % PORTS[1]

chrome = browser.Chromium.start()
try:
    with open(os.path.join(chrome.profile, "DevToolsActivePort"), encoding="ascii") as fh:
        port = int(fh.read().split()[0])

    def call(method, params=None):
        chrome.deadline = time.monotonic() + 30
        try:
            return chrome.page(method, params)
        finally:
            chrome.deadline = None

    def targets():
        chrome.deadline = time.monotonic() + 30
        try:
            return chrome.call("Target.getTargets")["targetInfos"]
        finally:
            chrome.deadline = None

    target_id = next(t["targetId"] for t in targets() if t.get("type") == "page")

    def text():
        got = call("Runtime.evaluate", {"expression": "document.body.innerText", "returnByValue": True})
        return (got.get("result") or {}).get("value") or ""

    def go(url):
        call("Page.navigate", {"url": url})
        for _ in range(100):
            if text().strip():
                return
            time.sleep(0.1)
        fail("the page did not load: " + url)

    def wait_for(want, msg):
        for _ in range(100):
            if want in text():
                return
            time.sleep(0.1)
        fail(msg + ": the page says " + repr(text()))

    # Flow 1: a session cookie.
    go(A + "/app")
    expect("login required" in text(), "the test site starts signed out: " + repr(text()))
    cookie = {"name": "sess", "value": "s-1", "domain": "127.0.0.1", "path": "/", "httpOnly": True,
              "secure": False, "sameSite": "Lax", "expires": -1}
    result = cdp.handover(port, target_id, (A,), [cookie], [], [])
    expect(result == cdp.HandoverResult(True, A, None, 1), "cookie handover: %r" % (result,))
    wait_for("signed in", "the cookie handover did not sign the tab in")
    hidden = call("Runtime.evaluate", {"expression": "document.cookie", "returnByValue": True})
    expect("sess" not in ((hidden.get("result") or {}).get("value") or ""), "the HttpOnly cookie is page-readable")

    # A cookie outside the item's origins is dropped before the write.
    foreign = dict(cookie, name="other", value="o-1", domain="other.example.com")
    result = cdp.handover(port, target_id, (A,), [foreign], [], [])
    expect(result == cdp.HandoverResult(True, A, None, 0), "foreign cookie handover: %r" % (result,))
    got = call("Runtime.evaluate", {"expression": "document.cookie", "returnByValue": True})
    expect("other" not in ((got.get("result") or {}).get("value") or ""), "a foreign cookie was written")

    # Flow 2: a token in localStorage and a sessionStorage entry.
    go(A + "/token")
    expect("no token" in text(), "the token page starts empty: " + repr(text()))
    result = cdp.handover(port, target_id, (A,), [], [["token", "t-1"]], [["tab", "x-1"]])
    expect(result == cdp.HandoverResult(True, A, None, 0), "storage handover: %r" % (result,))
    wait_for("token t-1 tab x-1", "the storage handover did not reach the tab")

    # A tab on another origin: refused, and nothing is written.
    go(B + "/token")
    result = cdp.handover(port, target_id, (A,), [cookie], [["token", "t-2"]], [["tab", "x-2"]])
    expect(result == cdp.HandoverResult(False, B, "handover_refused", 0),
           "refused handover: %r" % (result,))
    time.sleep(0.5)
    expect("no token" in text(), "a refused handover wrote storage: " + repr(text()))
    got = call("Runtime.evaluate", {"expression": "localStorage.length + ':' + document.cookie",
                                    "returnByValue": True})
    expect((got.get("result") or {}).get("value") == "0:", "a refused handover left data: %r" % (got,))
finally:
    chrome.stop()
print("image ok")
'''


def test_tcp_request_checks() -> None:
    """Host must name the loopback address and port, no Origin, JSON on POST."""
    with broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        expect(b.call("GET", "/health", headers={"Host": f"localhost:{b.port}"})[0] == 200, "Host localhost:<port>")
        for host in ("evil.example.com", f"evil.example.com:{b.port}", f"127.0.0.1:{b.port + 1}", "127.0.0.1"):
            got = b.call("GET", "/items", headers={"Host": host})
            expect(got == (403, {"error": "bad_host"}), f"Host {host!r}: {got}")
        got = b.call("GET", "/items", headers={"Origin": "https://evil.example.com"})
        expect(got == (403, {"error": "origin_refused"}), f"a request with Origin: {got}")
        got = b.call("POST", "/requests", {"account": "personal", "site": "a.example.com"},
                     headers={"Origin": "null"})
        expect(got == (403, {"error": "origin_refused"}), f"a POST with Origin gets its answer: {got}")
        for ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
            got = b.call("POST", "/requests", {"account": "personal", "site": "a.example.com"},
                         headers={"Content-Type": ctype})
            expect(got == (415, {"error": "unsupported_media_type"}), f"POST with Content-Type {ctype!r}: {got}")
        got = b.call("POST", "/requests", {"account": "personal", "site": "a.example.com"},
                     headers={"Content-Type": "application/json; charset=utf-8"})
        expect(got[0] == 200, f"JSON with a charset parameter: {got}")
        status, got = b.call("GET", "/requests")
        expect([r["site"] for r in got["pending"]] == ["a.example.com"], f"refused POSTs stored nothing: {got}")
        # The Hermes backend and the fill overlay send urllib requests built this way
        # (bitwarden_broker_backend.call, _bwbroker_call); broker.cli is covered below.
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for base in (f"http://127.0.0.1:{b.port}", f"http://localhost:{b.port}"):
            req = urllib.request.Request(base + "/requests", data=json.dumps(
                {"account": "personal", "site": "b.example.com"}).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=10) as resp:
                expect(resp.status == 200 and "request_id" in json.loads(resp.read()), f"urllib POST via {base}")
            with opener.open(urllib.request.Request(base + "/accounts", method="GET"), timeout=10) as resp:
                expect(resp.status == 200, f"urllib GET via {base}")
        b.expect_clean()


def test_sync_coalesced() -> None:
    """One POST /sync at a time, and none within SYNC_MIN_INTERVAL of the last."""
    import threading
    with broker_run() as b:
        b.env["BITWARDEN_SYNC_MIN_SECONDS"] = "30"
        b.add_account("personal")
        b.start(unlocked=["personal"])
        slow = vault_data(slow_sync=2)
        b.run.vault_file.write_text(json.dumps(slow), encoding="utf-8")
        first: list = []
        worker = threading.Thread(target=lambda: first.append(b.call("POST", "/sync", {})))
        worker.start()
        time.sleep(0.5)
        expect(b.call("POST", "/sync", {}) == (200, {"synced": False, "reason": "busy"}),
               "a second /sync while one runs is refused as busy")
        worker.join(30)
        expect(first == [(200, {"synced": ["personal"]})], f"the first /sync: {first}")
        started = time.monotonic()
        got = b.call("POST", "/sync", {})
        expect(got == (200, {"synced": ["personal"], "reason": "recent"}), f"a /sync within 30 s: {got}")
        expect(time.monotonic() - started < 1.5, "a /sync within 30 s runs no bw sync")
        b.expect_clean()


def _sync_calls(b) -> int:
    return sum(1 for line in b.run.log_lines() if line["argv"][:1] == ["sync"])


def test_items_syncs_a_stale_listing() -> None:
    """The agent picks what to sign in to from /items, so a listing older than the floor syncs first."""
    with broker_run() as b:
        b.env["BITWARDEN_ITEMS_SYNC_SECONDS"] = "0"  # every listing counts as stale
        b.add_account("personal")
        b.start(unlocked=["personal"])
        before = _sync_calls(b)
        status, body = b.call("GET", "/items")
        expect(status == 200 and isinstance(body, list), f"/items answered {status}: {body!r}")
        expect(_sync_calls(b) == before + 1, f"a stale listing synced once: {before} -> {_sync_calls(b)}")
        b.expect_clean()


def test_items_does_not_sync_a_fresh_listing() -> None:
    """A sync costs seconds, so a listing within the floor serves the vault it has."""
    with broker_run() as b:
        b.env["BITWARDEN_ITEMS_SYNC_SECONDS"] = "600"
        b.add_account("personal")
        b.start(unlocked=["personal"])
        b.call("GET", "/items")
        before = _sync_calls(b)
        b.call("GET", "/items")
        b.call("GET", "/items")
        expect(_sync_calls(b) == before, f"a fresh listing ran bw sync: {before} -> {_sync_calls(b)}")
        b.expect_clean()


def test_in_page_fill_syncs_once_before_refusing() -> None:
    """In-page fill reads the same local vault, so it gets the same one sync and the same honest code."""
    hidden = login_item("A", FOLDER_AGENT, password=None, uris=[("https://a.example.com", None)])
    with broker_run(vault=vault_data(items=[hidden])) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        before = _sync_calls(b)
        status, got = b.call("POST", "/fill", fill_body(handle("personal", "A")))
        expect(_sync_calls(b) == before + 1, f"one sync before refusing: {before} -> {_sync_calls(b)}")
        expect((status, got) == (422, {"error": "no_password"}), f"/fill answered {status} {got}")
        b.expect_clean()


def main() -> None:
    test_instance_store()
    test_normalize_origin()
    test_single_use_uri()
    test_login_candidates_order_and_cap()
    test_bw_invocation()
    test_scope_filtering()
    test_mixed_uris()
    test_no_uri_items_hidden()
    test_fill_syncs_once_when_the_password_is_not_readable()
    test_fill_refuses_a_password_a_sync_does_not_reveal()
    test_fill_does_not_sync_without_a_hook()
    test_429_backoff()
    test_errors_carry_no_text()
    test_exception_context_holds_no_secrets()
    test_real_bw_stderr()
    test_node_warning_noise()
    test_absent_is_its_own_class()
    test_absent_leaves_the_account_alone()
    test_manual_unlock_and_lock()
    test_denylist()
    test_fill_rejects_bad_arguments()
    test_fill_cdp_down()
    test_fill_bounded_by_timeout()
    test_totp_page_write()
    test_sanitize_keeps_only_signin_fields()
    test_api_returns_no_values()
    test_items_sign_in_only_flag()
    test_fill_refuses_sign_in_only()
    test_signin_routes_keep_tcp_checks()
    test_no_route_returns_canary()
    test_sequence_opens_the_item_uri()
    test_open_walks_to_the_first_page_with_a_login_form()
    test_open_stops_at_a_username_only_page()
    test_open_carries_on_past_page_error_and_origin_mismatch()
    test_open_stops_the_ladder_on_timeout()
    test_open_caps_navigations()
    test_open_returns_the_last_snapshot_when_nothing_matches()
    test_open_raises_the_real_error_when_the_browser_never_started()
    test_open_does_not_return_a_snapshot_of_a_page_the_signer_left()
    test_handover_keeps_the_signers_cookie_rule()
    test_fill_uses_item_origins_only()
    test_submit_prefers_the_form_button_over_the_navigation()
    test_auto_sequence_clicks_the_form_button_not_the_navigation()
    test_auto_sequence_returns_snapshot_on_unknown_page()
    test_auto_sequence_returns_snapshot_when_the_password_is_on_an_idp_origin()
    test_redirect_off_origin_then_cancel()
    test_busy_and_cancel_from_any_caller()
    test_startup_ends_stale_session()
    test_health_signer_down()
    test_socket_serves_no_secrets()
    test_unlock_over_socket()
    test_manual_login_at_start()
    test_manual_login_refused_at_start()
    test_accounts_retry_after()
    test_cli_for_vault_sh()
    test_sync_loop_and_idle_lock()
    test_tcp_request_checks()
    test_sync_coalesced()
    test_in_page_fill_syncs_once_before_refusing()
    test_items_syncs_a_stale_listing()
    test_items_does_not_sync_a_fresh_listing()
    mounts = [
        (ROOT / "scripts", "/work/scripts"),
        (ROOT / "compose" / "bitwarden-broker", "/work/compose/bitwarden-broker"),
    ]
    # BITWARDEN_TEST_IMAGE=assistant-bitwarden-broker:local runs the fill tests on
    # the broker image's own Python and websockets instead of the Hermes image's.
    broker_image = os.environ.get("BITWARDEN_TEST_IMAGE", "")
    if broker_image:
        # The broker image has no Chromium, so the handover driver stays on the Hermes image.
        hermes_image.run_driver(CDP_DRIVER, mounts=mounts, image=broker_image, python="python3")
    else:
        tag = hermes_image.image_gate()
        if tag is not None:
            hermes_image.run_driver(CDP_DRIVER, tag=tag, mounts=mounts)
            hermes_image.run_driver(HANDOVER_DRIVER, tag=tag,
                                    mounts=mounts + [(ROOT / "compose" / "bitwarden-signin", "/signin-src")],
                                    docker_args=HANDOVER_DOCKER_ARGS)
    print("ok")


if __name__ == "__main__":
    main()
