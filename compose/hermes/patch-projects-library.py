#!/usr/bin/env python3
"""Merge /opt/projects FS library into Hermes projects.list / projects.tree.

Virtual merge on read (no stub rows on scan). Selecting a synthetic fs: id via
projects.set_active lazily upserts a real projects.db row so active_id works.

Copies projects_library.py onto container FS at cont-init so runtime imports
survive Docker Desktop bind-mount inode churn on /bootstrap.

Applied idempotently on hermes container start (cont-init).
"""

from __future__ import annotations

import shutil
from pathlib import Path

TARGET = Path("/opt/hermes/tui_gateway/methods_projects.py")
LIBRARY_SRC = Path("/bootstrap/projects_library.py")
LIBRARY_DST = Path("/opt/hermes/projects_library.py")
IMPORT_DIR = "/opt/hermes"
MARKER = "# assistant-stack: fs projects library merge"
LOG_SNIPPET = '[projects-library]'

OLD_PAYLOAD = '''def _projects_payload(conn) -> dict:
    from hermes_cli import projects_db as pdb
    return {
        "projects": [p.to_dict() for p in pdb.list_projects(conn, include_archived=True)],
        "active_id": pdb.get_active_id(conn)}'''

NEW_PAYLOAD = '''def _projects_payload(conn) -> dict:
    from hermes_cli import projects_db as pdb
    # assistant-stack: fs projects library merge
    projects = [p.to_dict() for p in pdb.list_projects(conn, include_archived=True)]
    try:
        import sys
        if "/opt/hermes" not in sys.path:
            sys.path.insert(0, "/opt/hermes")
        from projects_library import merge_projects_with_library
        projects = merge_projects_with_library(projects)
    except Exception as e:
        print(f"[projects-library] merge failed: {e!r}", file=sys.stderr, flush=True)
    return {
        "projects": projects,
        "active_id": pdb.get_active_id(conn)}'''

OLD_TREE = '''        projects = [p.to_dict() for p in pdb.list_projects(conn)]
        active_id = pdb.get_active_id(conn)'''

NEW_TREE = '''        projects = [p.to_dict() for p in pdb.list_projects(conn)]
        # assistant-stack: fs projects library merge
        try:
            import sys
            if "/opt/hermes" not in sys.path:
                sys.path.insert(0, "/opt/hermes")
            from projects_library import merge_projects_with_library
            projects = merge_projects_with_library(projects)
        except Exception as e:
            print(f"[projects-library] merge failed: {e!r}", file=sys.stderr, flush=True)
        active_id = pdb.get_active_id(conn)'''

OLD_GET = '''@_projects_method("projects.get")
def _(rid, params, pdb, conn) -> dict:
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})'''

NEW_GET = '''@_projects_method("projects.get")
def _(rid, params, pdb, conn) -> dict:
    # assistant-stack: fs projects library merge
    raw_id = str(params.get("id") or "")
    try:
        import sys
        if "/opt/hermes" not in sys.path:
            sys.path.insert(0, "/opt/hermes")
        from projects_library import (
            find_library_entry_for_path,
            parse_fs_project_id,
            synthetic_project_dict,
        )
        fs_path = parse_fs_project_id(raw_id)
        if fs_path is not None:
            existing = pdb.find_by_primary_path(conn, fs_path, include_archived=True)
            if existing is not None:
                return _ok(rid, {"project": existing.to_dict()})
            entry = find_library_entry_for_path(fs_path)
            if entry is None:
                raise _NoProject
            return _ok(rid, {"project": synthetic_project_dict(entry)})
    except _NoProject:
        raise
    except Exception as e:
        print(f"[projects-library] get failed: {e!r}", file=sys.stderr, flush=True)
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})'''

OLD_SET_ACTIVE = '''@_projects_method("projects.set_active")
def _(rid, params, pdb, conn) -> dict:
    pdb.set_active(conn, _require_project(pdb, conn, params).id if params.get("id") else None)
    return _ok(rid, {"active_id": pdb.get_active_id(conn)})'''

