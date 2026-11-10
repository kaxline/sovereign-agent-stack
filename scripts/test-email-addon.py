#!/usr/bin/env python3
"""The email add-on's config renderer and container entry point.

    python3 scripts/test-email-addon.py

Fixtures are synthetic: made-up accounts on example.com and a throwaway
password, written with addon_store so the formats can't drift apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import addon_store as st  # noqa: E402
import email_addon as ea  # noqa: E402
import mail_index as mi  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if ok:
        print(f"ok: {label}")
    else:
        failures += 1
        print(f"FAIL: {label}{': ' + detail if detail else ''}")


MANIFEST = {
    "id": "io.example.test.email",
    "settings": {
        "since": {"type": "date"},
        "bodies": {"type": "bool", "default": False},
        "attachments": {"type": "bool", "default": False},
        "max_body_chars": {"type": "int", "default": 100000},
        "interval": {"type": "int", "default": 900},
    },
    "instances": {"name": "account", "settings": {
        "host": {"type": "string"},
        "port": {"type": "port", "optional": True},
        "security": {"type": "choice", "options": ["tls", "starttls", "plain"], "optional": True},
        "user": {"type": "string"},
        "address": {"type": "string", "optional": True},
        "addresses": {"type": "list", "optional": True},
        "mailboxes": {"type": "list"},
        "drafts_mailbox": {"type": "string", "optional": True},
        "password": {"type": "secret"},
        "tls_ca": {"type": "secret", "optional": True},
    }},
}
PASSWORD = "hunter2-synthetic-pw"
BOXES = ["INBOX", "Saved 'old'"]


def account(host="imap.example.com", **extra):
    return {"host": host, "user": "me@example.com", "mailboxes": BOXES, "password": PASSWORD, **extra}


def build(root: Path, accounts: dict, settings: dict | None = None) -> tuple[Path, Path]:
    if settings is not None:
        st.apply_settings(MANIFEST, root, settings)
    for slug, values in accounts.items():
        st.apply_instance(MANIFEST, root, slug, values)
    d = st.addon_dir(root, MANIFEST)
    return d / "config", d / "instances"


def render(cfg, inst) -> str:
    return ea.render(cfg, inst, index_path="/data/index/index.sqlite", drafts_dir="/data/drafts")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    cfg, inst = build(root, {"work": account(address="me@example.com", port=1993, drafts_mailbox="Drafts"),
                             "home": account(host="mail.example.org", security="starttls",
                                             addresses=["a@example.com", "b@example.com"])},
                      {"since": "2026-01-01", "bodies": True})
    text = render(cfg, inst)
    parsed = tomllib.loads(text)
    check("top level carries source, index, since, bodies",
          parsed["source"] == "imap" and parsed["index"] == "/data/index/index.sqlite"
          and str(parsed["since"]) == "2026-01-01" and parsed["bodies"] is True
          and parsed["attachments"] is False and parsed["max_body_chars"] == 100000, str(parsed))
    accts = parsed["accounts"]
    check("accounts follow slug order", [a["id"] for a in accts] == ["home", "work"], str(accts))
    check("account fields come through",
          accts[1]["host"] == "imap.example.com" and accts[1]["port"] == 1993
          and accts[1]["address"] == "me@example.com" and accts[1]["drafts_mailbox"] == "Drafts"
          and accts[0]["security"] == "starttls" and accts[0]["addresses"] == ["a@example.com", "b@example.com"],
          str(accts))
    check("the password appears nowhere", PASSWORD not in text and "password" not in text)
    check("list values with a quote and a space round-trip",
          accts[0]["mailboxes"] == BOXES and accts[1]["mailboxes"] == BOXES, str(accts[0]["mailboxes"]))
    out = Path(tmp) / "config.toml"
    out.write_text(text)
    try:
        config = mi.load_config(out)
        check("mail_index.load_config accepts the rendered file",
              [a.id for a in config.accounts] == ["home", "work"], str(config.accounts))
    except Exception as exc:  # noqa: BLE001
        check("mail_index.load_config accepts the rendered file", False, repr(exc))
    check("no tls_ca without the secret", all("tls_ca" not in a for a in accts))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    PEM = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
    cfg, inst = build(root, {"work": account(tls_ca=PEM)})
    check("the PEM secret is stored whole", (inst.parent / "secrets" / "work.tls_ca").read_text() == PEM)
    parsed = tomllib.loads(render(cfg, inst))
    check("tls_ca points at the mounted secret",
          parsed["accounts"][0]["tls_ca"] == "/run/addon/secrets/work.tls_ca", str(parsed["accounts"]))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    cfg, inst = build(root, {"work": account(security="plain")})
    try:
        render(cfg, inst)
        check("plain on a public host is refused", False, "no exception")
    except ValueError as exc:
        check("plain on a public host is refused, naming the slug", "work" in str(exc) and PASSWORD not in str(exc),
              str(exc))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    st.apply_settings(MANIFEST, root, {"since": "2026-01-01"})
    for host in ("greenmail", "localhost", "127.0.0.1", "host.docker.internal"):
        cfg, inst = build(root, {"work": account(host=host, security="plain")})
        try:
            text = render(cfg, inst)
            check(f"plain on {host} passes", tomllib.loads(text)["accounts"][0]["security"] == "plain")
            (root / "c.toml").write_text(text)
            mi.load_config(root / "c.toml")
            check(f"plain on {host} passes load_config", True)
        except Exception as exc:  # noqa: BLE001
            check(f"plain on {host} passes", False, repr(exc))
    # imap.example.com with plain: refused by load_config too, not only by render.
    (root / "bad.toml").write_text('source = "imap"\nindex = "/x/i.sqlite"\nsince = 2026-01-01\n'
                                   '[[accounts]]\nid = "work"\nhost = "imap.example.com"\nsecurity = "plain"\n'
                                   'user = "u"\nmailboxes = ["INBOX"]\n')
    try:
        mi.load_config(root / "bad.toml")
        check("load_config still refuses plain on imap.example.com", False, "no exception")
    except mi.ConfigError:
        check("load_config still refuses plain on imap.example.com", True)

HOSTILE = 'a"b\\c\nd]e = 1\n[x]'
HOSTILE_HOST = 'a"b\\c]e=1[x]'  # a host can't hold whitespace; the indexer would refuse it
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    cfg, inst = build(root, {"work": {"host": HOSTILE, "user": HOSTILE, "mailboxes": [HOSTILE, "ok"],
                                      "password": PASSWORD}}, {"since": "2026-01-01"})
    try:
        render(cfg, inst)
        check("a host with whitespace is refused", False, "no exception")
    except ValueError as exc:
        check("a host with whitespace is refused, naming the slug", str(exc).startswith("work: "), str(exc))
    cfg, inst = build(root, {"work": {"host": HOSTILE_HOST, "user": HOSTILE, "mailboxes": [HOSTILE, "ok"],
                                      "password": PASSWORD}})
    try:
        parsed = tomllib.loads(render(cfg, inst))
        acct = parsed["accounts"][0]
        check("hostile values round-trip exactly",
              acct["host"] == HOSTILE_HOST and acct["user"] == HOSTILE and acct["mailboxes"] == [HOSTILE, "ok"],
              str(acct))
        check("hostile values add no keys or tables",
              set(parsed) == {"source", "index", "since", "bodies", "attachments", "max_body_chars", "accounts"}
              and len(parsed["accounts"]) == 1
              and set(acct) == {"id", "host", "user", "mailboxes"}, str(parsed))
    except Exception as exc:  # noqa: BLE001
        check("hostile values parse", False, repr(exc))
    (cfg / "settings.env").write_text("ADDON_EMAIL_SINCE='x\"\\y [evil] bodies = true'\n")
    try:
        parsed = tomllib.loads(render(cfg, inst))
        check("a hostile since comes out quoted", isinstance(parsed["since"], str) and "evil" not in parsed
              and parsed["bodies"] is False, str(parsed))
    except ValueError:
        check("a hostile since is refused", True)
    except Exception as exc:  # noqa: BLE001
        check("a hostile since is quoted or refused", False, repr(exc))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    cfg = root / "config"  # no settings.env
    inst = root / "instances"
    inst.mkdir()
    parsed = tomllib.loads(render(cfg, inst))
    check("a missing settings.env and no instances give zero accounts",
          parsed.get("accounts", []) == [] and parsed["bodies"] is False, str(parsed))
    check("without since, only the mailbox listing gets a placeholder since",
          "since" not in parsed
          and str(tomllib.loads(ea.render(cfg, inst, index_path="/i", drafts_dir="/d", listing=True))["since"])
          == ea.LISTING_SINCE, str(parsed))

    # The entry point: nothing configured, one pass, exit 0.
    run = root / "run"
    (run / "config").mkdir(parents=True)
    (run / "instances").mkdir()
    (run / "secrets").mkdir()
    env = dict(os.environ, EMAIL_ADDON_RUN=str(run), EMAIL_ADDON_TMP=str(root / "tmp.toml"),
               EMAIL_ADDON_DATA=str(root / "data"), )
    proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py"),
                           "index", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("index with no accounts logs it and exits 0",
          proc.returncode == 0 and "email-indexer: no accounts configured" in proc.stderr + proc.stdout,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    # With an account that nothing listens for, the pass fails with 4 or 5 and the loop still exits 0.
    (run / "config" / "settings.env").write_text("ADDON_EMAIL_SINCE=2026-01-01\nADDON_EMAIL_INTERVAL=1\n")
    (run / "instances" / "work.json").write_text(json.dumps(
        {"host": "127.0.0.1", "port": 1, "security": "plain", "user": "u@example.com", "mailboxes": ["INBOX"]}))
    (run / "secrets" / "work.password").write_text("synthetic")
    proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py"),
                           "index", "--passes", "2"], env=env, capture_output=True, text=True, timeout=60)
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    check("a failed pass is logged and the loop goes on to the next pass",
          proc.returncode == 0 and "mail-index:" in proc.stderr and "synthetic" not in proc.stdout + proc.stderr
          and len(lines) == 2 and all(x.get("error") in ("4", "5") for x in lines),
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    # outbox with nothing configured logs, sleeps and never fails.
    (run / "instances" / "work.json").unlink()
    proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py"),
                           "outbox", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("outbox with no accounts logs it and exits 0",
          proc.returncode == 0 and "email-outbox: no accounts configured" in proc.stderr,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

# One account without mailboxes or with a bad setting must not break the others.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    run = root / "run"
    for sub in ("config", "instances", "secrets"):
        (run / sub).mkdir(parents=True)
    (run / "config" / "settings.env").write_text("ADDON_EMAIL_SINCE=2026-01-01\nADDON_EMAIL_INTERVAL=1\n")

    def instance(slug: str, **values) -> None:
        (run / "instances" / f"{slug}.json").write_text(json.dumps(values))
        (run / "secrets" / f"{slug}.password").write_text("synthetic")

    # Nothing listens on 127.0.0.1:1, so a pass fails to connect; the render is what's checked.
    instance("good", host="127.0.0.1", port=1, security="plain", user="u@example.com", mailboxes=["INBOX"])
    instance("nobox", host="127.0.0.1", port=1, security="plain", user="n@example.com")
    instance("public", host="imap.example.com", security="plain", user="p@example.com", mailboxes=["INBOX"])
    cfg, inst = run / "config", run / "instances"

    try:
        parsed = tomllib.loads(ea.render(cfg, inst, index_path="/i", drafts_dir="/d", listing=True, account="nobox"))
        check("listing renders only the requested account, with a placeholder scope",
              [a["id"] for a in parsed["accounts"]] == ["nobox"] and parsed["accounts"][0]["mailboxes"] == ["*"],
              str(parsed))
        (root / "list.toml").write_text(ea.render(cfg, inst, index_path=str(root / "i.sqlite"), drafts_dir="/d",
                                                  listing=True, account="nobox"))
        mi.load_config(root / "list.toml")
        check("load_config accepts the listing config for an account with no mailboxes", True)
    except Exception as exc:  # noqa: BLE001
        check("listing works for an account with no mailboxes", False, repr(exc))

    env = dict(os.environ, EMAIL_ADDON_RUN=str(run), EMAIL_ADDON_TMP=str(root / "tmp.toml"),
               EMAIL_ADDON_DATA=str(root / "data"))
    entry = [sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py")]
    proc = subprocess.run([*entry, "mailboxes", "--account", "nobox"], env=env, capture_output=True, text=True,
                          timeout=60)
    check("mailboxes for an account with no mailboxes gets past the config (fails only on connect)",
          proc.returncode == 5 and "config" not in proc.stderr and "nobox" in proc.stderr,
          f"rc={proc.returncode} err={proc.stderr!r}")

    proc = subprocess.run([*entry, "index", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    rendered = tomllib.loads((root / "tmp.toml").read_text())
    lines = [json.loads(x) for x in proc.stdout.splitlines() if x.startswith("{")]
    check("index renders only the good account", [a["id"] for a in rendered.get("accounts", [])] == ["good"],
          str(rendered))
    check("index logs each skipped account by slug and still runs the pass",
          proc.returncode == 0 and "email-indexer: nobox: " in proc.stderr and "email-indexer: public: " in proc.stderr
          and "email-indexer: good" not in proc.stderr and len(lines) == 1,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    # outbox: the render step in-process (a running watcher wouldn't end on its own).
    import contextlib
    import io
    lay = {"config": cfg, "instances": inst, "secrets": run / "secrets", "out": root / "outbox.toml",
           "index": "/i", "drafts": "/d", "scripts": ROOT / "scripts"}
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        has = ea.render_pass(lay, "email-outbox")
    rendered = tomllib.loads((root / "outbox.toml").read_text())
    check("outbox renders only the good account and logs each skipped slug",
          has and [a["id"] for a in rendered["accounts"]] == ["good"]
          and "email-outbox: nobox: " in err.getvalue() and "email-outbox: public: " in err.getvalue(),
          f"{rendered} {err.getvalue()!r}")

    (inst / "good.json").unlink()
    proc = subprocess.run([*entry, "index", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("index with every account skipped behaves like no accounts configured",
          proc.returncode == 0 and "email-indexer: no accounts configured" in proc.stderr
          and "email-indexer: nobox: " in proc.stderr and not proc.stdout.strip(),
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")
    proc = subprocess.run([*entry, "outbox", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("outbox with every account skipped logs the slugs and no accounts configured",
          proc.returncode == 0 and "email-outbox: no accounts configured" in proc.stderr
          and "email-outbox: public: " in proc.stderr, f"rc={proc.returncode} err={proc.stderr!r}")

print(f"\n{failures} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
