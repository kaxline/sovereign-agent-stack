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
errs = am.validate(MANIFEST)
check("fixture manifest is valid", errs == [], str(errs))

FIXTURE = json.loads((ROOT / "scripts" / "fixtures" / "addons-cli" / "fixture" / "addon.json").read_text())
with tempfile.TemporaryDirectory() as tmp:
    fx = st.listing(FIXTURE, Path(tmp), {})
    check("listing carries the fixture's data block", fx["data"] == FIXTURE.get("data") and fx["data"].get("paths"),
          str(fx.get("data")))
    check("listing carries backup, empty when the manifest has none", fx["backup"] == {}, str(fx.get("backup")))
    check("listing names the hooks present, sorted", fx["hooks"] == ["init"], str(fx.get("hooks")))
    check("listing carries instances.errors, empty when absent", fx["errors"] == [], str(fx.get("errors")))
    check("listing has no postInstallMessage key when the manifest has none", "postInstallMessage" not in fx,
          str(fx.get("postInstallMessage")))
    msg_fx = copy.deepcopy(FIXTURE)
    msg_fx["postInstallMessage"] = "Open http://localhost:1 now."
    check("listing carries postInstallMessage", st.listing(msg_fx, Path(tmp), {}).get("postInstallMessage")
          == "Open http://localhost:1 now.")
    msg_fx["postInstallMessage"] = "  "
    check("listing omits a blank postInstallMessage", "postInstallMessage" not in st.listing(msg_fx, Path(tmp), {}))
    n8n = json.loads((ROOT / "addons" / "n8n" / "addon.json").read_text())
    check("listing carries n8n's postInstallMessage",
          st.listing(n8n, Path(tmp), {}).get("postInstallMessage") == n8n["postInstallMessage"])
    rich_fx = copy.deepcopy(FIXTURE)
    rich_fx["instances"]["connect"] = {"command": "seed-init"}
    rich_fx["instances"]["disconnect"] = {"command": "seed-init"}
    rich_fx["instances"]["errors"] = ["bad_access"]
    rich_fx["backup"] = {"secrets": ["A_B"]}
    rx = st.listing(rich_fx, Path(tmp), {})
    check("listing includes connect and disconnect among the hooks",
          rx["hooks"] == ["connect", "disconnect", "init"], str(rx["hooks"]))
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

    # replace=True (connect --force): the instance is the new input alone.
    st.apply_instance(MANIFEST, root, "swap", {"host": "a.example.com", "user": "u", "password": "old-pw"})
    st.apply_instance(MANIFEST, root, "swap", {"host": "b.example.com"}, replace=True)
    check("replace drops settings left out",
          json.loads((d / "instances" / "swap.json").read_text()) == {"host": "b.example.com"})
    check("replace deletes a secret left out", not (d / "secrets" / "swap.password").exists())
    st.apply_instance(MANIFEST, root, "swap", {"password": "new-pw"}, replace=True)
    check("replace writes a secret it is given",
          (d / "secrets" / "swap.password").read_text() == "new-pw"
          and json.loads((d / "instances" / "swap.json").read_text()) == {})
    check("replace leaves other instances' secrets", (d / "secrets" / "work.password").read_text() == PASSWORD)
    st.remove_instance(MANIFEST, root, "swap")

    lst = st.listing(MANIFEST, root, {"GPTR_MCP_ENABLED": "1"})
    check("listing masks secret", lst["instances"]["work"]["password"] == {"set": True})
    check("listing top-level secret masked", lst["settings"]["api"] == {"set": True})
    check("listing leaks no password", PASSWORD not in json.dumps(lst) and "tok-123" not in json.dumps(lst))
    check("listing header", lst["id"] == MANIFEST["id"] and lst["shortId"] == "mailer"
          and lst["title"] == MANIFEST["title"] and lst["platform"] == "any" and lst["enabled"] is True
          and lst["permissions"] == MANIFEST["permissions"] and lst["tagline"] == MANIFEST["tagline"])
    check("listing carries description and store",
          lst["description"] == MANIFEST["description"] and lst["store"] == MANIFEST["store"])
    no_store = {k: v for k, v in MANIFEST.items() if k != "store"}
    check("listing store is {} without a store block", st.listing(no_store, root, {})["store"] == {})
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
    HOOKED = copy.deepcopy(MANIFEST)
    HOOKED["instances"]["connect"] = {"command": "seed-init"}
    for label, ch in (("LF", "\n"), ("CR", "\r")):
        raises(f"{label} in an instance secret refused with a connect hook, naming the setting", st.InputError,
               lambda ch=ch: st.apply_instance(HOOKED, root, "multi", {"host": "h", "password": f"top{ch}secret"}),
               setting="password", secret="top")
        raises(f"{label} in an instance secret refused by check_instance with a connect hook", st.InputError,
               lambda ch=ch: st.check_instance(HOOKED, {"host": "h", "mailboxes": ["INBOX"], "password": f"top{ch}secret"}),
               setting="password", secret="top")
    check("a refused multi-line secret writes nothing",
          not (st.addon_dir(root, MANIFEST) / "instances" / "multi.json").exists())
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
    st.apply_instance(MANIFEST, root, "multi", {"host": "h", "password": pem})
    check("without a connect hook a multi-line secret is stored whole",
          (st.addon_dir(root, MANIFEST) / "secrets" / "multi.password").read_text() == pem)
    st.remove_instance(MANIFEST, root, "multi")

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
from unittest import mock  # noqa: E402

