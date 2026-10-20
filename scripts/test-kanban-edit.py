#!/usr/bin/env python3
"""Checks for changing a kanban task's workspace in place (patch-kanban-edit.py / kanban_edit.py).

Needs the pinned Hermes image (skips without it unless --image). Applies the
guards and edit patches twice (idempotent) and then, against a throwaway
kanban board, as an unprivileged user:

- PATCH /tasks/{id} with workspace_kind="scratch" fixes a task that gave up on
  its worktree, keeps it blocked, records an ``edited`` event, and the next
  unblock starts a worker in a scratch workspace;
- the same PATCH answers 409 on a running task, 400 for a workspace that can't
  be built (and then applies none of the PATCH), 404 for an unknown task;
- kanban_update and kanban_archive are in the kanban toolset, visible to an
  orchestrator profile, hidden from dispatcher workers, and refuse running tasks.
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
import pwd
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
first = run_patch("patch-kanban-edit.py")
for label in ("dashboard PATCH", "core tool list", "kanban tools"):
    assert f"patched {label}" in first, first
assert run_patch("patch-kanban-edit.py").count("already applied") == 3

user = pwd.getpwnam("hermes")
home = Path(tempfile.mkdtemp(prefix="kanban-edit-"))
os.chown(home, user.pw_uid, user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.environ["HERMES_HOME"] = str(home)
os.environ["HOME"] = str(home)
for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
    os.environ.pop(var, None)
# An orchestrator profile: the kanban toolset named explicitly.
(home / "config.yaml").write_text("toolsets:\n  - kanban\n")

sys.path.insert(0, "/opt/hermes")
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kd

conn = kbc.connect()

def row(tid):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()

def events(tid, kind):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (tid, kind)).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]

def legacy_task(title, **cols):
    """A task filed before the guards, with a workspace that can't be built."""
    tid = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                     (*cols.values(), tid))
    return tid

def hold_others(*keep):
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET status = 'blocked' WHERE id NOT IN ({','.join('?' * len(keep))}) "
                     "AND status IN ('ready', 'todo')", keep)

kd._profile_exists_fn = lambda: None
spawned = {}
def spawn_fn(task, workspace, *a, **k):
    spawned[task.id] = str(workspace)
    return 4242

from fastapi import FastAPI
from fastapi.testclient import TestClient
from plugins.kanban.dashboard import plugin_api
app = FastAPI()
app.include_router(plugin_api.router)
client = TestClient(app)

# R2 acceptance: a task that gave up on its worktree, fixed in place.
stuck = legacy_task("stuck", workspace_kind="worktree")
kb.add_comment(conn, stuck, "owner", "keep me")
hold_others(stuck)
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert row(stuck)["status"] == "blocked" and events(stuck, "gave_up"), row(stuck)["status"]
resp = client.patch(f"/tasks/{stuck}", json={"workspace_kind": "scratch"})
assert resp.status_code == 200, resp.text
assert resp.json()["task"]["workspace_kind"] == "scratch", resp.json()
assert row(stuck)["status"] == "blocked", "a workspace change does not unblock"
assert row(stuck)["workspace_path"] is None
edited = events(stuck, "edited")
assert edited and edited[-1]["fields"] == ["workspace_kind"], edited
assert edited[-1]["from"] == {"workspace_kind": "worktree"} and edited[-1]["to"] == {"workspace_kind": "scratch"}
assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (stuck,)).fetchone()[0] == 1
resp = client.patch(f"/tasks/{stuck}", json={"status": "ready"})
assert resp.status_code == 200, resp.text
hold_others(stuck)
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert stuck in spawned, "the next unblock starts a worker"
assert spawned[stuck] == str(kb.workspaces_root() / stuck), spawned[stuck]
assert row(stuck)["status"] == "running"

# 409 on a running task; nothing changes.
resp = client.patch(f"/tasks/{stuck}", json={"workspace_kind": "dir", "workspace_path": str(home / "d")})
assert resp.status_code == 409, resp.text
assert row(stuck)["workspace_kind"] == "scratch"

