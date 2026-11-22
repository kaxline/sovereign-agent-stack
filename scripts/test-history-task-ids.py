#!/usr/bin/env python3
"""Checks for the filed task id in session history (patch-history-task-ids.py / history_task_ids.py).

Needs the pinned Hermes image (skips without it unless --image). Applies the
patch twice (idempotent), then projects a synthetic history through
``_history_to_messages``, the projection behind ``session.history`` and resume:

- a ``kanban_create`` row carries ``result: {task_id}``, also when the stored
  result is wrapped or followed by a note, and nothing else from the result;
- two tasks filed in one turn keep their own ids;
- a refused ``kanban_create`` and other tools' rows get no ``result``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402


IMAGE_DRIVER = r'''
import json
import os
import subprocess
import sys

def run_patch(name):
    out = subprocess.run([sys.executable, f"/bootstrap/{name}"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {name}: {out.stdout}{out.stderr}")
    return out.stdout

first = run_patch("patch-history-task-ids.py")
assert "patched history tool rows" in first, first
assert "already applied" in run_patch("patch-history-task-ids.py")

sys.path.insert(0, "/opt/hermes")
report = os.fdopen(os.dup(1), "w")  # the gateway import points sys.stdout at stderr
from tui_gateway.server import _history_to_messages

def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

def ok(tid):
    return json.dumps({"ok": True, "task_id": tid, "run_id": None, "status": "ready", "assignee": "default"})

history = [
    {"role": "user", "content": "file two tasks"},
    {"role": "assistant", "content": "", "tool_calls": [
        call("c1", "kanban_create", {"title": "Same", "assignee": "default"}),
        call("c2", "kanban_create", {"title": "Same", "assignee": "default"}),
        call("c3", "kanban_create", {"title": "Bad", "workspace_kind": "worktree"}),
        call("c4", "kanban_create", {"title": "Wrapped"}),
        call("c5", "terminal", {"command": "echo t_0123456789ab"}),
    ]},
    {"role": "tool", "tool_call_id": "c1", "content": ok("t_aaaaaaaaaaaa")},
    {"role": "tool", "tool_call_id": "c2", "content": ok("t_bbbbbbbbbbbb")},
    {"role": "tool", "tool_call_id": "c3", "content": json.dumps({"error": "workspace can't be built"})},
    {"role": "tool", "tool_call_id": "c4", "content":
        "<untrusted_tool_result>\n" + ok("t_cccccccccccc") + "\n</untrusted_tool_result>\nNote: {not json}"},
    {"role": "tool", "tool_call_id": "c5", "content": json.dumps({"ok": True, "task_id": "t_0123456789ab"})},
    {"role": "assistant", "content": "Filed."},
]
rows = {m["tool_call_id"]: m for m in _history_to_messages(history) if m["role"] == "tool"}
assert rows["c1"]["result"] == {"task_id": "t_aaaaaaaaaaaa"}, rows["c1"]
assert rows["c2"]["result"] == {"task_id": "t_bbbbbbbbbbbb"}, rows["c2"]
assert rows["c4"]["result"] == {"task_id": "t_cccccccccccc"}, rows["c4"]
assert "result" not in rows["c3"], rows["c3"]
assert "result" not in rows["c5"], "only kanban_create rows carry a result"
assert rows["c1"]["name"] == "kanban_create" and rows["c1"]["args"]["title"] == "Same", rows["c1"]
assert "content" not in rows["c1"], "the rest of the result stays out of the projection"

print("image ok", file=report, flush=True)
os._exit(0)
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