import addon_cli  # noqa: E402


def cli_main(argv, boom):
    """Run addon_cli.main with store.listing raising `boom`; (rc, stderr)."""
    real = addon_cli.store.listing

    def fake(*_a, **_k):
        raise boom
    addon_cli.store.listing = fake
    err = io.StringIO()
    # list reads compose config on stdin; an empty one, so an open terminal or
    # pipe on the real stdin can't block the run.
    empty = io.TextIOWrapper(io.BytesIO(b""))
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(sys, "stdin", empty):
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

# _mkdir on a directory another user owns (Docker on Linux creates bind-mount
# sources as root): chmod fails, and the error names the directory and chown.
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "addons" / "twenty" / "secrets"
    real_chmod = os.chmod

    def refuse_chmod(path, mode, *a, **kw):
        if Path(path) == target:
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_chmod(path, mode, *a, **kw)

    os.chmod = refuse_chmod
    try:
        st._mkdir(target)
        got = None
    except PermissionError as e:
        got = str(e)
    finally:
        os.chmod = real_chmod
    check("_mkdir's chmod failure is a PermissionError naming the directory and chown",
          got is not None and str(target) in got and "chown" in got, repr(got))

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

# Per-service figures: memory units, compose config parsing, listing.
check("memory_bytes: '1g' and 1073741824 agree",
      st.memory_bytes("1g") == 1073741824 and st.memory_bytes(1073741824) == 1073741824
      and st.memory_bytes("1073741824") == 1073741824)
check("memory_bytes: other units",
      st.memory_bytes("512m") == 536870912 and st.memory_bytes("2GB") == 2147483648
      and st.memory_bytes("64k") == 65536 and st.memory_bytes("10b") == 10)
check("memory_bytes: junk, booleans and negatives give None",
      all(st.memory_bytes(v) is None for v in (None, True, "lots", "", -5, "1.5g", [])))
cfg = {"services": {
    "a": {"image": "x:1", "deploy": {"resources": {"reservations": {"memory": "100"}, "limits": {"memory": "1g"}}},
          "mem_limit": 5, "mem_reservation": 6},
    "b": {"image": "y:2", "mem_limit": "512m", "mem_reservation": 7},
    "c": {},
}}
fig = st.service_figures(cfg)
check("service_figures prefers deploy.resources over mem_*",
      fig["a"] == {"image": "x:1", "reservation": 100, "limit": 1073741824, "ports": {}}, str(fig.get("a")))
check("service_figures falls back to mem_limit and mem_reservation",
      fig["b"] == {"image": "y:2", "reservation": 7, "limit": 536870912, "ports": {}}, str(fig.get("b")))
check("service_figures gives None for what compose leaves out",
      fig["c"] == {"image": None, "reservation": None, "limit": None, "ports": {}}, str(fig.get("c")))
check("service_figures tolerates a config without services", st.service_figures({}) == {})
ports = st.service_figures({"services": {"p": {"ports": [
    {"target": 8080, "published": "3456", "host_ip": "127.0.0.1"},
    {"target": 8080, "published": "9999"},
    {"target": 9000, "published": "9000-9001"},
    {"target": 22},
    "8443:443"]}}})["p"]["ports"]
check("service_figures keeps the first published binding per container port, no ranges",
      ports == {8080: ("127.0.0.1", 3456)}, str(ports))
WEB = {"id": "io.example.web", "services": {
    "proxy": {"compose": "w-proxy", "port": 8080},
    "gated": {"compose": "w-gated", "port": 80, "when": {"gate": "W_LOCAL"}}},
    "web": {"service": "proxy", "path": "/login-local"}}


def web_fig(name, binding):
    return {"services": {name: {"ports": {binding[0]: binding[1]}}}}


