#!/usr/bin/env python3
"""Save queued draft requests as drafts. See docs/email.md.

Usage:
  python3 scripts/mail-drafts.py --dir data/mail-drafts run      # one pass
  python3 scripts/mail-drafts.py --dir data/mail-drafts watch    # keep polling
  python3 scripts/mail-drafts.py --dir data/mail-drafts doctor   # counts only

The folder may also come from MAIL_DRAFTS_DIR. Drafts are saved to Mail's
Drafts and never sent; --open also shows each one in a compose window. Needs
the Automation permission for Mail. With --config, drafts for that config's
source = "imap" accounts are saved to the account's Drafts folder instead;
add --no-mail-app where there's no Mail.app. Prints counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import jxa  # noqa: E402
import mail_credentials  # noqa: E402
import mail_drafts as md  # noqa: E402
import mail_imap_conn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=os.environ.get("MAIL_DRAFTS_DIR"),
                        help="drafts folder shared with the mail-drafts MCP (or set MAIL_DRAFTS_DIR)")
    parser.add_argument("--open", action="store_true", help="also open each draft in a compose window")
    parser.add_argument("--max-per-hour", type=int, default=20, help="drafts created per hour (default 20)")
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for Mail per draft")
    parser.add_argument("--interval", type=int, default=30, help="seconds between passes for watch")
    parser.add_argument("--config", default=os.environ.get("MAIL_INDEX_CONFIG"),
                        help="indexer config whose source = \"imap\" accounts get IMAP drafts"
                             " (or set MAIL_INDEX_CONFIG)")
    parser.add_argument("--no-mail-app", action="store_true",
                        help="don't use Mail.app; only the config's IMAP accounts get drafts")
    parser.add_argument("command", choices=("run", "watch", "doctor"))
    args = parser.parse_args()
    if not args.dir:
        print("mail-drafts: pass --dir or set MAIL_DRAFTS_DIR", file=sys.stderr)
        return 2
    if args.max_per_hour < 1 or args.timeout < 1 or args.interval < 1:
        print("mail-drafts: --max-per-hour, --timeout and --interval must be positive", file=sys.stderr)
        return 2
    imap_accounts = {}
    if args.config:
        try:
            imap_accounts = mail_imap_conn.imap_accounts(Path(args.config))
        except mail_imap_conn.ConfigError as exc:
            print(f"mail-drafts: config: {exc}", file=sys.stderr)
            return 2
    if args.no_mail_app and not imap_accounts:
        print("mail-drafts: --no-mail-app needs a --config with source = \"imap\" accounts", file=sys.stderr)
        return 2
    helper = md.Helper(Path(args.dir), open_windows=args.open, max_per_hour=args.max_per_hour,
                       timeout=args.timeout, imap_accounts=imap_accounts, mail_app=not args.no_mail_app)
    try:
        if args.command == "doctor":
            print(json.dumps(helper.doctor(), indent=2))
        elif args.command == "run":
            print(json.dumps(helper.process().as_dict(), indent=2))
        else:
            while True:
                stats = helper.process()
                if any(stats.as_dict().values()):
                    print(json.dumps(stats.as_dict()), flush=True)
                time.sleep(args.interval)
    except jxa.AutomationDenied:
        print("mail-drafts: macOS didn't allow this program to control Mail. Allow it under"
              " System Settings > Privacy & Security > Automation; see docs/email.md.", file=sys.stderr)
        return 4
    except (mail_imap_conn.AuthFailed, mail_credentials.MissingPassword) as exc:
        print(f"mail-drafts: {exc}", file=sys.stderr)
        return 4
    except (jxa.ScriptError, mail_imap_conn.ImapError) as exc:
        print(f"mail-drafts: {exc}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
