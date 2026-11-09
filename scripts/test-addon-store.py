#!/usr/bin/env python3
"""Add-on data directory: settings, instances and secrets under data/addons.

    python3 scripts/test-addon-store.py
"""

from __future__ import annotations

import copy
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import addon_manifest as am  # noqa: E402
import addon_store as st  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if ok:
        print(f"ok: {label}")
    else:
        failures += 1
        print(f"FAIL: {label}{': ' + detail if detail else ''}")


def raises(label: str, exc, fn, setting=None, secret=None):
    try:
        fn()
    except exc as e:
        ok = setting is None or e.setting == setting
        if secret is not None and secret in str(e):
            ok = False
        check(label, ok, f"setting={getattr(e, 'setting', None)!r} msg={e}")
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"wrong exception {type(e).__name__}: {e}")
        return
    check(label, False, "no exception")


MANIFEST = json.loads((ROOT / "addons" / "gptr" / "addon.json").read_text())
MANIFEST["id"] = "io.example.test.mailer"
MANIFEST["gates"] = [{"flag": "GPTR_EXTRA_ENABLED", "profile": "research", "seed": False}]
MANIFEST["settings"] = {
    "since": {"type": "date", "label": "Since"},
    "interval": {"type": "int", "label": "Interval", "default": 300},
    "drafts": {"type": "bool", "label": "Drafts", "default": False, "flag": "GPTR_EXTRA_ENABLED"},
    "tags": {"type": "list", "optional": True},
    "mode": {"type": "choice", "options": ["a", "b"], "optional": True},
    "api": {"type": "secret", "optional": True},
    "motto": {"type": "string", "optional": True},
}
MANIFEST["instances"] = {"name": "account", "settings": {
    "host": {"type": "string", "label": "Host", "optional": True},
    "user": {"type": "string", "optional": True},
    "password": {"type": "secret", "optional": True},
    "mailboxes": {"type": "list", "label": "Mailboxes"},
}}
errs = am.validate(MANIFEST, "builtin")
check("fixture manifest is valid", errs == [], str(errs))

FIXTURE = json.loads((ROOT / "scripts" / "fixtures" / "addons-cli" / "fixture" / "addon.json").read_text())
with tempfile.TemporaryDirectory() as tmp:
    fx = st.listing(FIXTURE, Path(tmp), {})
    check("listing carries the fixture's data block", fx["data"] == FIXTURE.get("data") and fx["data"].get("paths"),
          str(fx.get("data")))
    check("listing carries backup, empty when the manifest has none", fx["backup"] == {}, str(fx.get("backup")))
    check("listing names the hooks present, sorted", fx["hooks"] == ["init"], str(fx.get("hooks")))
    check("listing carries instances.errors, empty when absent", fx["errors"] == [], str(fx.get("errors")))
    rich_fx = copy.deepcopy(FIXTURE)
    rich_fx["hooks"]["backup"] = {"script": ["scripts/vault.sh", "backup"]}
    rich_fx["instances"]["connect"] = {"command": "seed-init"}
    rich_fx["instances"]["disconnect"] = {"command": "seed-init"}
    rich_fx["instances"]["errors"] = ["bad_access"]
    rich_fx["backup"] = {"secrets": ["A_B"]}
    rx = st.listing(rich_fx, Path(tmp), {})
    check("listing includes connect and disconnect among the hooks",
          rx["hooks"] == ["backup", "connect", "disconnect", "init"], str(rx["hooks"]))
    check("listing carries instances.errors and backup",
          rx["errors"] == ["bad_access"] and rx["backup"] == {"secrets": ["A_B"]}, str((rx["errors"], rx["backup"])))

