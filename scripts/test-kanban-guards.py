#!/usr/bin/env python3
"""Checks for the kanban guards overlay (patch-kanban-guards.py / kanban_guards.py).

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the patch is applied in the image twice (idempotent) and then, against a
throwaway kanban board, as an unprivileged user:

- create_task refuses a worktree or dir workspace that can't be built, and
  files nothing; POST /tasks answers 400 with the same message;
- workspaces that resolve today are still filed;
- a pre-existing task whose workspace can't be resolved gives up on its first
  claim, sticky, with "retryable": false; a git failure that can heal keeps the
  normal limit;
- a task filed by another task's worker leaves a comment on that creator task
  when it gives up.
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import kanban_guards  # noqa: E402
import hermes_image  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_final_classification() -> None:
    final = kanban_guards.workspace_failure_is_final
    expect(final(ValueError("no default_workdir")), "a ValueError is final")
    expect(final(OSError(errno.EROFS, "Read-only file system")), "EROFS is final")
    expect(final(PermissionError(errno.EACCES, "denied")), "EACCES is final")
    expect(not final(OSError(errno.ENOSPC, "No space left")), "ENOSPC can heal")
    expect(final(RuntimeError("git worktree add failed for /r on branch b: fatal: Read-only file system")),
           "git on a read-only repo is final")
    expect(not final(RuntimeError("git worktree add failed for /r on branch b: fatal: Unable to create "
                                  "'/r/.git/index.lock': File exists.")), "a git lock can heal")
    expect(not final(RuntimeError("something else")), "other errors keep the normal limit")


IMAGE_DRIVER = r'''
import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

def run_patch():
    out = subprocess.run([sys.executable, "/bootstrap/patch-kanban-guards.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    return out.stdout

first = run_patch()
assert "patched create_task workspace check" in first and "patched dispatcher give-up" in first, first
second = run_patch()
assert second.count("already applied") == 2, second

# Drop to the gateway's user so permission bits apply, as they do in the stack.
user = pwd.getpwnam("hermes")
home = Path(tempfile.mkdtemp(prefix="kanban-guards-"))
os.chown(home, user.pw_uid, user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.environ["HERMES_HOME"] = str(home)
os.environ["HOME"] = str(home)
for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
    os.environ.pop(var, None)

sys.path.insert(0, "/opt/hermes")
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kd

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})

def make_repo(name):
    repo = home / name
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / "README").write_text("x\n")
    git("add", "README", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    return repo

def lock_down(path):
    for p in sorted(path.rglob("*"), reverse=True) + [path]:
        p.chmod(0o555 if p.is_dir() else 0o444)

writable = make_repo("writable")
readonly = make_repo("readonly")
lock_down(readonly)
plain = home / "plain"
plain.mkdir()
ro_dir = home / "ro-dir"
ro_dir.mkdir()
ro_dir.chmod(0o555)

from hermes_cli import kanban_db_connect as kbc
conn = kbc.connect()
def task_count():
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

def refused(label, needle, **kw):
    before = task_count()
    try:
        kb.create_task(conn, title=label, assignee="default", **kw)
    except ValueError as exc:
        msg = str(exc)
        assert needle in msg, f"{label}: {msg}"
        assert "workspace_kind='scratch'" in msg, f"{label} names the fix: {msg}"
        assert task_count() == before, f"{label} filed nothing"
        return msg
    raise SystemExit(f"FAIL {label} was filed")

def filed(label, **kw):
    return kb.create_task(conn, title=label, assignee="default", **kw)

no_default = refused("worktree, no path", "no default_workdir", workspace_kind="worktree")
refused("worktree, relative", "not absolute", workspace_kind="worktree", workspace_path="repo")
refused("worktree, not a repo", "not inside a git repo", workspace_kind="worktree", workspace_path=str(plain))
refused("worktree, read-only repo", "not writable", workspace_kind="worktree", workspace_path=str(readonly))
refused("worktree, read-only child", "not writable", workspace_kind="worktree",
        workspace_path=str(readonly / ".worktrees" / "x"))
refused("dir, no path", "needs a workspace_path", workspace_kind="dir")
refused("dir, read-only", "not writable", workspace_kind="dir", workspace_path=str(ro_dir / "sub"))

filed("scratch", workspace_kind="scratch")
filed("default kind")
filed("worktree, repo root", workspace_kind="worktree", workspace_path=str(writable))
filed("worktree, under repo", workspace_kind="worktree", workspace_path=str(writable / ".worktrees" / "y"))
filed("dir, missing but creatable", workspace_kind="dir", workspace_path=str(home / "new" / "dir"))

os.environ["HERMES_KANBAN_GUARDS"] = "0"
filed("guards off", workspace_kind="worktree")
os.environ.pop("HERMES_KANBAN_GUARDS")

# The dashboard's POST /tasks maps the same ValueError to a 400.
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plugins.kanban.dashboard import plugin_api
app = FastAPI()
app.include_router(plugin_api.router)
client = TestClient(app)
before = task_count()
resp = client.post("/tasks", json={"title": "via dashboard", "assignee": "default", "workspace_kind": "worktree"})
assert resp.status_code == 400, (resp.status_code, resp.text)
assert resp.json()["detail"] == no_default, resp.json()
assert task_count() == before, "POST /tasks filed nothing"

# Spawn side: a task filed before the guards (or whose repo changed since).
kd._profile_exists_fn = lambda: None
spawned = []
def spawn_fn(task, workspace, *a, **k):
    spawned.append(task.id)
    return 4242

def legacy_task(title, creator=None, **cols):
    tid = kb.create_task(conn, title=title, assignee="default", creator_task_id=creator)
    sets = ", ".join(f"{k} = ?" for k in cols)
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*cols.values(), tid))
    return tid

def events(tid, kind):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (tid, kind)).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]

def status(tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]

def hold_others(*keep):
    with kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET status = 'blocked' WHERE id NOT IN ({','.join('?' * len(keep))}) "
                     "AND status IN ('ready', 'todo')", keep)

creator = filed("orchestrator task")
with kb.write_txn(conn):
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (creator,))
stuck = legacy_task("stuck worktree", creator=creator, workspace_kind="worktree")
hold_others(stuck)
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert status(stuck) == "blocked", status(stuck)
gave_up = events(stuck, "gave_up")
assert len(gave_up) == 1, gave_up
assert gave_up[0]["retryable"] is False and gave_up[0]["sticky"] is True, gave_up
assert gave_up[0]["trigger_outcome"] == "spawn_failed", gave_up
assert gave_up[0]["error"].startswith("workspace: "), gave_up
assert stuck not in spawned
assert kb._has_sticky_block(conn, stuck)
kb.recompute_ready(conn)
assert status(stuck) == "blocked", "a final give-up stays blocked until an operator unblocks it"

comments = conn.execute("SELECT author, body FROM task_comments WHERE task_id = ?", (creator,)).fetchall()
assert len(comments) == 1, [dict(c) for c in comments]
assert comments[0]["author"] == "kanban-dispatcher"
assert stuck in comments[0]["body"] and "no default_workdir" in comments[0]["body"], comments[0]["body"]

# A task with no creator gives up without commenting anywhere.
orphan = legacy_task("orphan", workspace_kind="dir", workspace_path=None)
hold_others(orphan)
n_comments = conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0]
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert status(orphan) == "blocked" and events(orphan, "gave_up")[0]["retryable"] is False
assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == n_comments

# A git failure that can heal keeps the two-strike limit and no retryable flag.
from hermes_cli import kanban_db_workspace as kw
real_resolve = kw._resolve_worktree_workspace
def locked(task, *, board=None):
    raise RuntimeError(f"git worktree add failed for /x on branch b: fatal: Unable to create '/x/.git/index.lock': File exists.")
kw._resolve_worktree_workspace = locked
flaky = legacy_task("flaky", workspace_kind="worktree", workspace_path=str(writable))
hold_others(flaky)
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert status(flaky) == "ready", status(flaky)
assert not events(flaky, "gave_up") and len(events(flaky, "spawn_failed")) == 1
kd.dispatch_once(conn, spawn_fn=spawn_fn)
assert status(flaky) == "blocked"
assert "retryable" not in events(flaky, "gave_up")[0], events(flaky, "gave_up")
kw._resolve_worktree_workspace = real_resolve

print("image ok")
'''


def main() -> None:
    test_final_classification()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
