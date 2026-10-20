#!/usr/bin/env python3
"""Checks for kanban wakes on dashboard sessions (patch-kanban-wake.py / kanban_wake.py).

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the guards and wake patches are applied twice (idempotent) and then, against a
throwaway kanban board and a synthetic state.db:

- a session whose key compression rotated still claims the events of the tasks
  it filed before the rotation, exactly once;
- a subscription another live session holds, or one on an unrelated key, is
  left alone;
- the give-up text says whether a retry helps, carries the error and names the
  repair tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import kanban_wake  # noqa: E402
import hermes_image  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_gave_up_text() -> None:
    task = SimpleNamespace(id="t_1")
    final = kanban_wake.gave_up_suffix(task, {"retryable": False, "error": "workspace: no default_workdir"}, "x")
    expect("can't start until its workspace" in final, final)
    expect("Error: workspace: no default_workdir" in final, final)
    expect("kanban_update(task_id='t_1', workspace_kind='scratch')" in final, final)
    expect("kanban_unblock(task_id='t_1')" in final, final)
    retry = kanban_wake.gave_up_suffix(task, {"failures": 2, "error": "worker exited 1"}, "x")
    expect(retry.startswith(" gave up after 2 failed attempts"), retry)
    expect("kanban_show(task_id='t_1')" in retry and "kanban_archive" in retry, retry)
    long = kanban_wake.gave_up_suffix(task, {"error": "e" * 2000}, "x")
    expect(len(long) < 900, "the error is capped")


IMAGE_DRIVER = r'''
import os
import subprocess
import sys
import tempfile
from pathlib import Path

def run_patch(name):
    out = subprocess.run([sys.executable, f"/bootstrap/{name}"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {name}: {out.stdout}{out.stderr}")
    return out.stdout

run_patch("patch-kanban-guards.py")
assert "patched session notifications" in run_patch("patch-kanban-wake.py")
assert "already applied" in run_patch("patch-kanban-wake.py")

home = Path(tempfile.mkdtemp(prefix="kanban-wake-"))
os.environ["HERMES_HOME"] = str(home)
for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
    os.environ.pop(var, None)

sys.path.insert(0, "/opt/hermes")
from hermes_state import SessionDB
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kd
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server

# Synthetic lineage: "sess-a" was compressed into "sess-b".
db = SessionDB()
db.create_session("sess-a", "tui")
db.end_session("sess-a", "compression")
db.create_session("sess-b", "tui", parent_session_id="sess-a")
db.create_session("sess-z", "tui")
db.close()
live_b = {"session_key": "sess-b", "profile_home": None}
assert server._notif_resolve_event_key("sess-a", live_b) == "sess-b", "lineage resolves to the tip"

conn = kbc.connect()
kd._profile_exists_fn = lambda: None

def gave_up_task(title, chat_id):
    tid = kb.create_task(conn, title=title, assignee="default")
    kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind = 'worktree' WHERE id = ?", (tid,))
    return tid

filed_before = gave_up_task("filed before compression", "sess-a")
filed_elsewhere = gave_up_task("filed in another chat", "sess-z")
kd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
for tid in (filed_before, filed_elsewhere):
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "blocked"

# Another live session still holding sess-a owns its events.
server._notif_live_session_matches = lambda keys, exclude=None: "sess-a" in keys
assert server._collect_kanban_notifications(live_b) == [], "a key held by a live session is not adopted"

server._notif_live_session_matches = lambda keys, exclude=None: False
texts = [str(t) for t in server._collect_kanban_notifications(live_b)]
mine = [t for t in texts if filed_before in t]
assert len(mine) == 1, texts
assert "can't start until its workspace" in mine[0] and f"kanban_update(task_id='{filed_before}'" in mine[0], mine[0]
assert "no default_workdir" in mine[0], mine[0]
assert not any(filed_elsewhere in t for t in texts), "another chat's task is not claimed"
assert not any(filed_before in str(t) for t in server._collect_kanban_notifications(live_b)), "claimed once"

# The unrelated chat still gets its own event.
live_z = {"session_key": "sess-z", "profile_home": None}
assert any(filed_elsewhere in str(t) for t in server._collect_kanban_notifications(live_z))
# The gateway sends print() to stderr (stdout carries its JSON-RPC); write fd 1 directly.
os.write(1, b"image ok\n")
os._exit(0)  # skip gateway thread teardown
'''


def main() -> None:
    test_gave_up_text()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
