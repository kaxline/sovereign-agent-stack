#!/usr/bin/env python3
"""Entry point of the email-imap image: renders the indexer config from the
add-on's files, then runs the indexer loop, a mailbox listing or the drafts outbox.

    email_addon.py index                    # render, index, sleep, repeat
    email_addon.py mailboxes --account SLUG
    email_addon.py outbox

Reads settings.env (ADDON_EMAIL_<NAME>, shlex-quoted values) and
instances/<slug>.json under /run/addon, and passwords through
MAIL_CREDENTIALS_DIR. Python standard library only; doesn't import addon_store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SECRETS_MOUNT = "/run/addon/secrets"
INDEX_PATH = "/data/index/index.sqlite"
DRAFTS_DIR = "/data/drafts"
DEFAULT_INTERVAL = 900
_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:  # python -I leaves the script's directory off the path
    sys.path.insert(0, _LIB)

import mail_imap_conn  # noqa: E402

INSTANCE_KEYS = ("host", "port", "security", "user", "address", "addresses", "mailboxes", "drafts_mailbox")


def _setting_env(config_dir: Path) -> dict[str, str]:
    """settings.env as {KEY: value}; a missing file is empty."""
    out: dict[str, str] = {}
    try:
        lines = (Path(config_dir) / "settings.env").read_text(encoding="utf-8").split("\n")
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) == 1 and "=" in parts[0]:
            key, _, val = parts[0].partition("=")
            out[key] = val
    return out


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(f"ADDON_EMAIL_{name}")
    if raw is None or raw == "":
        return default
    if raw in ("1", "true"):
        return True
    if raw in ("0", "false"):
        return False
    raise ValueError(f"ADDON_EMAIL_{name} must be 1 or 0")


def _int(env: dict, name: str, default: int) -> int:
    raw = env.get(f"ADDON_EMAIL_{name}")
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"ADDON_EMAIL_{name} must be a whole number") from None
    if value <= 0:
        raise ValueError(f"ADDON_EMAIL_{name} must be positive")
    return value


def interval(config_dir: Path) -> int:
    return _int(_setting_env(config_dir), "INTERVAL", DEFAULT_INTERVAL)


def _q(text: str) -> str:
    """A TOML basic string."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _value(slug: str, key: str, value) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{slug}: {key} has the wrong type")
    if isinstance(value, int) and key == "port":
        return str(value)
    if isinstance(value, str) and key not in ("port", "addresses", "mailboxes"):
        return _q(value)
    if isinstance(value, list) and key in ("addresses", "mailboxes") and all(isinstance(v, str) for v in value):
        return "[" + ", ".join(_q(v) for v in value) + "]"
    raise ValueError(f"{slug}: {key} has the wrong type")


def render(config_dir: Path, instances_dir: Path, *, index_path: str, drafts_dir: str) -> str:
    """TOML in mail-index.toml format. Never holds a password: the mail
    tools read those from MAIL_CREDENTIALS_DIR. drafts_dir is accepted for
    symmetry with the outbox command and isn't part of the config."""
    env = _setting_env(config_dir)
    lines = ['source = "imap"', f"index = {_q(index_path)}"]
    since = env.get("ADDON_EMAIL_SINCE")
    if since:
        lines.append(f"since = {since}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since, re.ASCII)
                     else f"since = {_q(since)}")
    lines.append(f"bodies = {'true' if _bool(env, 'BODIES', False) else 'false'}")
    lines.append(f"attachments = {'true' if _bool(env, 'ATTACHMENTS', False) else 'false'}")
    lines.append(f"max_body_chars = {_int(env, 'MAX_BODY_CHARS', 100000)}")
    secrets = Path(instances_dir).parent / "secrets"
    for path in sorted(Path(instances_dir).glob("*.json")):
        slug = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(f"{slug}: the instance file can't be read as JSON") from None
        if not isinstance(data, dict):
            raise ValueError(f"{slug}: the instance file is not a JSON object")
        host = data.get("host")
        if data.get("security") == "plain" and (not isinstance(host, str) or not mail_imap_conn.plain_allowed(host)):
            raise ValueError(f"{slug}: security = \"plain\" is only allowed for a server on this machine"
                             " (host.docker.internal, localhost, 127.0.0.1 or greenmail)")
        lines += ["", "[[accounts]]", f"id = {_q(slug)}"]
        for key in INSTANCE_KEYS:
            if key in data and data[key] is not None:
                lines.append(f"{key} = {_value(slug, key, data[key])}")
        if (secrets / f"{slug}.tls_ca").is_file():
            lines.append(f"tls_ca = {_q(f'{SECRETS_MOUNT}/{slug}.tls_ca')}")
    return "\n".join(lines) + "\n"


def _layout() -> dict:
    run = Path(os.environ.get("EMAIL_ADDON_RUN", "/run/addon"))
    data = Path(os.environ.get("EMAIL_ADDON_DATA", "/data"))
    scripts = Path(__file__).resolve().parent.parent
    return {
        "config": run / "config", "instances": run / "instances", "secrets": run / "secrets",
        "out": Path(os.environ.get("EMAIL_ADDON_TMP", "/tmp/config.toml")),
        "index": str(data / "index" / "index.sqlite"), "drafts": str(data / "drafts"),
        "scripts": scripts,
    }