check("web_view is None without a web field", st.web_view({"services": {}}, {}, None) is None)
check("web_view resolves loopback to localhost",
      st.web_view(WEB, {}, web_fig("w-proxy", (8080, ("127.0.0.1", 3456))))
      == {"service": "proxy", "path": "/login-local", "url": "http://localhost:3456/login-local"})
check("web_view uses a non-loopback host_ip as the host",
      st.web_view(WEB, {}, web_fig("w-proxy", (8080, ("10.0.0.5", 80))))["url"] == "http://10.0.0.5:80/login-local")
check("web_view brackets an IPv6 host_ip",
      st.web_view(WEB, {}, web_fig("w-proxy", (8080, ("fd00::1", 80))))["url"] == "http://[fd00::1]:80/login-local")
check("web_view url is null without figures or a published port",
      st.web_view(WEB, {}, None)["url"] is None
      and st.web_view(WEB, {}, web_fig("w-proxy", (9999, ("", 1))))["url"] is None)
gated = {**WEB, "web": {"service": "gated", "path": "/"}}
check("web_view url is null while the service's gate is off",
      st.web_view(gated, {"W_LOCAL": "0"}, web_fig("w-gated", (80, ("", 8081))))["url"] is None)
check("web_view resolves a gated service while its gate is on",
      st.web_view(gated, {"W_LOCAL": "1"}, web_fig("w-gated", (80, ("", 8081))))["url"] == "http://localhost:8081/")
with tempfile.TemporaryDirectory() as tmp:
    bare = st.listing(FIXTURE, Path(tmp), {})
    check("listing without figures lists services with null image and memory",
          bare["services"][0] == {"key": "extra", "compose": "fixture-extra", "oneshot": False, "image": None,
                                  "memory": {"reservation": None, "limit": None}}, str(bare.get("services")))
    check("listing without figures has no downloadSize", all("downloadSize" not in s for s in bare["services"]))
    withf = st.listing(FIXTURE, Path(tmp), {}, {"services": {"fixture-seed": {"image": "x:1", "reservation": 1, "limit": 2}},
                                               "sizes": {"x:1": 99}, "oneshots": {"fixture-seed"}})
    seed = [s for s in withf["services"] if s["key"] == "seed"][0]
    check("listing merges figures and downloadSize by image",
          seed == {"key": "seed", "compose": "fixture-seed", "oneshot": True, "image": "x:1",
                   "memory": {"reservation": 1, "limit": 2}, "downloadSize": 99}, str(seed))
check("image_size sums layers for the platform",
      st.image_size([{"Descriptor": {"platform": {"os": "linux", "architecture": "arm64"}},
                      "OCIManifest": {"layers": [{"size": 3}, {"size": 4}]}},
                     {"Descriptor": {"platform": {"os": "linux", "architecture": "amd64"}},
                      "OCIManifest": {"layers": [{"size": 100}]}}], "linux/arm64") == 7)
check("image_size is None when the platform is missing",
      st.image_size([{"Descriptor": {"platform": {"os": "linux", "architecture": "amd64"}},
                      "OCIManifest": {"layers": [{"size": 1}]}}], "linux/arm64") is None)
check("image_size reads a single manifest object and SchemaV2Manifest",
      st.image_size({"Descriptor": {"platform": {"os": "linux", "architecture": "arm64"}},
                     "SchemaV2Manifest": {"layers": [{"size": 5}]}}, "linux/arm64") == 5)

# --- transient instance secrets and optional instances ---------------------------
TRANS = copy.deepcopy(MANIFEST)
TRANS["instances"]["settings"]["password"] = {"type": "secret", "label": "Password", "transient": True}
TRANS["instances"]["settings"]["mailboxes"]["optional"] = True
TRANS["instances"]["connect"] = {"command": "seed-init"}
TRANS["instances"]["disconnect"] = {"command": "seed-init"}
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    d = st.addon_dir(root, TRANS)
    st.apply_settings(TRANS, root, {"since": "2026-01-02"})
    st.check_instance(TRANS, {"host": "h", "password": "tr4nsient"})
    raises("check_instance still refuses a line break in a transient secret", st.InputError,
           lambda: st.check_instance(TRANS, {"host": "h", "password": "a\nb"}), "password")
    st.apply_instance(TRANS, root, "work", {"host": "h", "password": "tr4nsient"})
    check("apply_instance writes no secret file for a transient setting",
          not (d / "secrets" / "work.password").exists())
    check("apply_instance writes no password key for a transient setting",
          "password" not in json.loads((d / "instances" / "work.json").read_text()))
    check("no file under the add-on dir holds the transient value",
          not any("tr4nsient" in p.read_text(errors="replace") for p in d.rglob("*") if p.is_file()))
    check("missing() never reports a transient setting",
          not any("password" in m for m in st.missing(TRANS, root)), str(st.missing(TRANS, root)))
    lst = st.listing(TRANS, root, {})
    check("listing schema carries transient: true",
          lst["schema"]["instances"]["settings"]["password"].get("transient") is True)
    check("an instance view shows a transient setting neither set nor unset",
          "password" not in lst["instances"]["work"] and lst["instances"]["work"]["host"] == "h",
          str(lst["instances"]["work"]))
    # A transient secret in a non-transient neighbour manifest stays a normal secret.
    st.apply_instance(MANIFEST, root, "plain", {"password": "kept"})
    check("a non-transient secret is still written", (st.addon_dir(root, MANIFEST) / "secrets" / "plain.password").exists())

