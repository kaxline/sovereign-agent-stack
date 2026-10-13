#!/usr/bin/env python3
"""Write a fake Messages.app store for developing message readers. See imessage/README.md.

    python3 imessage/make-synthetic-messages.py /tmp/synthetic-messages/store

The output directory must not exist and its path must contain
`synthetic-messages`. Point a reader at <out>/Messages the way it would point
at the Messages folder under ~/Library, and at <out>/AddressBook for contacts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import synthetic_messages  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="new directory to create (path must contain synthetic-messages)")
    args = parser.parse_args()
    try:
        manifest = synthetic_messages.build(args.out)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(manifest['messages'])} synthetic messages in {len(manifest['chats'])} chats"
          f" to {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
