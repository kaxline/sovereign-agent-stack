#!/usr/bin/env python3
"""Checks for stopping a kanban run and holding its task (patch-kanban-hold.py / kanban_hold.py).

Needs the pinned Hermes image (skips without it unless --image). Applies the
patch twice (idempotent) and then, against a throwaway kanban board with a real
worker process, as an unprivileged user:

- POST /runs/{id}/terminate with hold: true stops the worker and returns with
  the task blocked; the run's outcome is ``blocked`` with ``operator_stop``, and
  the ``blocked`` event makes the block sticky, so dispatcher ticks leave it;
- an unblock returns the task to the pool, and a held review run to ``review``;
- a second hold on the ended run answers 409, an unknown run 404, and a hold
  whose run is no longer the task's current run changes nothing;
- terminate without hold still returns the task to ``ready``.
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
import threading
from pathlib import Path

def run_patch(name):
    out = subprocess.run([sys.executable, f"/bootstrap/{name}"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {name}: {out.stdout}{out.stderr}")
    return out.stdout

first = run_patch("patch-kanban-hold.py")
assert "patched dashboard terminate" in first, first
assert "already applied" in run_patch("patch-kanban-hold.py")

user = pwd.getpwnam("hermes")
home = Path(tempfile.mkdtemp(prefix="kanban-hold-"))
os.chown(home, user.pw_uid, user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.environ["HERMES_HOME"] = str(home)
os.environ["HOME"] = str(home)
for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
    os.environ.pop(var, None)

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

def run_row(run_id):
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()

kd._profile_exists_fn = lambda: None
workers = {}
def spawn_fn(task, workspace, *a, **k):
    proc = subprocess.Popen(["sleep", "300"])
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it: PID 1 here never does
    workers[task.id] = proc
    return proc.pid

def start(tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id != ? AND status IN ('ready', 'todo')", (tid,))
    kd.dispatch_once(conn, spawn_fn=spawn_fn)
    assert row(tid)["status"] == "running", row(tid)["status"]
    return int(row(tid)["current_run_id"])

from fastapi import FastAPI
from fastapi.testclient import TestClient
from plugins.kanban.dashboard import plugin_api
app = FastAPI()
app.include_router(plugin_api.router)
client = TestClient(app)

# Hold: the worker is gone and the task blocked when the call returns.
tid = kb.create_task(conn, title="runaway", assignee="default")
run_id = start(tid)
resp = client.post(f"/runs/{run_id}/terminate", json={"hold": True, "reason": "stopped from Boundary"})
assert resp.status_code == 200, resp.text
assert resp.json() == {"ok": True, "run_id": run_id, "task_id": tid, "status": "blocked"}, resp.json()
assert workers[tid].poll() is not None, "the worker was stopped"
t = row(tid)
assert t["status"] == "blocked" and t["claim_lock"] is None and t["worker_pid"] is None, dict(t)
r = run_row(run_id)
assert r["outcome"] == "blocked" and r["ended_at"] is not None, dict(r)
meta = json.loads(r["metadata"])
assert meta["operator_stop"] is True and meta["reason"] == "stopped from Boundary", meta
assert meta["terminated"] is True, meta
blocked = events(tid, "blocked")
assert blocked and blocked[-1]["operator_stop"] is True and blocked[-1]["source_status"] == "ready", blocked
assert not events(tid, "reclaimed"), "a hold is not a reclaim"
assert kb._has_sticky_block(conn, tid)
spawned = len(workers)
for _ in range(2):
    kb.recompute_ready(conn)
    kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert row(tid)["status"] == "blocked" and len(workers) == spawned, "dispatcher ticks leave a held task"

# Ended and unknown runs.
resp = client.post(f"/runs/{run_id}/terminate", json={"hold": True})
assert resp.status_code == 409, resp.text
resp = client.post("/runs/999999/terminate", json={"hold": True})
assert resp.status_code == 404, resp.text

# Unblock returns it to the pool; the next run can be held again.
resp = client.patch(f"/tasks/{tid}", json={"status": "ready"})
assert resp.status_code == 200, resp.text
second = start(tid)
from hermes_cli.kanban_hold import stop_and_hold
assert not stop_and_hold(conn, tid, run_id=run_id), "a stale run id changes nothing"
assert row(tid)["status"] == "running" and workers[tid].poll() is None

# Without hold, terminate still re-queues.
resp = client.post(f"/runs/{second}/terminate", json={"reason": "retry"})
assert resp.status_code == 200, resp.text
assert row(tid)["status"] == "ready" and run_row(second)["outcome"] == "reclaimed"
assert workers[tid].poll() is not None

# A held review run goes back to review on unblock.
rev = kb.create_task(conn, title="review me", assignee="default")
with kb.write_txn(conn):
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (rev,))
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id != ? AND status IN ('ready', 'todo')", (rev,))
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert row(rev)["status"] == "running", row(rev)["status"]
rid = int(row(rev)["current_run_id"])
resp = client.post(f"/runs/{rid}/terminate", json={"hold": True})
assert resp.status_code == 200, resp.text
assert events(rev, "blocked")[-1]["source_status"] == "review", events(rev, "blocked")
assert kb._resume_status_from_events(conn, rev) == "review"

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
