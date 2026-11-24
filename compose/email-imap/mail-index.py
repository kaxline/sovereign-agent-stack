#!/usr/bin/env python3
"""Index a scoped slice of your mail. See docs/email.md.

Usage:
  python3 scripts/mail-index.py --config mail-index.toml run
  python3 scripts/mail-index.py --config mail-index.toml doctor
  python3 scripts/mail-index.py --config mail-index.toml mailboxes [--account ID] [--json-errors]
  python3 scripts/mail-index.py --config mail-index.toml run --every SECONDS [--passes N]

The config may also come from MAIL_INDEX_CONFIG. The default source asks
Mail.app through scripting (Automation permission for Mail). With
source = "store" it reads Mail's files instead (Full Disk Access), and the
config must name the store. An account with source = "imap" is read from its
IMAP server, without changing anything there; its password comes from the
Keychain or from the variable `password_env` names, never from the config.
`run` and `doctor` print counts and timings only.
`mailboxes` prints account ids and names and mailbox names (not message
content) so you can write the config; it lists every account, so run it
yourself. With --json-errors, a failed command also prints one JSON object
on stdout, {"error": NAME, "message": TEXT}; ERROR_NAMES lists the names.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import mail_credentials  # noqa: E402
import mail_imap  # noqa: E402
import mail_imap_conn  # noqa: E402
import mail_index as mi  # noqa: E402
import mail_script as ms  # noqa: E402


def list_mailboxes(config: mi.Config) -> list[dict]:
    """Mail.app or store listings cover every account Mail has, so you can find
    their ids; IMAP listings cover the configured IMAP accounts."""
    kinds = {a.source for a in config.accounts}
    out: list[dict] = []
    if "imap" in kinds:
        imap_only = replace(config, accounts=[a for a in config.accounts if a.source == "imap"])
        out += mail_imap.ImapSource(imap_only).mailboxes()
    if "script" in kinds:
        out += [{"account": a["id"], "name": a["name"], "mailbox": b["path"], "messages": b.get("count")}
                for a in ms.ScriptSource(config).mailboxes(counts=True) for b in a["mailboxes"]]
    if "store" in kinds:
        out += _store_mailboxes(config)
    return sorted(out, key=lambda r: (r["account"], r["mailbox"].lower()))


def _store_mailboxes(config: mi.Config) -> list[dict]:
    version_dir = mi.find_version_dir(config.store)
    with tempfile.TemporaryDirectory(prefix="mail-index-") as tmp:
        meta = mi.snapshot_metadata(version_dir, Path(tmp))
        db = sqlite3.connect(meta)
        try:
            mi.probe_columns(db)
            rows = db.execute("SELECT mb.url, COUNT(m.ROWID) FROM mailboxes mb"
                              " LEFT JOIN messages m ON m.mailbox = mb.ROWID GROUP BY mb.url").fetchall()
        finally:
            db.close()
    out = []
    for url, count in rows:
        parsed = mi.parse_mailbox_url(url or "")
        if parsed:
            out.append({"account": parsed[0], "mailbox": parsed[1], "messages": count})
    return sorted(out, key=lambda r: (r["account"], r["mailbox"].lower()))


# The names --json-errors prints, by the exception that failed the command.
# Clients should treat a name they don't know like "failed".
ERROR_NAMES = {
    "bad_config": "the config or an account's settings are invalid (exit 2)",
    "bad_layout": "Mail's store has a layout this version can't read (exit 3)",
    "permission_denied": "macOS refused Automation or Full Disk Access (exit 4)",
    "password_missing": "no password is stored for the account (exit 4)",
    "login_refused": "the server rejected the user name or password (exit 4)",
    "server_unreachable": "the connection or its TLS handshake failed (exit 5)",
    "server_error": "the server refused a command after the login, or Mail failed (exit 5)",
    "failed": "anything else (exit 1)",
}


def _classify(exc: BaseException) -> tuple[str, int, str]:
    """(error name, exit code, the text after `mail-index: `) for a failure.
    The texts name the account and never carry a password."""
    if isinstance(exc, mi.ConfigError):
        return "bad_config", 2, f"config: {exc}"
    if isinstance(exc, mi.LayoutError):
        return "bad_layout", 3, str(exc)
    if isinstance(exc, ms.AutomationDenied):
        return "permission_denied", 4, ("macOS didn't allow this program to control Mail. Allow it under"
                                        " System Settings > Privacy & Security > Automation; see docs/email.md.")
    if isinstance(exc, PermissionError):
        return "permission_denied", 4, ("permission denied reading the store. With source = \"store\", macOS needs"
                                        " Full Disk Access for the program running the indexer; see docs/email.md.")
    if isinstance(exc, mail_credentials.MissingPassword):
        return "password_missing", 4, str(exc)
    if isinstance(exc, mail_imap_conn.AuthFailed):
        return "login_refused", 4, str(exc)
    if isinstance(exc, mail_imap_conn.Unreachable):
        return "server_unreachable", 5, str(exc)
    if isinstance(exc, (ms.ScriptError, mail_imap_conn.ImapError)):
        return "server_error", 5, str(exc)
    # Type name only: the message or a traceback could carry message content.
    return "failed", 1, f"unexpected {type(exc).__name__}"


def guarded(fn, json_errors: bool = False) -> tuple[object, int]:
    """Call fn and map its failures to an exit code, printing the `mail-index:` line
    and, with json_errors, the JSON error object on stdout. Returns (result, 0) on
    success. The one place exceptions become exit codes."""
    try:
        return fn(), 0
    except Exception as exc:  # noqa: BLE001 - the loop must outlive anything a pass throws
        name, code, text = _classify(exc)
        print(f"mail-index: {text}", file=sys.stderr, flush=True)
        if json_errors:
            print(json.dumps({"error": name, "message": text}), flush=True)
        return None, code


def pass_code(result: object) -> int:
    """A run's exit code from its result: 4 when an account's password failed,
    5 when an account's server did (the account ids are under `failed`), else 0."""
    failed = result.get("failed") if isinstance(result, dict) else None
    codes = set(failed.values()) if failed else set()
    return 4 if 4 in codes else (5 if codes else 0)


