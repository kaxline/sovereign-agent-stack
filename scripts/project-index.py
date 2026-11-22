#!/usr/bin/env python3
"""Generate or check a project's INDEX.md from sources.yaml (or the project tree).

An INDEX.md with .index-manifest.json beside it is generated, and this script
rewrites it. One without a manifest is hand-written: the script leaves it alone
unless given --force, which hands it over to the generator.

Usage:
  python3 scripts/project-index.py --root path/to/project [--force]
  python3 scripts/project-index.py --root path/to/project --check
  python3 scripts/project-index.py --refresh-all path/to/projects [--json]
  python3 scripts/project-index.py --check-all path/to/projects [--json]
  make project-index PROJECT=my-project [FORCE=1]

Exit codes for --root: 0 written or fresh (a hand-written index counts as
fresh for --check), 1 stale or missing, 2 invalid sources or not a directory,
3 hand-written index refused without --force. --refresh-all and --check-all
exit 0 unless the projects directory is missing (2); each project's result is
in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import project_sources as ps  # noqa: E402


def one_project(root: Path, *, check: bool, force: bool) -> int:
    if not root.is_dir():
        print(f"project-index: not a directory: {root}", file=sys.stderr)
        return 2
    kind = ps.index_kind(root)
    try:
        if check:
            if kind == ps.HAND_WRITTEN:
                print(f"project-index: hand-written (not checked) ({root})")
                return 0
            if ps.catalog_is_fresh(root):
                print(f"project-index: fresh ({root})")
                return 0
            print(f"project-index: stale or missing ({root})", file=sys.stderr)
            return 1
        if kind == ps.HAND_WRITTEN and not force:
            print(
                f"project-index: {root / ps.INDEX_NAME} is hand-written (no "
                f"{ps.MANIFEST_NAME}); left as is. To replace it with a generated "
                "index, run with --force (make project-index ... FORCE=1).",
                file=sys.stderr,
            )
            return 3
        warnings = ps.write_index(root)
    except ps.SourcesError as exc:
        print(f"project-index: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"project-index: {warning}", file=sys.stderr)
    print(f"project-index: wrote {root / ps.INDEX_NAME}")
    return 0


def all_projects(projects: Path, *, refresh: bool, as_json: bool) -> int:
    if not projects.is_dir():
        print(f"project-index: not a directory: {projects}", file=sys.stderr)
        return 2
    result = ps.survey_projects(projects, refresh=refresh)
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return 0
    changed = "refreshed" if refresh else "stale"
    for name in result[changed]:
        print(f"project-index: {changed} {name}")
    for item in result["failed"]:
        print(f"project-index: {item['project']}: {item['error']}", file=sys.stderr)
    print(
        f"project-index: {len(result[changed])} {changed}, {len(result['fresh'])} fresh, "
        f"{len(result['hand_written'])} hand-written, {len(result['failed'])} failed"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--root", help="Project directory (contains AGENTS.md / optional sources.yaml)"
    )
    target.add_argument(
        "--refresh-all",
        metavar="DIR",
        help="Rewrite every stale generated index under DIR's project folders",
    )
    target.add_argument(
        "--check-all",
        metavar="DIR",
        help="Report every generated index under DIR's project folders; write nothing",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="With --root: exit 0 if INDEX.md matches the sources; 1 if stale or missing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --root: replace a hand-written INDEX.md with a generated one",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --refresh-all or --check-all: print one JSON object",
    )
    args = parser.parse_args()
    if args.root:
        if args.json:
            parser.error("--json goes with --refresh-all or --check-all")
        root = Path(args.root).expanduser().resolve()
        return one_project(root, check=args.check, force=args.force)
    if args.check or args.force:
        parser.error("--check and --force go with --root")
    refresh = bool(args.refresh_all)
    projects = Path(args.refresh_all or args.check_all).expanduser().resolve()
    return all_projects(projects, refresh=refresh, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