OPT = copy.deepcopy(MANIFEST)
OPT["instances"]["optional"] = True
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    st.apply_settings(OPT, root, {"since": "2026-01-02"})
    check("optional instances: no instance, no instances: entry", st.missing(OPT, root) == [],
          str(st.missing(OPT, root)))
    st.apply_instance(OPT, root, "work", {"host": "h"})
    check("optional instances: an existing instance still reports its missing settings",
          st.missing(OPT, root) == ["work.mailboxes"], str(st.missing(OPT, root)))
    check("listing carries instances.optional in the schema",
          st.listing(OPT, root, {})["schema"]["instances"].get("optional") is True)
    check("listing leaves optional out without the field",
          "optional" not in st.listing(MANIFEST, root, {})["schema"]["instances"])
with tempfile.TemporaryDirectory() as tmp:
    check("without optional, no instance still reports instances:account",
          st.missing(MANIFEST, Path(tmp)) == ["since", "instances:account"], str(st.missing(MANIFEST, Path(tmp))))

# --- addon_cli: an mcp entry without a name (Twenty's `instances` entry) ----------
NONAME = copy.deepcopy(MANIFEST)
NONAME["mcp"] = [{"name": "named", "when": {"gate": "GPTR_EXTRA_ENABLED"}}, {"instances": {"from": "workspace"}}]
check("_toggle_fields skips an mcp entry without a name",
      addon_cli._plan_fields(NONAME)["mcp"] == ["named@GPTR_EXTRA_ENABLED"],
      str(addon_cli._plan_fields(NONAME)["mcp"]))
NONAME["mcp"] = {"instances": {"from": "workspace"}}
check("_toggle_fields copes with a single nameless mcp object", addon_cli._plan_fields(NONAME)["mcp"] == [])

# --- addon_cli hook-events: which hook lines are relayed ----------------------------
def hook_events(text: str):
    """addon_cli hook-events on TEXT; (relayed objects, stderr lines)."""
    out, err = io.StringIO(), io.StringIO()
    stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "stdin", stdin):
        addon_cli.main(["--addons-dir", str(ROOT / "addons"), "--data-root", "/nonexistent",
                        "hook-events", "check"])
    return [json.loads(x) for x in out.getvalue().splitlines()], err.getvalue().splitlines()


long_text = "x" * 2000
relayed, logged = hook_events("\n".join([
    json.dumps({"phase": "setup", "step": "fake", "status": "running", "detail": "a\tb\x1b[31mc"}),
    json.dumps({"phase": "pull", "service": "s", "status": long_text, "nested": {"k": "v\x07"}}),
    json.dumps({"phase": "progress", "status": "x"}),
    json.dumps({"phase": "done", "account": "a"}),
    json.dumps({"phase": "error", "error": "timeout"}),
    "plain text",
]) + "\n")
check("hook-events relays only setup and pull lines",
      [r["phase"] for r in relayed] == ["setup", "pull"], repr(relayed))
check("hook-events sets step to the caller's step, so a hook can't print a fake step",
      all(r["step"] == "check" for r in relayed), repr(relayed))
check("hook-events cleans string fields: tab to space, control characters dropped",
      relayed and relayed[0]["detail"] == "a b[31mc", repr(relayed[:1]))
check("hook-events cuts a long string field to 500 characters",
      len(relayed) > 1 and relayed[1]["status"] == "x" * 500, str(len(relayed[1]["status"]) if len(relayed) > 1 else ""))
check("hook-events cleans nested string fields",
      len(relayed) > 1 and relayed[1]["nested"] == {"k": "v"}, repr(relayed[1:2]))
check("hook-events sends other phases, done, error and plain text to the step log",
      len(logged) == 4 and '"progress"' in logged[0] and '"done"' in logged[1]
      and '"error"' in logged[2] and logged[3] == "plain text", repr(logged))

if failures:
    print(f"{failures} failed")
    sys.exit(1)
print("all add-on store checks passed")