def one_command(args) -> object:
    config = mi.load_config(Path(args.config))
    if args.command == "run":
        return mi.run(config).as_dict()
    if args.command == "doctor":
        return mi.doctor(config)
    if args.account is None:
        return list_mailboxes(config)
    if args.account not in {a.id for a in config.accounts}:
        raise mi.ConfigError(f"no account {args.account}")
    # Log in to that IMAP account only, so another account's failure isn't
    # reported as this one's. Mail.app and store listings cover every account.
    config = replace(config, accounts=[a for a in config.accounts if a.source != "imap" or a.id == args.account])
    return [r for r in list_mailboxes(config) if r["account"] == args.account]


def run_loop(args) -> int:
    """Repeat `run` every args.every seconds. SIGTERM and SIGINT are noted and acted on
    between passes, so a pass in progress finishes before the process exits."""
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    done = 0
    while not stop.is_set():
        result, code = guarded(lambda: one_command(args))
        if code == 2:
            return 2
        if code == 0 and pass_code(result):
            # Some accounts failed and the rest were indexed: the counts, plus the
            # error a failed pass prints, so readers of `error` still see it.
            result = {**result, "error": str(pass_code(result))}
        print(json.dumps(result if code == 0 else {"error": str(code)}), flush=True)
        done += 1
        if args.passes is not None and done >= args.passes:
            break
        stop.wait(args.every)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("MAIL_INDEX_CONFIG"),
                        help="TOML config (or set MAIL_INDEX_CONFIG)")
    parser.add_argument("command", choices=("run", "doctor", "mailboxes"))
    parser.add_argument("--account", help="mailboxes: list only this account id")
    parser.add_argument("--every", type=float, metavar="SECONDS",
                        help="run: repeat every SECONDS until stopped, one JSON line per pass")
    parser.add_argument("--passes", type=int, metavar="N", help="run --every: stop after N passes")
    parser.add_argument("--json-errors", action="store_true",
                        help="on failure, also print {\"error\": NAME, \"message\": TEXT} on stdout")
    args = parser.parse_args()
    if args.account is not None and args.command != "mailboxes":
        parser.error("--account only applies to mailboxes")
    if args.every is not None and args.command != "run":
        parser.error("--every only applies to run")
    if args.every is not None and args.every <= 0:
        parser.error("--every must be positive")
    if args.passes is not None and (args.every is None or args.passes < 1):
        parser.error("--passes needs --every and a positive count")
    if not args.config:
        print("mail-index: pass --config or set MAIL_INDEX_CONFIG", file=sys.stderr)
        if args.json_errors:
            print(json.dumps({"error": "bad_config", "message": "pass --config or set MAIL_INDEX_CONFIG"}))
        return 2
    if args.json_errors and args.every is not None:
        parser.error("--json-errors doesn't apply to run --every")
    if args.every is not None:
        return run_loop(args)
    result, code = guarded(lambda: one_command(args), json_errors=args.json_errors)
    if code:
        return code
    print(json.dumps(result, indent=2))
    return pass_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
