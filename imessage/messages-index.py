#!/usr/bin/env python3
"""Index a scoped slice of Messages.app history. See imessage/README.md.

Usage:
  python3 imessage/messages-index.py --config messages-index.toml run
  python3 imessage/messages-index.py --config messages-index.toml doctor
  python3 imessage/messages-index.py --config messages-index.toml chats
  python3 imessage/messages-index.py --config messages-index.toml watch
  python3 imessage/messages-index.py --config messages-index.toml notify-test

The config may also come from MESSAGES_INDEX_CONFIG. There is no default
store: the config names it. `run` and `doctor` print counts only. `chats`
prints chat ids, names and participants so you can write the config; that is
personal data, so run it yourself. `watch` keeps the index current and raises
new-message events as the config's [notify] table says, logging counts only.
`notify-test` sends one test event with no message data.

Exit codes: 2 config, 3 unrecognised database layout, 4 macOS denied access,
5 the test event wasn't delivered.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import messages_index as mi  # noqa: E402

DENIED = ("messages-index: macOS denied access to the Messages folder. The program running the"
          " indexer needs Full Disk Access; see imessage/README.md.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.environ.get("MESSAGES_INDEX_CONFIG"),
                        help="TOML config (or set MESSAGES_INDEX_CONFIG)")
    parser.add_argument("command", choices=("run", "doctor", "chats", "watch", "notify-test"))
    args = parser.parse_args()
    if not args.config:
        print("messages-index: pass --config or set MESSAGES_INDEX_CONFIG", file=sys.stderr)
        return 2
    try:
        if args.command in ("watch", "notify-test"):
            return watch(Path(args.config), args.command)
        config = mi.load_config(Path(args.config))
        if args.command == "run":
            result = mi.run(config).as_dict()
        elif args.command == "doctor":
            result = mi.doctor(config)
        else:
            result = mi.list_chats(config)
    except mi.ConfigError as exc:
        print(f"messages-index: config: {exc}", file=sys.stderr)
        return 2
    except mi.LayoutError as exc:
        print(f"messages-index: {exc}", file=sys.stderr)
        return 3
    except PermissionError:
        print(DENIED, file=sys.stderr)
        return 4
    except sqlite3.OperationalError as exc:
        if "unable to open" in str(exc) or "authorization" in str(exc):
            print(DENIED, file=sys.stderr)
            return 4
        raise
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def watch(path: Path, command: str) -> int:
    import messages_watch as mw

    config, watch_config, notify = mw.load(path)
    if command == "notify-test":
        if notify is None:
            raise mi.ConfigError("add a [notify] table to send a test event")
        event = mw.test_event(notify)
        watcher = mw.Watcher(config, watch_config, notify)
        delivered = watcher._deliver(event)
        print(json.dumps({"delivered": delivered, "url": bool(notify.url), "outbox": bool(notify.outbox)}))
        return 0 if delivered else 5
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        mi.open_store(config).close()  # a missing chat.db is a config error (exit 2)
    except PermissionError:
        # Until Full Disk Access is granted, keep running and retry each minute rather
        # than exit and have launchd restart the agent over and over.
        print(DENIED, file=sys.stderr, flush=True)
    mw.Watcher(config, watch_config, notify, log=lambda line: print(line, flush=True)).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
