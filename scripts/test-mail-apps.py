#!/usr/bin/env python3
"""Checks for the packaged mail helper apps (macOS only).

Builds both apps into a temporary folder, checks their identity, signature
and contents, and runs the bundled helpers in ways that never reach Mail:
the reader against a synthetic store (source = "store"), the drafts helper
over an empty queue. Installs nothing and loads no launchd agent.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import synthetic_mail as sm  # noqa: E402

READER = "Assistant Mail Reader"
DRAFTS = "Assistant Mail Drafts"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def run(argv: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=120, **kw)


def scripts(app: Path) -> set[str]:
    base = app / "Contents" / "Resources" / "scripts"
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


def main() -> int:
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        print("SKIP mail apps (needs macOS with swiftc)")
        return 0
    with tempfile.TemporaryDirectory(prefix="synthetic-mail-apps-") as tmp:
        tmp = Path(tmp)
        out = tmp / "apps"
        built = run([ROOT / "macos" / "mail-apps" / "build.sh", out])
        expect(built.returncode == 0, f"build failed: {built.stderr}")
        reader, drafts = out / f"{READER}.app", out / f"{DRAFTS}.app"

        ids = set()
        for app, exe in ((reader, "assistant-mail-reader"), (drafts, "assistant-mail-drafts")):
            info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
            ids.add(info["CFBundleIdentifier"])
            expect(info["CFBundleExecutable"] == exe and info["LSUIElement"] is True, f"{app.name} Info.plist")
            expect("never" in info["NSAppleEventsUsageDescription"], f"{app.name} explains what it won't do")
            expect(run(["codesign", "--verify", "--strict", app]).returncode == 0, f"{app.name} signature")
            ent = run(["codesign", "-d", "--entitlements", "-", "--xml", app]).stdout
            expect("com.apple.security.automation.apple-events" in ent, f"{app.name} Apple events entitlement")
            launcher = json.loads((app / "Contents" / "Resources" / "launcher.json").read_text())
            expect(Path(launcher["python"]).is_absolute(), "launcher records an absolute Python")
            expect(launcher["keychain_service"] == "local.assistant.mail-imap",
                   f"launcher names the IMAP Keychain service: {launcher}")
        expect(len(ids) == 2, f"the apps need distinct bundle ids: {ids}")

        shared = {"lib/jxa.py", "lib/mail_imap_conn.py", "lib/mail_credentials.py"}
        expect(scripts(reader) == shared | {"mail-index.py", "lib/mail_index.py", "lib/mail_script.py",
                                            "lib/mail_script.js", "lib/mail_imap.py"},
               f"reader contents: {scripts(reader)}")
        expect(scripts(drafts) == shared | {"mail-drafts.py", "lib/mail_drafts.py", "lib/mail_drafts.js",
                                            "lib/drafts.py", "lib/mail_imap_drafts.py"},
               f"drafts contents: {scripts(drafts)}")

        # Editing a sealed script breaks the signature, so a granted app can't be changed quietly.
        copy = tmp / "tampered.app"
        shutil.copytree(reader, copy, symlinks=True)
        with open(copy / "Contents" / "Resources" / "scripts" / "lib" / "mail_script.js", "a") as f:
            f.write("\n// changed\n")
        expect(run(["codesign", "--verify", "--strict", copy]).returncode != 0, "tampering must break the seal")

        # The bundled reader indexes a synthetic store; nothing here reaches Mail.
        store = tmp / "synthetic-mail-store"
        sm.build(store)
        index = tmp / "index" / "mail.sqlite"
        config = tmp / "mail-index.toml"
        config.write_text(f'source = "store"\nstore = "{store}"\nindex = "{index}"\nsince = 2026-01-01\n'
                          f'[[accounts]]\nid = "{sm.ACCOUNTS["personal"].id}"\nmailboxes = ["INBOX"]\n')
        exe = reader / "Contents" / "MacOS" / "assistant-mail-reader"
        env = os.environ | {"PYTHONPATH": str(tmp / "nowhere"), "MAIL_INDEX_CONFIG": "/nonexistent"}
        result = run([exe, "--config", config, "run"], env=env)
        expect(result.returncode == 0, f"bundled reader run: {result.stderr}")
        stats = json.loads(result.stdout)
        expect(stats["added"] == 7 and stats["in_scope"] == 7, f"bundled reader indexed the store: {stats}")
        expect(run([exe, "run"], env=env).returncode == 2, "the launcher passes no environment: no config, exit 2")
        expect(not list(reader.rglob("__pycache__")), "no bytecode written into the bundle")

        exe = drafts / "Contents" / "MacOS" / "assistant-mail-drafts"
        queue = tmp / "drafts"
        result = run([exe, "--dir", queue, "run"])
        expect(result.returncode == 0, f"bundled drafts run: {result.stderr}")
        expect(json.loads(result.stdout) == {"created": 0, "failed": 0, "rejected": 0, "deferred": 0},
               f"empty queue: {result.stdout}")
        expect(run([exe, "run"]).returncode == 2, "drafts helper needs --dir")
        expect(run(["codesign", "--verify", "--strict", reader]).returncode == 0, "running leaves the seal intact")
        expect(run(["codesign", "--verify", "--strict", drafts]).returncode == 0, "running leaves the seal intact")

        dry = run([ROOT / "macos" / "mail-apps" / "install.sh", "reader", "--config", config, "--dry-run"])
        expect(dry.returncode == 0 and "<integer>900</integer>" in dry.stdout, f"installer dry run: {dry.stderr}")
        lint = subprocess.run(["plutil", "-lint", "-"], input=dry.stdout, capture_output=True, text=True)
        expect(lint.returncode == 0, "installer writes a valid plist")
        bad = run([ROOT / "macos" / "mail-apps" / "install.sh", "drafts", "--dir", "relative", "--dry-run"])
        expect(bad.returncode == 2, "installer refuses a relative drafts folder")
    print("OK mail apps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