def _render_to_file(lay: dict) -> bool:
    """Write the config; True when it has at least one account."""
    text = render(lay["config"], lay["instances"], index_path=lay["index"], drafts_dir=lay["drafts"])
    tmp = lay["out"].with_name(lay["out"].name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, lay["out"])
    return "[[accounts]]" in text


def _python(script: Path, *args: str) -> list[str]:
    return [sys.executable, "-I", "-B", str(script), *args]


class Supervisor:
    """Signal handling shared by the long-running commands: a stop request is
    remembered and forwarded to the child process."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.child: subprocess.Popen | None = None
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, _frame) -> None:
        self.stop.set()
        if self.child is not None:
            try:
                self.child.send_signal(signum)
            except OSError:
                pass

    def start(self, cmd: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(cmd)
        self.child = proc
        if self.stop.is_set():  # the signal arrived before the child was recorded
            try:
                proc.terminate()
            except OSError:
                pass
        return proc

    def finish(self, proc: subprocess.Popen) -> int:
        code = proc.wait()
        self.child = None
        return code


def run_index(lay: dict, passes: int | None) -> int:
    sup = Supervisor()
    done = 0
    while not sup.stop.is_set():
        try:
            every = interval(lay["config"])
            has_accounts = _render_to_file(lay)
        except (ValueError, OSError) as exc:
            print(f"email-indexer: {exc}", file=sys.stderr, flush=True)
            return 2
        if not has_accounts:
            print("email-indexer: no accounts configured", file=sys.stderr, flush=True)
        else:
            # One pass per call, so the config is rendered again before each.
            code = sup.finish(sup.start(_python(lay["scripts"] / "mail-index.py", "--config", str(lay["out"]),
                                                "run", "--every", str(every), "--passes", "1")))
            if code == 2:
                return 2
        done += 1
        if passes is not None and done >= passes:
            break
        sup.stop.wait(every)
    return 0


def run_outbox(lay: dict, passes: int | None) -> int:
    """Keep mail-drafts watch running while accounts exist. mail-drafts doesn't
    handle SIGTERM itself, so this stays the parent and forwards it. The config is
    rendered every interval; a change restarts the watcher. Never exits nonzero."""
    sup = Supervisor()
    done = 0
    while not sup.stop.is_set():
        try:
            every = interval(lay["config"])
            has_accounts = _render_to_file(lay)
        except (ValueError, OSError) as exc:
            print(f"email-outbox: {exc}", file=sys.stderr, flush=True)
            every, has_accounts = DEFAULT_INTERVAL, None
        if has_accounts is False:
            print("email-outbox: no accounts configured", file=sys.stderr, flush=True)
        elif has_accounts:
            seen = lay["out"].read_text(encoding="utf-8")
            proc = sup.start(_python(lay["scripts"] / "mail-drafts.py", "watch", "--no-mail-app",
                                     "--config", str(lay["out"]), "--dir", lay["drafts"]))
            while proc.poll() is None and not sup.stop.wait(min(every, 5)) and not _changed(lay, seen, every):
                pass
            if proc.poll() is None:
                proc.terminate()
            code = sup.finish(proc)
            if not sup.stop.is_set():
                print(f"email-outbox: the watcher stopped with code {code}; restarting", file=sys.stderr, flush=True)
        done += 1
        if passes is not None and done >= passes:
            break
        sup.stop.wait(every)
    return 0


_last_check = [0.0]


def _changed(lay: dict, seen: str, every: int) -> bool:
    """True when the rendered config differs from `seen`; checked at most once per interval."""
    now = time.monotonic()
    if now - _last_check[0] < every:
        return False
    _last_check[0] = now
    try:
        _render_to_file(lay)
        return lay["out"].read_text(encoding="utf-8") != seen
    except (ValueError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("index", "mailboxes", "outbox"))
    parser.add_argument("--account", help="mailboxes: the account slug")
    parser.add_argument("--passes", type=int, metavar="N", help="index: stop after N passes")
    args = parser.parse_args(argv)
    if args.command == "mailboxes" and not args.account:
        parser.error("mailboxes needs --account")
    if args.passes is not None and (args.command == "mailboxes" or args.passes < 1):
        parser.error("--passes only applies to index and outbox, and must be positive")
    lay = _layout()
    os.environ["MAIL_CREDENTIALS_DIR"] = str(lay["secrets"])
    if args.command == "index":
        return run_index(lay, args.passes)
    if args.command == "outbox":
        return run_outbox(lay, args.passes)
    try:
        _render_to_file(lay)
    except (ValueError, OSError) as exc:
        print(f"email-addon: {exc}", file=sys.stderr)
        return 2
    cmd = _python(lay["scripts"] / "mail-index.py", "--config", str(lay["out"]),
                  "mailboxes", "--account", args.account)
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    raise SystemExit(main())
