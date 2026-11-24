#!/usr/bin/env python3
"""``hold: true`` on the dashboard's ``POST /runs/{run_id}/terminate``.

Compatibility overlay (not upstream), for Boundary change request 0005 R2.
Copies kanban_hold.py into hermes_cli/ and adds a ``hold`` field to the
terminate body; with it the handler calls ``kanban_hold.stop_and_hold`` in
place of ``reclaim_task`` (plugins/kanban/dashboard/plugin_api.py). Without it
terminate behaves as upstream.

See kanban_hold.py for the design and docs/upstream/kanban-terminate-hold.md
for the upstream draft.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, edit, install

MARKER = "# assistant-stack: kanban hold"
TAG = "[patch-kanban-hold]"


def main() -> None:
    install(BOOTSTRAP / "kanban_hold.py", ROOT / "hermes_cli" / "kanban_hold.py", TAG)
    body_old = (
        "class TerminateRunBody(BaseModel):\n"
        "    reason: Optional[str] = None\n"
    )
    body_new = body_old + (
        f"    {MARKER}: stop the worker and leave the task sticky blocked.\n"
        "    hold: bool = False\n"
    )
    handler_old = (
        "            raise _conflict(f\"run {run_id} already ended\")\n"
        "        if not kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason):\n"
    )
    handler_new = (
        "            raise _conflict(f\"run {run_id} already ended\")\n"
        "        if payload.hold:\n"
        "            from hermes_cli.kanban_hold import stop_and_hold as _assistant_stop_and_hold\n"
        "            if not _assistant_stop_and_hold(conn, r.task_id, run_id=run_id, reason=payload.reason):\n"
        "                raise _conflict(f\"cannot hold run {run_id}: task {r.task_id} is no longer running it\")\n"
        "            return {\"ok\": True, \"run_id\": run_id, \"task_id\": r.task_id, \"status\": \"blocked\"}\n"
        "        if not kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason):\n"
    )
    edit(ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py",
         [(body_old, body_new), (handler_old, handler_new)], MARKER, TAG, label="dashboard terminate")


if __name__ == "__main__":
    main()
