#!/usr/bin/env python3
"""Doctor's project checks, printed as TAB-separated OK/WARN lines.

    project_doctor.py doctor-indexes PROJECTS_ROOT
        One line summing up the projects' indexes, plus a WARN per index
        that can't be built.
    project_doctor.py doctor-visibility PROJECTS_ROOT [CODING_PRIMARY] [CODING_EXTRA] [CONTEXT_EXTRA]
        Whether each extra host root a sources.yaml lists sits under a coding
        or context root. The roots are host paths, colon-separated: for a
        synced coding root, its host folder rather than its container path.

Host stdlib only. Run by scripts/doctor.sh as a script path, so
project_sources.py is imported from beside it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from project_sources import (
    SOURCES_NAME,
    SourcesError,
    extra_host_roots,
    survey_projects,
)


def split_colon_paths(raw: str) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(":"):
        part = part.strip()
        if part:
            out.append(part)
    return out


def path_is_visible(path: str | Path, prefixes: Iterable[str]) -> bool:
    candidate = str(Path(path).expanduser().resolve())
    for prefix in prefixes:
        if not prefix:
            continue
        outer = str(Path(prefix).expanduser().resolve()) if Path(prefix).exists() else prefix.rstrip("/")
        if candidate == outer or candidate.startswith(outer + "/"):
            return True
    return False


def visibility_prefixes(
    *,
    coding_primary: str = "",
    coding_extra: str = "",
    context_extra: str = "",
) -> list[str]:
    prefixes = []
    if coding_primary:
        prefixes.append(coding_primary)
    prefixes.extend(split_colon_paths(coding_extra))
    prefixes.extend(split_colon_paths(context_extra))
    return prefixes


def _doctor_visibility(
    projects_root: Path,
    coding_primary: str,
    coding_extra: str,
    context_extra: str,
) -> int:
    """Print TAB-separated OK/WARN lines for extra source roots."""
    if not projects_root.is_dir():
        return 0
    prefixes = visibility_prefixes(
        coding_primary=coding_primary,
        coding_extra=coding_extra,
        context_extra=context_extra,
    )
    for child in sorted(projects_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / SOURCES_NAME).is_file():
            continue
        try:
            extras = extra_host_roots(child)
        except SourcesError as exc:
            sys.stdout.write(f"WARN\tsources.yaml ({child.name}): {exc}\n")
            continue
        if not extras:
            sys.stdout.write(
                f"OK\tsources.yaml ({child.name}): no extra host roots\n"
            )
            continue
        for extra in extras:
            shown = str(extra)
            if path_is_visible(extra, prefixes):
                sys.stdout.write(f"OK\tcontext visible ({child.name}): {shown}\n")
            else:
                sys.stdout.write(
                    f"WARN\tsource {child.name} lists {shown} but it is not a "
                    f"context/coding root — run: make context-root-add DIR={shown}\n"
                )
    return 0


def _doctor_indexes(projects_root: Path) -> int:
    """Print TAB-separated OK/WARN lines summing up the projects' indexes. A
    stale generated index is no warning: the WebUI prefill and scripts/up.sh
    refresh it. Only an index that can't be built warns."""
    if not projects_root.is_dir():
        return 0
    r = survey_projects(projects_root, refresh=False)
    if r["fresh"] or r["stale"] or r["hand_written"]:
        parts = [f"{len(r['fresh'])} fresh"]
        if r["stale"]:
            parts.append(f"{len(r['stale'])} stale (refreshed on the next start or chat turn)")
        if r["hand_written"]:
            parts.append(f"{len(r['hand_written'])} hand-written (not checked)")
        sys.stdout.write(f"OK\tproject indexes: {', '.join(parts)}\n")
    for item in r["failed"]:
        sys.stdout.write(
            f"WARN\tINDEX cannot be built ({item['project']}): {item['error']}\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stderr.write(
            "Usage: project_doctor.py doctor-visibility "
            "<projects-root> [coding-primary] [coding-extra] [context-extra]\n"
            "       project_doctor.py doctor-indexes <projects-root>\n"
        )
        return 2
    if args[0] == "doctor-indexes":
        if len(args) != 2:
            sys.stderr.write("doctor-indexes requires a projects root\n")
            return 2
        return _doctor_indexes(Path(args[1]).expanduser())
    if args[0] == "doctor-visibility":
        if len(args) < 2:
            sys.stderr.write("doctor-visibility requires a projects root\n")
            return 2
        return _doctor_visibility(
            Path(args[1]).expanduser(),
            args[2] if len(args) > 2 else "",
            args[3] if len(args) > 3 else "",
            args[4] if len(args) > 4 else "",
        )
    sys.stderr.write(f"unknown command: {args[0]}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
