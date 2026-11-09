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
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

PLAIN_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "greenmail"}
SECRETS_MOUNT = "/run/addon/secrets"
INDEX_PATH = "/data/index/index.sqlite"
DRAFTS_DIR = "/data/drafts"
DEFAULT_INTERVAL = 900
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
        lines.append(f"since = {since}" if len(since) == 10 and since[4] == since[7] == "-" and
                     since.replace("-", "").isdigit() else f"since = {_q(since)}")
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
        if data.get("security") == "plain" and (not isinstance(host, str) or host.lower() not in PLAIN_HOSTS):
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


def run_index(lay: dict, passes: int | None) -> int:
    stop = threading.Event()
    child: list[subprocess.Popen] = []

    def on_signal(signum, _frame):
        stop.set()
        for proc in child:
            try:
                proc.send_signal(signum)
            except OSError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    done = 0
    while not stop.is_set():
        try:
            every = interval(lay["config"])
            has_accounts = _render_to_file(lay)
        except ValueError as exc:
            print(f"email-indexer: {exc}", file=sys.stderr, flush=True)
            return 2
        if not has_accounts:
            print("email-indexer: no accounts configured", file=sys.stderr, flush=True)
        else:
            # One pass per call, so the config is rendered again before each.
            proc = subprocess.Popen(_python(lay["scripts"] / "mail-index.py", "--config", str(lay["out"]),
                                            "run", "--every", str(every), "--passes", "1"))
            child.append(proc)
            code = proc.wait()
            child.clear()
            if code == 2:
                return 2
        done += 1
        if passes is not None and done >= passes:
            break
        stop.wait(every)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("index", "mailboxes", "outbox"))
    parser.add_argument("--account", help="mailboxes: the account slug")
    parser.add_argument("--passes", type=int, metavar="N", help="index: stop after N passes")
    args = parser.parse_args(argv)
    if args.command == "mailboxes" and not args.account:
        parser.error("mailboxes needs --account")
    if args.passes is not None and (args.command != "index" or args.passes < 1):
        parser.error("--passes only applies to index, and must be positive")
    lay = _layout()
    os.environ["MAIL_CREDENTIALS_DIR"] = str(lay["secrets"])
    if args.command == "index":
        return run_index(lay, args.passes)
    try:
        _render_to_file(lay)
    except ValueError as exc:
        print(f"email-addon: {exc}", file=sys.stderr)
        return 2
    if args.command == "mailboxes":
        cmd = _python(lay["scripts"] / "mail-index.py", "--config", str(lay["out"]),
                      "mailboxes", "--account", args.account)
    else:
        cmd = _python(lay["scripts"] / "mail-drafts.py", "watch", "--no-mail-app",
                      "--config", str(lay["out"]), "--dir", lay["drafts"])
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    raise SystemExit(main())
