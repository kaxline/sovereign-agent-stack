#!/usr/bin/env python3
"""FS project library: immediate children of /opt/projects are discoverable Projects.

Shared by the Hermes cont-init overlay (projects.list / projects.tree) and the
WebUI workspace-list patch. Disk is source of truth for library existence;
projects.db / workspaces.json only attach metadata when paths match.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_LIBRARY_ROOT = "/opt/projects"
FS_ID_PREFIX = "fs:"
ENV_LIBRARY_ROOT = "HERMES_PROJECTS_LIBRARY_ROOT"


def library_root() -> str:
    raw = (os.environ.get(ENV_LIBRARY_ROOT) or DEFAULT_LIBRARY_ROOT).strip()
    return _norm(raw or DEFAULT_LIBRARY_ROOT)


def _norm(path: str) -> str:
    text = (path or "").strip()
    if not text:
        return ""
    return os.path.normpath(text)


def fs_project_id(path: str) -> str:
    return f"{FS_ID_PREFIX}{_norm(path)}"


def parse_fs_project_id(project_id: str) -> Optional[str]:
    raw = str(project_id or "")
    if not raw.startswith(FS_ID_PREFIX):
        return None
    path = _norm(raw[len(FS_ID_PREFIX) :])
    return path or None


def is_library_child_path(path: str, root: Optional[str] = None) -> bool:
    """True when path is an immediate child of the library root."""
    root_n = _norm(root or library_root())
    path_n = _norm(path)
    if not root_n or not path_n or path_n == root_n:
        return False
    prefix = root_n + os.sep
    if not path_n.startswith(prefix):
        return False
    rest = path_n[len(prefix) :]
    return bool(rest) and os.sep not in rest and not rest.startswith(".")


def list_library_entries(root: Optional[str] = None) -> list[dict[str, str]]:
    """Immediate non-hidden subdirectories of the library root.

    Skips files (e.g. README.md), hidden dirs, and non-directories.
    A plain folder is enough — AGENTS.md is not required.
    """
    root_n = _norm(root or library_root())
    root_path = Path(root_n)
    if not root_path.is_dir():
        return []

    denylist = {
        s.strip()
        for s in (os.environ.get("HERMES_PROJECTS_LIBRARY_DENYLIST") or "").split(",")
        if s.strip()
    }

    entries: list[dict[str, str]] = []
    try:
        children = sorted(root_path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []

    for child in children:
        name = child.name
        if not name or name.startswith("."):
            continue
        if name in denylist:
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        path = _norm(str(child))
        entries.append({"slug": name, "path": path, "name": name})
    return entries


def find_library_entry_for_path(
    path: str, root: Optional[str] = None
) -> Optional[dict[str, str]]:
    path_n = _norm(path)
    for entry in list_library_entries(root):
        if _norm(entry["path"]) == path_n:
            return entry
    return None


def synthetic_project_dict(entry: dict[str, str]) -> dict[str, Any]:
    path = _norm(entry["path"])
    slug = entry.get("slug") or Path(path).name
    name = entry.get("name") or slug
    return {
        "id": fs_project_id(path),
        "slug": slug,
        "name": name,
        "description": None,
        "icon": None,
        "color": None,
        "board_slug": None,
        "primary_path": path,
        "archived": False,
        "created_at": 0,
        "folders": [
            {
                "path": path,
                "label": None,
                "is_primary": True,
                "added_at": 0,
            }
        ],
    }


def _project_folder_paths(project: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    primary = project.get("primary_path")
    if primary:
        paths.append(_norm(str(primary)))
    for folder in project.get("folders") or []:
        if isinstance(folder, dict):
            fp = folder.get("path")
        else:
            fp = None
        if fp:
            paths.append(_norm(str(fp)))
    # Preserve order, drop dupes / empties.
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p and p not in seen:
            out.append(p)
            seen.add(p)
    return out


def merge_projects_with_library(
    db_projects: Iterable[dict[str, Any]] | None,
    root: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Virtual merge: disk children define the library; DB rows attach by path.

    - Live `/opt/projects/<slug>` → DB row if primary/folder matches, else synthetic.
    - DB-only projects outside the library root remain.
    - Stale DB rows for deleted library children are omitted from the library view.
    """
    root_n = _norm(root or library_root())
    entries = list_library_entries(root_n)
    disk_paths = {_norm(e["path"]) for e in entries}

    covered: dict[str, dict[str, Any]] = {}
    outside: list[dict[str, Any]] = []
    outside_ids: set[str] = set()

    for project in db_projects or []:
        if not isinstance(project, dict):
            continue
        folder_paths = _project_folder_paths(project)
        hits = [p for p in folder_paths if p in disk_paths]
        library_refs = [p for p in folder_paths if is_library_child_path(p, root_n)]
        non_library = [
            p
            for p in folder_paths
            if p != root_n and not is_library_child_path(p, root_n)
        ]

        if hits:
            for hit in hits:
                covered.setdefault(hit, project)
            continue

        if library_refs and not non_library:
            # Stale library-only row (folder gone) — drop from library view.
            continue

        pid = str(project.get("id") or "")
        if pid and pid not in outside_ids:
            outside.append(project)
            outside_ids.add(pid)
        elif not pid:
            outside.append(project)

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for entry in entries:
        path = _norm(entry["path"])
        if path in covered:
            project = covered[path]
            pid = str(project.get("id") or "")
            if pid and pid in seen_ids:
                continue
            result.append(project)
            if pid:
                seen_ids.add(pid)
        else:
            syn = synthetic_project_dict(entry)
            result.append(syn)
            seen_ids.add(str(syn["id"]))

    for project in outside:
        pid = str(project.get("id") or "")
        if pid and pid in seen_ids:
            continue
        result.append(project)
        if pid:
            seen_ids.add(pid)

    return result


