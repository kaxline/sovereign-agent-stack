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
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect, fail  # noqa: E402

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
        self.accounts_dir = run.tmp / "accounts"
        self.accounts_dir.mkdir(exist_ok=True)
        self.socket = run.tmp / "sync" / "sync.sock"
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
            "BITWARDEN_ACCOUNTS_DIR": str(self.accounts_dir),
            "BITWARDEN_DATA_DIR": str(run.tmp),
            "BITWARDEN_SECRETS_DIR": str(run.secrets_root),
            "BITWARDEN_SYNC_MINUTES": sync_minutes,
            "BITWARDEN_IDLE_LOCK_MINUTES": idle_minutes,
            "BITWARDEN_LISTEN": f"127.0.0.1:{self.port}",
            "BITWARDEN_SYNC_SOCKET": str(self.socket),
            "BITWARDEN_BW": str(wrapper),
            "BITWARDEN_SYNC_MIN_SECONDS": "0",  # test_sync_coalesced sets 30
        }

    def add_account(self, slug, scope="folder:Agent", unlock="stored", secrets_scope=None) -> None:
        self.run.account(slug, scope=scope, unlock=unlock)
        lines = ["BW_MODE=cloud", "BW_SERVER=https://vault.bitwarden.com", f"BW_SCOPE={scope}",
                 f"BW_UNLOCK={unlock}", f"BW_CLIENTID={CLIENT_ID}"]
        if secrets_scope:
            lines.append(f"BW_SECRETS_ITEM_SCOPE={secrets_scope}")
        path = self.accounts_dir / f"{slug}.env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)

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
        b.add_account("personal", secrets_scope="folder:Stack")
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
            "origins": ["https://login.example.com"], "has_totp": True}, "item A metadata")
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
               == (200, {"released": True, "returned": True}), "/release")

        stderr = b.stderr()
        expect(f"POST /fill 200 {a}" in stderr, f"the access log names the handle:\n{stderr}")
        expect("GET /nope" not in stderr and "debug" not in stderr, "the access log repeats an unknown path")
        b.stop()
        b.expect_clean(extra_secrets=[STACK_VALUE])


def test_sync_socket_scope() -> None:
    with broker_run(vault=stack_vault()) as b:
        b.add_account("personal", secrets_scope="folder:Stack")
        b.add_account("other")  # no BW_SECRETS_ITEM_SCOPE: serves no secrets
        b.start(unlocked=["personal", "other"])
        mode = stat.S_IMODE(b.socket.stat().st_mode)
        expect(mode == 0o600, f"socket mode {oct(mode)}")
        expect(b.sock_call("GET", "/secrets") == (200, [{"name": "llm-proxy/openai", "value": STACK_VALUE}]),
               "the socket serves the Stack item only")
        expect(b.call("GET", "/secrets") == (404, {"error": "not_found"}), "TCP /secrets")
        expect(handle("personal", "A") in b.handles(), "the agent scope is unchanged")
        expect(not any("5555" in h for h in b.handles()), "a stack secret is listed to the agent")
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
        (b.accounts_dir / "broken.env").write_text("BW_MODE=cloud\n", encoding="utf-8")
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
        expect(any(r[0] == "refused" and "broken.env" in r[1] for r in rows), f"refused file not listed: {rows}")

        status, _ = b.call("POST", "/requests", {"site": "https://www.example.com/login", "account": "personal",
                                                 "reason": "needs\tto sign in\nnow"})
        expect(status == 200, f"POST /requests {status}")
        code, out, err = broker_cli(b, "requests", "--json")
        expect(code == 0, f"requests --json exit {code}: {err}")
        data = json.loads(out)
        expect([r["site"] for r in data["pending"]] == ["www.example.com"], f"requests --json {data}")
        code, out, err = broker_cli(b, "requests", "--tsv")
        fields = out.rstrip("\n").split("\t")
        expect(code == 0 and len(out.splitlines()) == 1 and len(fields) == 4, f"requests --tsv {out!r}")
        expect(fields[1:3] == ["personal", "www.example.com"], f"requests --tsv fields {fields}")
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


def run_api_cdp_tests() -> None:
    """The /fill tests that need the fake DevTools server, so websockets. Run inside the Hermes image."""
    test_api_fill_through_cdp()
    test_api_fill_write_unconfirmed()


CDP_DRIVER = r"""
import runpy, sys
sys.dont_write_bytecode = True
ns = runpy.run_path("/work/scripts/test-bitwarden-broker.py", run_name="cdp_driver")
ns["run_cdp_tests"]()
ns["run_api_cdp_tests"]()
print("image ok")
"""


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
    test_real_bw_stderr()
    test_node_warning_noise()
    test_manual_unlock_and_lock()
    test_denylist()
    test_fill_rejects_bad_arguments()
    test_fill_cdp_down()
    test_fill_bounded_by_timeout()
    test_totp_page_write()
    test_api_returns_no_values()
    test_sync_socket_scope()
    test_unlock_over_socket()
    test_manual_login_at_start()
    test_manual_login_refused_at_start()
    test_accounts_retry_after()
    test_cli_for_vault_sh()
    test_sync_loop_and_idle_lock()
    test_tcp_request_checks()
    test_sync_coalesced()
    mounts = [
        (ROOT / "scripts", "/work/scripts"),
        (ROOT / "compose" / "bitwarden-broker", "/work/compose/bitwarden-broker"),
    ]
    # BITWARDEN_TEST_IMAGE=assistant-bitwarden-broker:local runs the fill tests on
    # the broker image's own Python and websockets instead of the Hermes image's.
    broker_image = os.environ.get("BITWARDEN_TEST_IMAGE", "")
    if broker_image:
        hermes_image.run_driver(CDP_DRIVER, mounts=mounts, image=broker_image, python="python3")
    else:
        hermes_image.maybe_run(CDP_DRIVER, mounts=mounts)
    print("ok")


if __name__ == "__main__":
    main()
