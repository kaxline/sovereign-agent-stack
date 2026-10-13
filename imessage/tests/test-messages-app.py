#!/usr/bin/env python3
"""Checks for the packaged Messages reader app (macOS only).

Builds the app into a temporary folder, checks its identity, signature and
contents, and runs the bundled helper against a synthetic store: one index
run, and the watcher until it raises an event. Installs nothing and loads no
launchd agent.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
LIB = SRC / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import synthetic_messages as sm  # noqa: E402

NAME = "Assistant Messages Reader"
EXE = "assistant-messages-reader"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def run(argv: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=180, **kw)


def scripts(app: Path) -> set[str]:
    base = app / "Contents" / "Resources" / "scripts"
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


def main() -> int:
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        print("SKIP Messages app (needs macOS with swiftc)")
        return 0
    with tempfile.TemporaryDirectory(prefix="msgapp-test-") as tmp:
        tmp = Path(tmp)
        out = tmp / "apps"
        built = run([SRC / "macos" / "build.sh", out])
        expect(built.returncode == 0, f"build failed: {built.stderr}")
        app = out / f"{NAME}.app"

        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        expect(info["CFBundleIdentifier"] == "local.assistant.messages-reader", "bundle id")
        expect(info["CFBundleExecutable"] == EXE and info["LSUIElement"] is True, "Info.plist")
        expect("NSAppleEventsUsageDescription" not in info, "the reader asks for no Apple events")
        expect(run(["codesign", "--verify", "--strict", app]).returncode == 0, "signature")
        ent = run(["codesign", "-d", "--entitlements", "-", "--xml", app]).stdout
        expect("apple-events" not in ent, "no Apple events entitlement")
        flags = run(["codesign", "-d", "--verbose=2", app]).stderr
        expect("runtime" in flags, "hardened runtime")
        launcher = json.loads((app / "Contents" / "Resources" / "launcher.json").read_text())
        expect(Path(launcher["python"]).is_absolute(), "launcher records an absolute Python")
        expect(scripts(app) == {"messages-index.py", "lib/messages_index.py", "lib/messages_watch.py",
                                "lib/typedstream.py"}, f"contents: {scripts(app)}")
        for path in (app / "Contents" / "Resources" / "scripts").rglob("*.py"):
            expect("osascript" not in path.read_text(), f"{path.name}: no scripting code in the reader")

        copy = tmp / "tampered.app"
        shutil.copytree(app, copy, symlinks=True)
        with open(copy / "Contents" / "Resources" / "scripts" / "lib" / "messages_watch.py", "a") as f:
            f.write("\n# changed\n")
        expect(run(["codesign", "--verify", "--strict", copy]).returncode != 0, "tampering must break the seal")

        # The bundled helper indexes a synthetic store; nothing here reaches real messages.
        root = tmp / "synthetic-messages-app" / "store"
        sm.build(root)
        outbox = tmp / "events"
        config = tmp / "messages-index.toml"
        config.write_text(f'store = "{root / "Messages"}"\nindex = "{tmp / "index" / "index.sqlite"}"\n'
                          f'since = 2026-01-01\nchats = ["*"]\n[watch]\npoll_seconds = 30\ndebounce_ms = 100\n'
                          f'[notify]\nchats = ["*"]\noutbox = "{outbox}"\n')
        exe = app / "Contents" / "MacOS" / EXE
        env = os.environ | {"PYTHONPATH": str(tmp / "nowhere"), "MESSAGES_INDEX_CONFIG": "/nonexistent"}
        result = run([exe, "--config", config, "run"], env=env)
        expect(result.returncode == 0, f"bundled run: {result.stderr}")
        expect(json.loads(result.stdout)["added"] > 0, "bundled run indexed the store")
        expect(run([exe, "run"], env=env).returncode == 2, "the launcher passes no environment: no config, exit 2")

        watcher = subprocess.Popen([str(exe), "--config", str(config), "watch"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        try:
            state = Path(f"{tmp / 'index' / 'index.sqlite'}.watch.json")
            deadline = time.monotonic() + 15
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            expect(state.exists(), "the bundled watcher finished its first pass")
            time.sleep(0.3)
            when = datetime.fromtimestamp(time.time() - 1, timezone.utc).isoformat(timespec="seconds")
            sm.append(root, sm.Spec("app-new", "alex", "alex", when, "New from the app test", read=False))
            deadline = time.monotonic() + 5
            while not list(outbox.glob("*.json")) and time.monotonic() < deadline:
                time.sleep(0.1)
            events = list(outbox.glob("*.json"))
            expect(len(events) == 1, "the bundled watcher raised an event")
            expect("New from the app test" not in events[0].read_text(), "the event has no message text")
        finally:
            watcher.send_signal(signal.SIGTERM)
            try:
                code = watcher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                watcher.kill()
                fail("the watcher must stop on SIGTERM")
        expect(code == 0, f"the watcher exits cleanly on SIGTERM, got {code}")
        log = watcher.stdout.read() if watcher.stdout else ""
        expect("New from the app test" not in log, "logs carry counts only")
        expect(not list(app.rglob("__pycache__")), "no bytecode written into the bundle")
        expect(run(["codesign", "--verify", "--strict", app]).returncode == 0, "running leaves the seal intact")

        dry = run([SRC / "macos" / "install.sh", "--config", config, "--dry-run"])
        expect(dry.returncode == 0 and "<string>watch</string>" in dry.stdout and "KeepAlive" in dry.stdout,
               f"installer dry run: {dry.stderr}")
        lint = subprocess.run(["plutil", "-lint", "-"], input=dry.stdout, capture_output=True, text=True)
        expect(lint.returncode == 0, "installer writes a valid plist")
        timed = run([SRC / "macos" / "install.sh", "--config", config, "--interval", "900", "--dry-run"])
        expect("<integer>900</integer>" in timed.stdout and "<string>run</string>" in timed.stdout,
               "interval mode runs the indexer on a timer")
        bad = run([SRC / "macos" / "install.sh", "--config", "relative.toml", "--dry-run"])
        expect(bad.returncode == 2, "installer refuses a relative config path")
    print("ok Messages app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
