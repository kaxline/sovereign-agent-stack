#!/usr/bin/env python3
"""Index a scoped slice of an Apple Mail store. See docs/email.md.

Usage:
  python3 scripts/mail-index.py --config mail-index.toml run
  python3 scripts/mail-index.py --config mail-index.toml doctor
  python3 scripts/mail-index.py --config mail-index.toml mailboxes

The config may also come from MAIL_INDEX_CONFIG. There is no default store:
the config names it. `run` and `doctor` print counts only. `mailboxes` prints
account ids and mailbox names (not message content) so you can write the
config; it lists the whole store, so run it yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import mail_index as mi  # noqa: E402


def list_mailboxes(config: mi.Config) -> list[dict]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("MAIL_INDEX_CONFIG"),
                        help="TOML config (or set MAIL_INDEX_CONFIG)")
    parser.add_argument("command", choices=("run", "doctor", "mailboxes"))
    args = parser.parse_args()
    if not args.config:
        print("mail-index: pass --config or set MAIL_INDEX_CONFIG", file=sys.stderr)
        return 2
    try:
        config = mi.load_config(Path(args.config))
        if args.command == "run":
            result = mi.run(config).as_dict()
        elif args.command == "doctor":
            result = mi.doctor(config)
        else:
            result = list_mailboxes(config)
    except mi.ConfigError as exc:
        print(f"mail-index: config: {exc}", file=sys.stderr)
        return 2
    except mi.LayoutError as exc:
        print(f"mail-index: {exc}", file=sys.stderr)
        return 3
    except PermissionError:
        print("mail-index: permission denied reading the store. macOS needs Full Disk Access"
              " for the program running the indexer; see docs/email.md.", file=sys.stderr)
        return 4
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
