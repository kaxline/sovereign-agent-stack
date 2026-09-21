#!/usr/bin/env python3
"""Merge /opt/projects FS library into WebUI load_workspaces (on read).

Immediate children of HERMES_WEBUI_DEFAULT_WORKSPACE (/opt/projects) appear in
the workspace dropdown without a manual add. Custom names in workspaces.json
are preserved; vanished library folders drop from the effective list; paths
outside the library root stay as explicit manual workspaces.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# assistant-stack: fs projects library workspaces"

OLD = '''def load_workspaces() -> list:
    ws_file = _workspaces_file()
    if ws_file.exists():
        try:
            raw = json.loads(ws_file.read_text(encoding='utf-8'))
            cleaned = _clean_workspace_list(raw)
            if len(cleaned) != len(raw):
                # Persist the cleaned version so stale entries don't keep reappearing
                try:
                    ws_file.write_text(
                        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
                    )
                except Exception:
                    logger.debug("Failed to persist cleaned workspace list")
            return cleaned or [{'path': _profile_default_workspace(), 'name': 'Home'}]
        except Exception:
            logger.debug("Failed to load workspaces from %s", ws_file)
    # No profile-local file yet.
    # For the DEFAULT profile: migrate from the legacy global file (one-time cleanup).
    # For NAMED profiles: always start clean with just their own workspace.
    try:
        from api.profiles import get_active_profile_name
        is_default = get_active_profile_name() in ('default', None)
    except ImportError:
        is_default = True
    if is_default:
        migrated = _migrate_global_workspaces()
        if migrated:
            return migrated
    # Fresh start: single entry from the profile's configured workspace, labeled "Home"
    return [{'path': _profile_default_workspace(), 'name': 'Home'}]'''

NEW = '''def load_workspaces() -> list:
    # assistant-stack: fs projects library workspaces
    def _with_library(workspaces: list) -> list:
        try:
            import sys
            if "/bootstrap" not in sys.path:
                sys.path.insert(0, "/bootstrap")
            from projects_library import merge_workspaces_with_library
            return merge_workspaces_with_library(workspaces)
        except Exception:
            logger.debug("FS projects library merge failed", exc_info=True)
            return workspaces

    ws_file = _workspaces_file()
    if ws_file.exists():
        try:
            raw = json.loads(ws_file.read_text(encoding='utf-8'))
            cleaned = _clean_workspace_list(raw)
            if len(cleaned) != len(raw):
                # Persist the cleaned version so stale entries don't keep reappearing
                try:
                    ws_file.write_text(
                        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding='utf-8'
                    )
                except Exception:
                    logger.debug("Failed to persist cleaned workspace list")
            base = cleaned or [{'path': _profile_default_workspace(), 'name': 'Home'}]
            return _with_library(base)
        except Exception:
            logger.debug("Failed to load workspaces from %s", ws_file)
    # No profile-local file yet.
    # For the DEFAULT profile: migrate from the legacy global file (one-time cleanup).
    # For NAMED profiles: always start clean with just their own workspace.
    try:
        from api.profiles import get_active_profile_name
        is_default = get_active_profile_name() in ('default', None)
    except ImportError:
        is_default = True
    if is_default:
        migrated = _migrate_global_workspaces()
        if migrated:
            return _with_library(migrated)
    # Fresh start: single entry from the profile's configured workspace, labeled "Home"
    return _with_library([{'path': _profile_default_workspace(), 'name': 'Home'}])'''


def patch(path: Path) -> str:
    if not path.is_file():
        return f"skip missing {path}"
    text = path.read_text()
    if MARKER in text:
        return f"already patched {path}"
    if OLD not in text:
        return f"pattern not found in {path} (upstream changed?)"
    path.write_text(text.replace(OLD, NEW, 1))
    return f"patched {path}"


def main(argv: list[str]) -> int:
    targets = [Path(p) for p in argv[1:]] or [
        Path("/apptoo/api/workspace.py"),
        Path("/app/api/workspace.py"),
    ]
    for target in targets:
        print(f"[patch-webui-projects-library] {patch(target)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
