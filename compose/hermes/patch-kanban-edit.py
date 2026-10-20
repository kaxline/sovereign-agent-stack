#!/usr/bin/env python3
"""Change a kanban task's workspace in place; kanban_update and kanban_archive tools.

Compatibility overlay (not upstream), for Boundary change request 0006 R2.
Copies kanban_edit.py into hermes_cli/ and:

- adds ``workspace_kind``, ``workspace_path``, ``clear_workspace_path`` and
  ``project_id`` to the dashboard's PATCH body and applies them first, so a
  refused change leaves the rest of the PATCH unapplied
  (plugins/kanban/dashboard/plugin_api.py);
- lists ``kanban_update`` and ``kanban_archive`` in the core tool list next to
  ``kanban_unblock``, which puts them in the ``kanban`` toolset (toolsets.py);
- registers both tools at the end of tools/kanban_tools.py.

See kanban_edit.py for the design and docs/extending.md for the contract.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
BOOTSTRAP = Path(os.environ.get("KANBAN_EDIT_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: kanban edit"
TAG = "[patch-kanban-edit]"


def _install() -> None:
    src, dst = BOOTSTRAP / "kanban_edit.py", ROOT / "hermes_cli" / "kanban_edit.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, dst)
    print(f"{TAG} installed {dst}")


def _edit(path: Path, label: str, edits: list[tuple[str, str, int]], append: str = "") -> None:
    """Apply (old, new, expected_count) edits to *path* once; MARKER makes it idempotent."""
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({label})")
        return
    for old, new, count in edits:
        found = text.count(old)
        if found != count:
            raise SystemExit(
                f"{TAG} expected {count} x {old.strip()[:60]!r} in {path}, found {found}. "
                "Hermes image may have changed — update this patch.")
        text = text.replace(old, new)
    if append:
        text = text.rstrip("\n") + "\n" + append
    path.write_text(text)
    print(f"{TAG} patched {label}")


def _patch_dashboard() -> None:
    body_old = (
        "    reasoning_effort: Optional[str] = None\n"
        "    clear_reasoning_effort: bool = False\n"
        "\n"
        "\n"
        "class BulkTaskBody(BaseModel):\n"
    )
    body_new = (
        "    reasoning_effort: Optional[str] = None\n"
        "    clear_reasoning_effort: bool = False\n"
        f"    {MARKER}: workspace fields; refused while the task runs.\n"
        "    workspace_kind: Optional[str] = None\n"
        "    workspace_path: Optional[str] = None\n"
        "    clear_workspace_path: bool = False\n"
        "    project_id: Optional[str] = None\n"
        "\n"
        "\n"
        "class BulkTaskBody(BaseModel):\n"
    )
    handler_old = (
        "def update_task(task_id: str, payload: UpdateTaskBody, board: Optional[str] = Query(None)):\n"
        "    with _board_conn(board) as (board, conn):\n"
        "        _require_task(conn, task_id)\n"
    )
    handler_new = handler_old + (
        "        from hermes_cli.kanban_edit import apply_patch as _assistant_patch_workspace\n"
        "        _assistant_patch_workspace(conn, task_id, payload, board)\n"
    )
    _edit(ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py", "dashboard PATCH", [
        (body_old, body_new, 1),
        (handler_old, handler_new, 1),
    ])


def _patch_toolsets() -> None:
    old = '    "kanban_unblock",\n'
    new = old + f'    "kanban_update", "kanban_archive",  {MARKER}\n'
    _edit(ROOT / "toolsets.py", "core tool list", [(old, new, 1)])


def _patch_tools() -> None:
    install = (
        f"\n{MARKER}: kanban_update and kanban_archive (orchestrator-only).\n"
        "try:\n"
        "    import sys as _assistant_sys\n"
        "    from hermes_cli import kanban_edit as _assistant_kanban_edit\n"
        "    _assistant_kanban_edit.install_tools(_assistant_sys.modules[__name__])\n"
        "except Exception:\n"
        "    logger.exception(\"kanban edit overlay failed to install\")\n"
    )
    path = ROOT / "tools" / "kanban_tools.py"
    text = path.read_text()
    if MARKER not in text and ("\nlogger = " not in text or "\n_TOOLS = (" not in text):
        raise SystemExit(f"{TAG} tools/kanban_tools.py has no module-level logger or _TOOLS. "
                         "Hermes image may have changed — update this patch.")
    _edit(path, "kanban tools", [], append=install)


def main() -> None:
    _install()
    _patch_dashboard()
    _patch_toolsets()
    _patch_tools()


if __name__ == "__main__":
    main()
