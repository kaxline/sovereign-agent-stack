#!/usr/bin/env python3
"""Checks for the Bitwarden broker's accounts, bw wrapper and item listing.

Stdlib only. Runs scripts/fixtures/fake-bw.py as the bw executable against a
synthetic vault (example.com sites, generated passwords). Every test searches
the captured log records, exception text, fake-bw log and files the broker
writes for the run's synthetic secrets.
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

from broker import accounts, bwcli, origins, vault  # noqa: E402

FAKE_BW = ROOT / "scripts" / "fixtures" / "fake-bw.py"

MASTER = secrets.token_urlsafe(18)
CLIENT_SECRET = secrets.token_urlsafe(24)
WRONG_PASSWORD = "wrong-" + secrets.token_urlsafe(18)
ITEM_PASSWORDS = {name: secrets.token_urlsafe(16) for name in "ABCDEFG"}
TOTP_SEED = base64.b32encode(secrets.token_bytes(10)).decode("ascii")
SECRETS = [MASTER, CLIENT_SECRET, WRONG_PASSWORD, TOTP_SEED, *ITEM_PASSWORDS.values()]

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
        self.vault_file.write_text(json.dumps(vault_data(**vault_extra)), encoding="utf-8")
        os.environ["FAKE_BW_VAULT"] = str(self.vault_file)
        os.environ["FAKE_BW_LOG"] = str(self.log_file)
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
        return bwcli.Bw(account, self.data_root, self.secrets_root, bw_path=str(FAKE_BW))

    def log_lines(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def record(self, exc: BaseException) -> None:
        self.errors.append(str(exc))
        self.errors.append(repr(exc))
        self.errors.append("".join(traceback.format_exception(exc)))

    def expect_clean(self, extra_text: str = "", extra_secrets=()) -> None:
        texts = [extra_text, *self.errors]
        if self.log_file.exists():
            texts.append(self.log_file.read_text(encoding="utf-8"))
        for path in self.tmp.rglob("*"):
            # The fake's own state and vault stand in for Bitwarden's servers, and
            # secrets/ holds the inputs.
            if path.is_file() and path.name not in ("vault.json", "fake-bw-state.json") and "secrets" not in path.parts:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        blob = "\n".join(texts)
        for value in (*SECRETS, *extra_secrets):
            expect(value not in blob, "a synthetic secret appeared in captured output")


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
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        acct = run.account("personal")
        bw = run.bw(acct)
        expect(bw.state == "locked" and bw.error_class == "", "starts locked")
        bw.login()
        bw.unlock()
        expect(bw.state == "unlocked", f"state {bw.state}")
        bw.sync()
        password = bw.get_password(item_id("A"))
        expect(password == ITEM_PASSWORDS["A"], "get_password")
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
        for line in lines:
            expect(line["appdata"] == str(run.data_root / "personal"), "per-account BITWARDENCLI_APPDATA_DIR")
            expect(line["nointeraction"], "BW_NOINTERACTION set")
            if line["argv"][:1] in (["sync"], ["get"], ["list"]):
                expect(line["env"] == ["BW_SESSION"], f"session by env only: {line}")

        session = bw._session
        expect(session and session not in run.log_file.read_text(encoding="utf-8"), "session key never logged")

        # A second instance (broker restart) reuses the data dir: no second config, login is a no-op.
        before = len(lines)
        again = run.bw(acct)
        again.login()
        again.unlock()
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
        got: dict[str, set] = {}
        for meta in v.items():
            got.setdefault(meta.account, set()).add(meta.item_id)
        expect(got.get("folder") == {item_id("A"), item_id("E")}, f"folder scope {got.get('folder')}")
        expect(got.get("coll") == {item_id("C")}, f"collection scope {got.get('coll')}")
        expect(got.get("every") == {item_id(n) for n in "ABCE"}, f"all scope {got.get('every')}")
        expect("nofolder" not in got, "missing folder lists nothing")

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
    for retry_after, want in ((120, 120), (None, 60)):
        with tempfile.TemporaryDirectory() as tmp, captured() as out:
            run = Run(Path(tmp), rate_limit_after=1, retry_after=retry_after)
            bw = run.bw(run.account("personal"))
            bw.login()  # the one allowed server call
            bw.unlock()
            v = vault.Vault({"personal": bw}, vault.Denylist(run.tmp / "denylist.json"))
            first = v.items()
            expect(len(first) == 2, f"listed {len(first)}")
            start = time.monotonic()
            expect_bw_error(run, bw.sync, "rate_limited", "429 on sync")
            expect(want - 5 <= bw.retry_at - start <= want + 1, f"retry_at {bw.retry_at - start:.0f}s, want {want}")
            calls = len(run.log_lines())
            expect_bw_error(run, bw.sync, "rate_limited", "sync during backoff")
            expect(len(run.log_lines()) == calls, "sync during backoff ran bw")
            expect(v.items() == first, "items served from the last list during backoff")
            expect(len(run.log_lines()) == calls, "listing during backoff ran bw")
            expect(bw.state == "unlocked" and bw.error_class == "rate_limited", f"{bw.state}/{bw.error_class}")
            run.expect_clean(out.getvalue())


def test_errors_carry_no_text() -> None:
    with tempfile.TemporaryDirectory() as tmp, captured() as out:
        run = Run(Path(tmp))
        stored = run.bw(run.account("stored", master=WRONG_PASSWORD))
        stored.login()
        expect_bw_error(run, stored.unlock, "auth", "wrong stored password")
        expect(stored.state == "error" and stored.error_class == "auth", f"{stored.state}/{stored.error_class}")

        manual = run.bw(run.account("manual", unlock="manual"))
        manual.login()
        expect_bw_error(run, lambda: manual.unlock(WRONG_PASSWORD), "auth", "wrong manual password")

        expect_bw_error(run, lambda: run.bw(run.account("nologin", unlock="manual")).sync(), "locked",
                        "sync before unlock")
        broken = bwcli.Bw(run.account("broken"), run.data_root, run.secrets_root, bw_path=str(run.tmp / "no-bw"))
        expect_bw_error(run, broken.login, "unknown", "missing bw executable")
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


def main() -> None:
    test_account_file_tolerance()
    test_normalize_origin()
    test_bw_invocation()
    test_scope_filtering()
    test_mixed_uris()
    test_no_uri_items_hidden()
    test_429_backoff()
    test_errors_carry_no_text()
    test_manual_unlock_and_lock()
    test_denylist()
    print("ok")


if __name__ == "__main__":
    main()