# Shell- and env-hostile, but one line: instance secrets refuse CR and LF.
PASSWORD = "p|w= 'x\"\\$(y)"

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    d = st.addon_dir(root, MANIFEST)
    check("addon_dir is data/addons/<short id>", d == root / "addons" / "mailer", str(d))

    out = st.apply_settings(MANIFEST, root, {"interval": 600})
    check("flagless apply returns no flags", out == {}, str(out))
    out = st.apply_settings(MANIFEST, root, {"drafts": True})
    check("bool with flag returns the flag as 1", out == {"GPTR_EXTRA_ENABLED": "1"}, str(out))
    out = st.apply_settings(MANIFEST, root, {"drafts": False})
    check("bool with flag returns 0 when false", out == {"GPTR_EXTRA_ENABLED": "0"}, str(out))
    env = (d / "config" / "settings.env").read_text()
    check("settings.env holds both keys", "ADDON_MAILER_INTERVAL=600" in env and "ADDON_MAILER_DRAFTS=" in env, env)
    st.apply_settings(MANIFEST, root, {"tags": ["a b", "c"], "motto": "it's fine"})
    env = (d / "config" / "settings.env").read_text()
    check("list written as JSON, string quoted", "ADDON_MAILER_TAGS='[\"a b\", \"c\"]'" in env
          and "ADDON_MAILER_INTERVAL=600" in env, env)
    check("directory is 0700", stat.S_IMODE(d.stat().st_mode) == 0o700)
    check("config directory is 0700", stat.S_IMODE((d / "config").stat().st_mode) == 0o700)
    check("no settings.env at the add-on root", not (d / "settings.env").exists())
    check("no flag written to any file", "GPTR_EXTRA_ENABLED" not in env)

    st.apply_settings(MANIFEST, root, {"api": "tok-123"})
    f = d / "secrets" / "api"
    check("top-level secret in secrets/<name> at 0600",
          f.read_text() == "tok-123" and stat.S_IMODE(f.stat().st_mode) == 0o600)
    check("secrets dir 0700", stat.S_IMODE(f.parent.stat().st_mode) == 0o700)
    check("secret not in settings.env", "tok-123" not in (d / "config" / "settings.env").read_text())

    st.apply_instance(MANIFEST, root, "work", {"host": "imap.example.com", "password": PASSWORD})
    st.apply_instance(MANIFEST, root, "work", {"user": "a@example.com"})
    inst = json.loads((d / "instances" / "work.json").read_text())
    check("instance file merged, no secret", inst == {"host": "imap.example.com", "user": "a@example.com"}, str(inst))
    pf = d / "secrets" / "work.password"
    check("instance secret reads back exactly", pf.read_text() == PASSWORD)
    check("instance secret mode 0600", stat.S_IMODE(pf.stat().st_mode) == 0o600)
    hits = [str(p) for p in root.rglob("*") if p.is_file() and PASSWORD.encode() in p.read_bytes()]
    check("password only in its own file", hits == [str(pf)], str(hits))
    check("no temp files left", not [p for p in root.rglob("*") if ".tmp" in p.name])

    lst = st.listing(MANIFEST, root, {"GPTR_MCP_ENABLED": "1"})
    check("listing masks secret", lst["instances"]["work"]["password"] == {"set": True})
    check("listing top-level secret masked", lst["settings"]["api"] == {"set": True})
    check("listing leaks no password", PASSWORD not in json.dumps(lst) and "tok-123" not in json.dumps(lst))
    check("listing header", lst["id"] == MANIFEST["id"] and lst["shortId"] == "mailer"
          and lst["title"] == MANIFEST["title"] and lst["platform"] == "any" and lst["enabled"] is True
          and lst["permissions"] == MANIFEST["permissions"] and lst["tagline"] == MANIFEST["tagline"])
    check("listing enabled false when flag unset", st.listing(MANIFEST, root, {})["enabled"] is False)
    check("listing value and default", lst["settings"]["interval"] == 600 and lst["settings"]["tags"] == ["a b", "c"])
    check("listing bool is bool", lst["settings"]["drafts"] is False)
    check("listing omits unset setting without default", "mode" not in lst["settings"] and "since" not in lst["settings"])
    check("listing instance values", lst["instances"]["work"]["host"] == "imap.example.com")
    want_schema = {"settings": MANIFEST["settings"],
                   "instances": {"name": "account", "settings": MANIFEST["instances"]["settings"]}}
    check("listing carries the settings schema", lst.get("schema") == want_schema, str(lst.get("schema")))
    no_inst = copy.deepcopy(MANIFEST)
    del no_inst["instances"]
    check("schema instances is null without an instances block",
          "instances" in st.listing(no_inst, root, {})["schema"]
          and st.listing(no_inst, root, {})["schema"]["instances"] is None)
    sneaky = copy.deepcopy(MANIFEST)
    sneaky["settings"]["api"]["default"] = "tok-default"
    check("schema never carries a secret's default",
          "tok-default" not in json.dumps(st.listing(sneaky, root, {})["schema"]))
    lst["schema"]["settings"]["since"]["label"] = "changed"
    check("schema is a copy of the manifest", MANIFEST["settings"]["since"]["label"] == "Since")
    d2 = copy.deepcopy(MANIFEST)
    d2["host"] = {"platform": "darwin"}
    check("platform from host", st.listing(d2, root, {})["platform"] == "darwin")

    check("missing lists since then work.mailboxes", st.missing(MANIFEST, root) == ["since", "work.mailboxes"],
          str(st.missing(MANIFEST, root)))
    st.apply_settings(MANIFEST, root, {"since": "2026-01-02"})
    st.apply_instance(MANIFEST, root, "work", {"mailboxes": ["INBOX"]})
    check("missing empty once set", st.missing(MANIFEST, root) == [])

    with tempfile.TemporaryDirectory() as tmp2:
        r2 = Path(tmp2)
        st.apply_settings(MANIFEST, r2, {"since": "2026-01-02"})
        check("one instance is required", st.missing(MANIFEST, r2) == ["instances:account"],
              str(st.missing(MANIFEST, r2)))
    with tempfile.TemporaryDirectory() as tmp3:
        r3 = Path(tmp3)
        st.apply_instance(MANIFEST, r3, "home", {"host": "h"})
        c = st.addon_dir(r3, MANIFEST) / "config"
        check("the store creates config/ with the add-on directory",
              c.is_dir() and stat.S_IMODE(c.stat().st_mode) == 0o700)

    raises("bad slug", st.InputError, lambda: st.apply_instance(MANIFEST, root, "Bad Slug", {}), setting="slug")
    raises("dotty slug", st.InputError, lambda: st.apply_instance(MANIFEST, root, "../x", {}), setting="slug")
    raises("wrong int type", st.InputError, lambda: st.apply_settings(MANIFEST, root, {"interval": "x"}),
           setting="interval", secret="x")
    raises("bool is not an int", st.InputError, lambda: st.apply_settings(MANIFEST, root, {"interval": True}),
           setting="interval")
    raises("unknown name", st.InputError, lambda: st.apply_settings(MANIFEST, root, {"nope": 1}), setting="nope")
    raises("bad choice", st.InputError, lambda: st.apply_settings(MANIFEST, root, {"mode": "zzz"}), setting="mode")
    raises("bad date", st.InputError, lambda: st.apply_settings(MANIFEST, root, {"since": "2026-13-45"}),
           setting="since")
    raises("secret wrong type does not leak", st.InputError,
           lambda: st.apply_instance(MANIFEST, root, "work", {"password": 12345}), setting="password",
           secret="12345")
    raises("instance unknown name", st.InputError,
           lambda: st.apply_instance(MANIFEST, root, "work", {"slug": "x"}), setting="slug")
    check("failed apply wrote nothing", json.loads((d / "instances" / "work.json").read_text())["user"] == "a@example.com")

    bad = copy.deepcopy(MANIFEST)
    bad["id"] = "io.example.test.."
    raises("id ending in .. refused", st.InputError, lambda: st.addon_dir(root, bad))
    bad["id"] = ""
    raises("empty id refused", st.InputError, lambda: st.addon_dir(root, bad))
    bad["id"] = "io.example.a/b"
    raises("slash id refused", st.InputError, lambda: st.addon_dir(root, bad))

    ok31, bad32 = "a" + "b" * 30, "a" + "b" * 31
    st.apply_instance(MANIFEST, root, ok31, {"host": "h"})
    check("31-char slug accepted", ok31 in st.listing(MANIFEST, root, {})["instances"])
    raises("32-char slug refused", st.InputError, lambda: st.apply_instance(MANIFEST, root, bad32, {}), setting="slug")
    raises("slug with trailing newline refused", st.InputError,
           lambda: st.apply_instance(MANIFEST, root, "work\n", {}), setting="slug")
    st.remove_instance(MANIFEST, root, ok31)
    for label, ch in (("LF", "\n"), ("CR", "\r")):
        raises(f"{label} in an instance secret refused, naming the setting", st.InputError,
               lambda ch=ch: st.apply_instance(MANIFEST, root, "multi", {"host": "h", "password": f"top{ch}secret"}),
               setting="password", secret="top")
        raises(f"{label} in an instance secret refused by check_instance", st.InputError,
               lambda ch=ch: st.check_instance(MANIFEST, {"host": "h", "mailboxes": ["INBOX"], "password": f"top{ch}secret"}),
               setting="password", secret="top")
    check("a refused multi-line secret writes nothing",
          not (st.addon_dir(root, MANIFEST) / "instances" / "multi.json").exists())

    for label, ch in (("U+2028", "\u2028"), ("form feed", "\f"), ("U+0085", "\x85"), ("tab", "\t")):
        raises(f"{label} in a setting string refused", st.InputError,
               lambda ch=ch: st.apply_settings(MANIFEST, root, {"motto": f"x{ch}ADDON_MAILER_INTERVAL=7 y"}),
               setting="motto")
    check("interval unchanged after injection attempts", st.listing(MANIFEST, root, {})["settings"]["interval"] == 600)

    snap = sorted(str(p) for p in root.rglob("*"))
    raises("lone surrogate in secret refused", st.InputError,
           lambda: st.apply_instance(MANIFEST, root, "work", {"host": "ok", "password": "\ud800zz"}),
           setting="password", secret="zz")
    raises("lone surrogate in string refused", st.InputError,
           lambda: st.apply_settings(MANIFEST, root, {"motto": "\ud800"}), setting="motto")
    raises("lone surrogate in list refused", st.InputError,
           lambda: st.apply_settings(MANIFEST, root, {"tags": ["\ud800"]}), setting="tags")
    check("surrogate attempts wrote nothing", snap == sorted(str(p) for p in root.rglob("*"))
          and json.loads((d / "instances" / "work.json").read_text())["host"] == "imap.example.com")

    st.remove_instance(MANIFEST, root, "work")
    left = [p.name for p in (d / "secrets").iterdir() if p.name.startswith("work.")]
    check("remove_instance clears work.* secrets", left == [], str(left))
    check("remove_instance deletes instance file", not (d / "instances" / "work.json").exists())
    check("remove_instance keeps top-level secret", (d / "secrets" / "api").exists())
    raises("remove bad slug", st.InputError, lambda: st.remove_instance(MANIFEST, root, "a/b"), setting="slug")

