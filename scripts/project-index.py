#!/usr/bin/env python3
"""Generate or check a project's INDEX.md from sources.yaml (or the project tree).

Usage:
  python3 scripts/project-index.py --root path/to/project
  python3 scripts/project-index.py --root path/to/project --check
  make project-index PROJECT=my-project
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import project_sources as ps  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        required=True,
        help="Project directory (contains AGENTS.md / optional sources.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 if INDEX.md matches the sources; 1 if stale or missing",
    )
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"project-index: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        if args.check:
            if ps.catalog_is_fresh(root):
                print(f"project-index: fresh ({root})")
                return 0
            print(f"project-index: stale or missing ({root})", file=sys.stderr)
            return 1

        warnings = ps.write_index(root)
    except ps.SourcesError as exc:
        print(f"project-index: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"project-index: {warning}", file=sys.stderr)
    print(f"project-index: wrote {root / ps.INDEX_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
