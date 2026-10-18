#!/usr/bin/env python3
"""Merge the /opt/projects FS library into WebUI load_workspaces (on read).

Immediate children of HERMES_WEBUI_DEFAULT_WORKSPACE (/opt/projects) appear in
the workspace dropdown without a manual add. Custom names in workspaces.json
are preserved; vanished library folders drop from the effective list; paths
outside the library root stay as explicit manual workspaces.

The patch appends a module-level wrapper to api/workspace.py that rebinds
load_workspaces to merge the stock result with the library. Callers inside the
module and later `from api.workspace import load_workspaces` imports both
resolve to the wrapper, and every return path of the stock function is
covered without anchoring on its body. The only anchor is the stock
definition line, which must still exist.

Copies projects_library.py next to api/ (into the app root) so runtime imports
survive Docker Desktop bind-mount inode churn on /bootstrap. The wrapper finds
it relative to workspace.py's own location, so the same patched text works
for /apptoo and for /app after the stock init rsyncs one onto the other.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

MARKER = "# assistant-stack: fs projects library workspaces"
LIBRARY_SRC = Path("/bootstrap/projects_library.py")
STOCK_DEF = re.compile(r"^def load_workspaces\(", re.MULTILINE)

WRAPPER = f'''

{MARKER}
_assistant_stack_stock_load_workspaces = load_workspaces


def load_workspaces(*args, **kwargs) -> list:
    workspaces = _assistant_stack_stock_load_workspaces(*args, **kwargs)
    try:
        import sys as _sys
        _app_root = str(Path(__file__).resolve().parent.parent)
        if _app_root not in _sys.path:
            _sys.path.insert(0, _app_root)
        from projects_library import merge_workspaces_with_library
        return merge_workspaces_with_library(workspaces)
    except Exception:
        logger.warning("FS projects library merge failed", exc_info=True)
        return workspaces
'''


def install_library(app_root: Path) -> str:
    if not LIBRARY_SRC.is_file():
        return f"missing {LIBRARY_SRC}"
    dst = app_root / "projects_library.py"
    shutil.copyfile(LIBRARY_SRC, dst)
    return f"installed {dst}"


def patch(path: Path) -> str:
    if not path.is_file():
        return f"skip missing {path}"
    install_msg = install_library(path.resolve().parent.parent)
    text = path.read_text()
    if MARKER in text:
        return f"{install_msg}; already patched {path}"
    if not STOCK_DEF.search(text):
        return f"{install_msg}; pattern not found in {path} (upstream changed?)"
    for needed in ("\nlogger = ", "from pathlib import Path"):
        if needed not in text:
            return f"{install_msg}; {needed.strip()!r} missing in {path} (upstream changed?)"
    path.write_text(text.rstrip("\n") + "\n" + WRAPPER)
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