def merge_workspaces_with_library(
    workspaces: Iterable[dict[str, Any]] | None,
    root: Optional[str] = None,
    *,
    home_name: str = "Home",
) -> list[dict[str, str]]:
    """Merge FS library children into a WebUI workspace list (on read).

    Preserves custom names for known paths, drops vanished library children,
    keeps Home and any non-library manual paths.
    """
    root_n = _norm(root or library_root())
    entries = list_library_entries(root_n)
    disk_paths = {_norm(e["path"]) for e in entries}

    by_path: dict[str, dict[str, str]] = {}
    extras: list[dict[str, str]] = []
    home: Optional[dict[str, str]] = None

    for item in workspaces or []:
        if not isinstance(item, dict):
            continue
        path = _norm(str(item.get("path") or ""))
        if not path:
            continue
        name = str(item.get("name") or path)
        if path == root_n:
            home = {"path": root_n, "name": name or home_name}
            continue
        if is_library_child_path(path, root_n):
            if path in disk_paths:
                by_path[path] = {"path": path, "name": name}
            continue
        extras.append({"path": path, "name": name})

    if home is None:
        home = {"path": root_n, "name": home_name}

    result: list[dict[str, str]] = [home]
    for entry in entries:
        path = _norm(entry["path"])
        if path in by_path:
            result.append(by_path[path])
        else:
            result.append({"path": path, "name": entry["name"]})
    result.extend(extras)
    return result


def materialize_library_project(pdb: Any, conn: Any, path: str) -> Any:
    """Find or lazily create a DB project for a live library path.

    Returns a projects_db.Project (or None when the path is not a live library child).
    """
    path_n = _norm(path)
    if not is_library_child_path(path_n):
        return None
    if not os.path.isdir(path_n):
        return None

    existing = pdb.find_by_primary_path(conn, path_n, include_archived=True)
    if existing is not None:
        return existing
    existing = pdb.project_for_path(conn, path_n, include_archived=True)
    if existing is not None:
        return existing

    slug = Path(path_n).name
    # Do not pass slug= directly: normalize_slug rejects leading '_' / '-', while
    # create_project's _slugify(name) accepts those folder names.
    pid = pdb.create_project(
        conn,
        name=slug,
        primary_path=path_n,
        folders=[path_n],
    )
    return pdb.get_project(conn, pid)
