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
from pathlib import Path

from overlay_patch import ROOT, edit, install

BOOTSTRAP = Path(os.environ.get("KANBAN_EDIT_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: kanban edit"
TAG = "[patch-kanban-edit]"


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
    edit(ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py", [(body_old, body_new), (handler_old, handler_new)],
         MARKER, TAG, label="dashboard PATCH")


def _patch_toolsets() -> None:
    old = '    "kanban_unblock",\n'
    new = old + f'    "kanban_update", "kanban_archive",  {MARKER}\n'
    edit(ROOT / "toolsets.py", [(old, new)], MARKER, TAG, label="core tool list")


def _patch_tools() -> None:
    install_call = (
        f"\n{MARKER}: kanban_update and kanban_archive (orchestrator-only).\n"
        "try:\n"
        "    import sys as _assistant_sys\n"
        "    from hermes_cli import kanban_edit as _assistant_kanban_edit\n"
        "    _assistant_kanban_edit.install_tools(_assistant_sys.modules[__name__])\n"
        "except Exception:\n"
        "    logger.exception(\"kanban edit overlay failed to install\")\n"
    )
    edit(ROOT / "tools" / "kanban_tools.py", [], MARKER, TAG, label="kanban tools", append=install_call,
         requires=["\nlogger = ", "\n_TOOLS = ("])


def main() -> None:
    install(BOOTSTRAP / "kanban_edit.py", ROOT / "hermes_cli" / "kanban_edit.py", TAG)
    _patch_dashboard()
    _patch_toolsets()
    _patch_tools()


if __name__ == "__main__":
    main()
