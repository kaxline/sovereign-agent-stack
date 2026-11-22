"""The filed task id on ``kanban_create`` rows of the history projection (Boundary CR 0005 R3).

Stack overlay, installed as tui_gateway/history_task_ids.py by
patch-history-task-ids.py. ``_history_to_messages`` (tui_gateway/session_history.py)
keeps a tool row's name and arguments but drops its result, so a client that
renders filed tasks as cards loses the task id when it reloads the history.
For ``kanban_create`` rows this adds ``"result": {"task_id": "t_…"}`` and nothing
else from the result.

The stored result is ``{"ok": true, "task_id": …}`` (``_ok`` in
tools/kanban_tools.py), possibly inside the ``<untrusted_tool_result>`` wrapper
or followed by tool-loop notes, so the first JSON object in the text is read.
A refused call (``ok`` missing or false) or an id that isn't ``t_<hex>`` adds
nothing.
"""
from __future__ import annotations

import json
import re

_TASK_ID = re.compile(r"t_[0-9a-f]{8,}")
_DECODER = json.JSONDecoder()


def _first_object(text: str):
    start = text.find("{")
    while start != -1:
        try:
            value, _ = _DECODER.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return value if isinstance(value, dict) else None
    return None


def tool_result(name: str, content) -> dict:
    """``{"result": {"task_id": …}}`` for a successful ``kanban_create`` row, else ``{}``."""
    if name != "kanban_create" or not isinstance(content, str):
        return {}
    result = _first_object(content)
    if not result or result.get("ok") is not True:
        return {}
    task_id = result.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        return {}
    return {"result": {"task_id": task_id}}