# One PATCH can fix and unblock; a refused workspace applies none of the PATCH.
other = legacy_task("other", workspace_kind="dir")
with kb.write_txn(conn):
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (other,))
resp = client.patch(f"/tasks/{other}", json={"workspace_kind": "worktree", "status": "ready", "title": "x"})
assert resp.status_code == 400 and "no default_workdir" in resp.json()["detail"], resp.text
assert row(other)["status"] == "blocked" and row(other)["title"] == "other"
resp = client.patch(f"/tasks/{other}", json={"workspace_path": "/a", "clear_workspace_path": True})
assert resp.status_code == 400, resp.text
resp = client.patch(f"/tasks/{other}", json={"project_id": "no-such-project"})
assert resp.status_code == 400 and "unknown project" in resp.json()["detail"], resp.text
d = home / "workdir"
resp = client.patch(f"/tasks/{other}", json={"workspace_path": str(d), "status": "ready"})
assert resp.status_code == 200, resp.text
assert row(other)["workspace_kind"] == "dir" and row(other)["workspace_path"] == str(d)
assert row(other)["status"] == "ready"
resp = client.patch("/tasks/t_missing", json={"workspace_kind": "scratch"})
assert resp.status_code == 404, resp.text
# A PATCH without workspace fields behaves as before.
resp = client.patch(f"/tasks/{other}", json={"title": "renamed"})
assert resp.status_code == 200 and row(other)["title"] == "renamed"

# Tools: in the kanban toolset, orchestrator-only.
import toolsets
from tools import kanban_tools as kt
from tools.registry import registry
names = toolsets.resolve_toolset("kanban")
assert "kanban_update" in names and "kanban_archive" in names, names
assert "kanban_update" not in toolsets.resolve_toolset("hermes-api-server")
update, archive = registry.get_entry("kanban_update"), registry.get_entry("kanban_archive")
assert update and archive and update.toolset == "kanban"
assert update.check_fn() and archive.check_fn(), "an orchestrator profile sees both"
import model_tools
defined = {d["function"]["name"] for d in model_tools.get_tool_definitions(enabled_toolsets=["kanban"], quiet_mode=True)}
assert {"kanban_update", "kanban_archive", "kanban_unblock"} <= defined, sorted(defined)

def call(entry, **args):
    return json.loads(entry.handler(args))

blocked = legacy_task("blocked", workspace_kind="worktree")
with kb.write_txn(conn):
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,))
out = call(update, task_id=blocked, workspace_kind="scratch", project_id=None)
assert out.get("ok") and out["changed"] == ["workspace_kind"] and out["workspace_kind"] == "scratch", out
out = call(update, task_id=blocked)
assert "error" in out and "Nothing changed" in out["error"], out
out = call(update, task_id=blocked, workspace_kind="worktree")
assert "error" in out and "no default_workdir" in out["error"], out
out = call(update, task_id=stuck, workspace_kind="dir", workspace_path=str(home / "d"))
assert "error" in out and "running" in out["error"], out
out = call(archive, task_id=stuck)
assert "error" in out and "running" in out["error"] and row(stuck)["status"] == "running", out
out = call(archive, task_id=blocked)
assert out == {"ok": True, "task_id": blocked, "status": "archived"}, out
assert row(blocked)["status"] == "archived"
out = call(archive, task_id=blocked)
assert "error" in out and "already archived" in out["error"], out
out = call(update, task_id=blocked, workspace_kind="dir", workspace_path=str(home / "d"))
assert "error" in out and "archived" in out["error"], out

# Dispatcher workers neither see nor run them.
os.environ["HERMES_KANBAN_TASK"] = other
assert not update.check_fn() and not archive.check_fn()
out = call(update, task_id=other, workspace_kind="scratch")
assert "error" in out and "orchestrator-only" in out["error"], out
os.environ.pop("HERMES_KANBAN_TASK")

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
