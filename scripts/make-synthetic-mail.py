#!/usr/bin/env python3
"""Write a fake Apple Mail store for developing mail readers. See docs/email.md.

    python3 scripts/make-synthetic-mail.py /tmp/synthetic-mail/store

The output directory must not exist and its path must contain `synthetic-mail`.
Point a reader at it the way it would point at the folder Mail keeps under
~/Library (the directory that holds V10/).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import synthetic_mail  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="new directory to create (path must contain synthetic-mail)")
    args = parser.parse_args()
    try:
        manifest = synthetic_mail.build(args.out)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(manifest['messages'])} synthetic messages to {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
