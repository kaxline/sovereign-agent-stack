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
    cfg, inst = build(root, {"work": account()})
    # addon_store refuses line breaks in secrets, so a PEM can't come through apply_instance.
    (inst.parent / "secrets" / "work.tls_ca").write_text("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
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
    for host in ("greenmail", "localhost", "127.0.0.1", "host.docker.internal"):
        cfg, inst = build(root, {"work": account(host=host, security="plain")})
        try:
            parsed = tomllib.loads(render(cfg, inst))
            check(f"plain on {host} passes", parsed["accounts"][0]["security"] == "plain")
        except ValueError as exc:
            check(f"plain on {host} passes", False, str(exc))

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    cfg = root / "config"  # no settings.env
    inst = root / "instances"
    inst.mkdir()
    parsed = tomllib.loads(render(cfg, inst))
    check("a missing settings.env and no instances give zero accounts",
          parsed.get("accounts", []) == [] and parsed["bodies"] is False, str(parsed))

    # The entry point: nothing configured, one pass, exit 0.
    run = root / "run"
    (run / "config").mkdir(parents=True)
    (run / "instances").mkdir()
    (run / "secrets").mkdir()
    env = dict(os.environ, EMAIL_ADDON_RUN=str(run), EMAIL_ADDON_TMP=str(root / "tmp.toml"),
               EMAIL_ADDON_DATA=str(root / "data"), ADDON_EMAIL_INTERVAL="1")
    proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py"),
                           "index", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("index with no accounts logs it and exits 0",
          proc.returncode == 0 and "email-indexer: no accounts configured" in proc.stderr + proc.stdout,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    # With an account that nothing listens for, the pass fails with 4 or 5 and the loop still exits 0.
    (run / "config" / "settings.env").write_text("ADDON_EMAIL_SINCE=2026-01-01\n")
    (run / "instances" / "work.json").write_text(json.dumps(
        {"host": "127.0.0.1", "port": 1, "security": "plain", "user": "u@example.com", "mailboxes": ["INBOX"]}))
    (run / "secrets" / "work.password").write_text("synthetic")
    proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "lib" / "email_addon.py"),
                           "index", "--passes", "1"], env=env, capture_output=True, text=True, timeout=60)
    check("a failed pass is logged and does not stop the loop",
          proc.returncode == 0 and "mail-index:" in proc.stderr and "synthetic" not in proc.stdout + proc.stderr,
          f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

print(f"\n{failures} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
