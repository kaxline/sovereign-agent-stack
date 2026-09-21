#!/usr/bin/env python3
"""Merge /opt/projects FS library into WebUI load_workspaces (on read).

Immediate children of HERMES_WEBUI_DEFAULT_WORKSPACE (/opt/projects) appear in
the workspace dropdown without a manual add. Custom names in workspaces.json
are preserved; vanished library folders drop from the effective list; paths
outside the library root stay as explicit manual workspaces.

Copies projects_library.py onto the WebUI app FS at entrypoint so runtime
imports survive Docker Desktop bind-mount inode churn on /bootstrap.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "# assistant-stack: fs projects library workspaces"
LIBRARY_SRC = Path("/bootstrap/projects_library.py")
LOG_SNIPPET = "FS projects library merge failed"

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


def _new_block(import_dir: str) -> str:
    return f'''def load_workspaces() -> list:
    # assistant-stack: fs projects library workspaces
    def _with_library(workspaces: list) -> list:
        try:
            import sys
            if "{import_dir}" not in sys.path:
                sys.path.insert(0, "{import_dir}")
            from projects_library import merge_workspaces_with_library
            return merge_workspaces_with_library(workspaces)
        except Exception:
            logger.warning("FS projects library merge failed", exc_info=True)
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
            base = cleaned or [{{'path': _profile_default_workspace(), 'name': 'Home'}}]
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
    return _with_library([{{'path': _profile_default_workspace(), 'name': 'Home'}}])'''


def _import_dir_for(path: Path) -> str:
    # /app/api/workspace.py → /app ; /apptoo/api/workspace.py → /apptoo
    return str(path.resolve().parent.parent)


def install_library(import_dir: str) -> str:
    if not LIBRARY_SRC.is_file():
        return f"missing {LIBRARY_SRC}"
    dst = Path(import_dir) / "projects_library.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LIBRARY_SRC, dst)
    return f"installed {dst}"


# Paths that may appear in older overlays or after /apptoo → /app rsync.
_LEGACY_IMPORT_DIRS = ("/bootstrap", "/apptoo", "/app")


def _is_fully_patched(text: str, import_dir: str) -> bool:
    if not (
        MARKER in text
        and "merge_workspaces_with_library" in text
        and f'"{import_dir}"' in text
        and "logger.warning" in text
        and LOG_SNIPPET in text
    ):
        return False
    for legacy in _LEGACY_IMPORT_DIRS:
        if legacy != import_dir and f'"{legacy}"' in text:
            return False
    return True


def _needs_migration(text: str, import_dir: str) -> bool:
    return MARKER in text and "merge_workspaces_with_library" in text and not _is_fully_patched(
        text, import_dir
    )


def migrate(text: str, import_dir: str) -> str:
    # Entrypoint patches /apptoo first; stock init rsyncs that tree to /app, so the
    # second pass must rewrite the import dir to match the runtime tree.
    for legacy in _LEGACY_IMPORT_DIRS:
        if legacy == import_dir:
            continue
        text = text.replace(
            f'if "{legacy}" not in sys.path:\n                sys.path.insert(0, "{legacy}")',
            f'if "{import_dir}" not in sys.path:\n                sys.path.insert(0, "{import_dir}")',
        )
    text = text.replace(
        'logger.debug("FS projects library merge failed", exc_info=True)',
        'logger.warning("FS projects library merge failed", exc_info=True)',
    )
    return text


def patch(path: Path) -> str:
    if not path.is_file():
        return f"skip missing {path}"
    import_dir = _import_dir_for(path)
    install_msg = install_library(import_dir)
    text = path.read_text()

    if _is_fully_patched(text, import_dir):
        return f"{install_msg}; already patched {path}"

    if _needs_migration(text, import_dir):
        migrated = migrate(text, import_dir)
        if migrated == text and not _is_fully_patched(migrated, import_dir):
            return f"{install_msg}; migrate noop {path} (unexpected shape)"
        path.write_text(migrated)
        return f"{install_msg}; migrated {path}"

    if OLD not in text:
        return f"{install_msg}; pattern not found in {path} (upstream changed?)"
    path.write_text(text.replace(OLD, _new_block(import_dir), 1))
    return f"{install_msg}; patched {path}"


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
