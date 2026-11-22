#!/usr/bin/env python3
"""The filed task id on ``kanban_create`` rows of ``session.history``.

Compatibility overlay (not upstream), for Boundary change request 0005 R3.
Copies history_task_ids.py into tui_gateway/ and merges its ``tool_result``
into each tool row of ``_history_to_messages``
(tui_gateway/session_history.py), which every history and resume answer uses.

See history_task_ids.py for the design and docs/upstream/history-task-ids.md
for the upstream draft.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, edit, install

BOOTSTRAP = Path(os.environ.get("HISTORY_TASK_IDS_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: history task ids"
TAG = "[patch-history-task-ids]"


def main() -> None:
    install(BOOTSTRAP / "history_task_ids.py", ROOT / "tui_gateway" / "history_task_ids.py", TAG)
    old = '                             **({"args": args} if args else {}), **({"labels": labels} if labels else {})})\n'
    new = (
        '                             **({"args": args} if args else {}), **({"labels": labels} if labels else {}),\n'
        f'                             **_assistant_tool_result(name, m.get("content"))}})  {MARKER}\n'
    )
    imp_old = "            labels = _bridged_tool_labels(name, args)\n"
    imp_new = imp_old + "            from tui_gateway.history_task_ids import tool_result as _assistant_tool_result\n"
    edit(ROOT / "tui_gateway" / "session_history.py", [(old, new), (imp_old, imp_new)], MARKER, TAG,
         label="history tool rows")


if __name__ == "__main__":
    main()