NEW_SET_ACTIVE = '''@_projects_method("projects.set_active")
def _(rid, params, pdb, conn) -> dict:
    # assistant-stack: fs projects library merge
    raw_id = params.get("id")
    if not raw_id:
        pdb.set_active(conn, None)
    else:
        try:
            import sys
            if "/opt/hermes" not in sys.path:
                sys.path.insert(0, "/opt/hermes")
            from projects_library import materialize_library_project, parse_fs_project_id
            fs_path = parse_fs_project_id(str(raw_id))
            if fs_path is not None:
                proj = materialize_library_project(pdb, conn, fs_path)
                if proj is None:
                    raise _NoProject
                pdb.set_active(conn, proj.id)
            else:
                pdb.set_active(conn, _require_project(pdb, conn, params).id)
        except _NoProject:
            raise
        except Exception as e:
            print(f"[projects-library] set_active failed: {e!r}", file=sys.stderr, flush=True)
            pdb.set_active(conn, _require_project(pdb, conn, params).id)
    return _ok(rid, {"active_id": pdb.get_active_id(conn)})'''


def install_library() -> str:
    if not LIBRARY_SRC.is_file():
        return f"missing {LIBRARY_SRC}"
    LIBRARY_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LIBRARY_SRC, LIBRARY_DST)
    return f"installed {LIBRARY_DST}"


def _is_fully_patched(text: str) -> bool:
    return (
        MARKER in text
        and "merge_projects_with_library" in text
        and "materialize_library_project" in text
        and f'"{IMPORT_DIR}"' in text
        and LOG_SNIPPET in text
        and '"/bootstrap"' not in text
    )


def _needs_migration(text: str) -> bool:
    return (
        MARKER in text
        and "merge_projects_with_library" in text
        and "materialize_library_project" in text
        and not _is_fully_patched(text)
    )


def migrate(text: str) -> str:
    """Rewrite an older overlay that still imports from /bootstrap or swallows errors."""
    # Durable import path.
    text = text.replace(
        'if "/bootstrap" not in sys.path:\n            sys.path.insert(0, "/bootstrap")',
        f'if "{IMPORT_DIR}" not in sys.path:\n            sys.path.insert(0, "{IMPORT_DIR}")',
    )
    text = text.replace(
        'if "/bootstrap" not in sys.path:\n                sys.path.insert(0, "/bootstrap")',
        f'if "{IMPORT_DIR}" not in sys.path:\n                sys.path.insert(0, "{IMPORT_DIR}")',
    )

    # Silent merge/get failures → stderr.
    text = text.replace(
        """    except Exception:
        pass
    return {
        "projects": projects,
        "active_id": pdb.get_active_id(conn)}""",
        """    except Exception as e:
        print(f"[projects-library] merge failed: {e!r}", file=sys.stderr, flush=True)
    return {
        "projects": projects,
        "active_id": pdb.get_active_id(conn)}""",
    )
    text = text.replace(
        """        except Exception:
            pass
        active_id = pdb.get_active_id(conn)""",
        """        except Exception as e:
            print(f"[projects-library] merge failed: {e!r}", file=sys.stderr, flush=True)
        active_id = pdb.get_active_id(conn)""",
    )
    text = text.replace(
        """    except Exception:
        pass
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})""",
        """    except Exception as e:
        print(f"[projects-library] get failed: {e!r}", file=sys.stderr, flush=True)
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})""",
    )
    text = text.replace(
        """        except Exception:
            pdb.set_active(conn, _require_project(pdb, conn, params).id)""",
        """        except Exception as e:
            print(f"[projects-library] set_active failed: {e!r}", file=sys.stderr, flush=True)
            pdb.set_active(conn, _require_project(pdb, conn, params).id)""",
    )
    return text


def patch(path: Path) -> str:
    if not path.is_file():
        return f"skip missing {path}"
    text = path.read_text()

    if _is_fully_patched(text):
        return f"already patched {path}"

    if _needs_migration(text):
        migrated = migrate(text)
        if migrated == text:
            return f"migrate noop {path} (unexpected shape)"
        path.write_text(migrated)
        return f"migrated {path}"

    replacements = (
        (OLD_PAYLOAD, NEW_PAYLOAD, "_projects_payload"),
        (OLD_TREE, NEW_TREE, "_project_tree_inputs projects="),
        (OLD_GET, NEW_GET, "projects.get"),
        (OLD_SET_ACTIVE, NEW_SET_ACTIVE, "projects.set_active"),
    )
    missing = []
    for old, _new, label in replacements:
        if old not in text:
            missing.append(label)
    if missing:
        return f"pattern not found in {path}: {', '.join(missing)} (upstream changed?)"

    for old, new, _label in replacements:
        text = text.replace(old, new, 1)
    path.write_text(text)
    return f"patched {path}"


def main() -> int:
    print(f"[patch-projects-library] {install_library()}", flush=True)
    print(f"[patch-projects-library] {patch(TARGET)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