# --- addon_cli.py: unexpected exceptions -----------------------------------------
import contextlib  # noqa: E402
import io  # noqa: E402

import addon_cli  # noqa: E402


def cli_main(argv, boom):
    """Run addon_cli.main with store.listing raising `boom`; (rc, stderr)."""
    real = addon_cli.store.listing

    def fake(*_a, **_k):
        raise boom
    addon_cli.store.listing = fake
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = addon_cli.main(argv)
    finally:
        addon_cli.store.listing = real
    return rc, err.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    argv = ["--addons-dir", str(ROOT / "addons"), "--data-root", tmp, "list"]
    rc, err = cli_main(argv, PermissionError(13, "Permission denied", "/x/addons/gptr/secrets"))
    check("an OSError is one error line with its type, exit 1",
          rc == 1 and err == "error: PermissionError: [Errno 13] Permission denied: '/x/addons/gptr/secrets'\n",
          repr((rc, err)))
    rc, err = cli_main(argv, RuntimeError("value s3cr3t-in-message"))
    check("another exception prints its type only, exit 1",
          rc == 1 and err == "error: RuntimeError\n", repr((rc, err)))

# --- state.json ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    check("state is {} before anything is recorded", st.state(FIXTURE, Path(tmp)) == {})
    st.set_state(FIXTURE, Path(tmp), "init", {"version": "1.0.0", "at": "2026-11-09T00:00:00Z"})
    st.set_state(FIXTURE, Path(tmp), "other", 1)
    got = st.state(FIXTURE, Path(tmp))
    check("set_state then state round-trips and keeps other keys",
          got == {"init": {"version": "1.0.0", "at": "2026-11-09T00:00:00Z"}, "other": 1}, repr(got))
    path = Path(tmp) / "addons" / "fixture" / "state.json"
    check("state.json is 0600", (path.stat().st_mode & 0o777) == 0o600, oct(path.stat().st_mode))
    check("no temp file is left behind",
          sorted(p.name for p in path.parent.iterdir() if p.name.startswith(".")) == [])
    for bad in ("[1]", "{broken"):
        path.write_text(bad)
        try:
            st.state(FIXTURE, Path(tmp))
            raised = False
        except st.StateError:
            raised = True
        check(f"state raises on a corrupt state.json ({bad})", raised)
        try:
            st.set_state(FIXTURE, Path(tmp), "x", 1)
            raised = False
        except st.StateError:
            raised = True
        check("set_state refuses to overwrite a corrupt state.json",
              raised and path.read_text() == bad)
    check("slug is the public slug check", st.slug("work") == "work")

with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "addons" / "fixture"
    st.prepare(FIXTURE, Path(tmp))
    modes = {n: (d / n).stat().st_mode & 0o777 for n in ("config", "instances", "secrets")}
    check("prepare creates config, instances and secrets at 0700",
          modes == {"config": 0o700, "instances": 0o700, "secrets": 0o700}, repr(modes))
    (d / "secrets" / "x").write_text("keep")
    os.chmod(d / "secrets" / "x", 0o640)
    os.chmod(d / "secrets", 0o755)
    st.prepare(FIXTURE, Path(tmp))
    check("a second prepare keeps an existing secret and its mode",
          (d / "secrets" / "x").read_text() == "keep" and (d / "secrets" / "x").stat().st_mode & 0o777 == 0o640)
    check("public stored and slugs read the data directory",
          st.stored(FIXTURE, d) == {} and st.slugs(d) == [])

if failures:
    print(f"{failures} failed")
    sys.exit(1)
print("all add-on store checks passed")
