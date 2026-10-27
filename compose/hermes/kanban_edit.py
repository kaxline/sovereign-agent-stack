"""Change a kanban task's workspace in place, and archive tasks from agent tools.

Compatibility overlay (not upstream), for Boundary change request 0006 R2.
patch-kanban-edit.py copies this file to ``hermes_cli/kanban_edit.py``.

- ``set_task_workspace`` changes ``workspace_kind``, ``workspace_path`` and
  ``project_id`` on a task that isn't running or archived. The new workspace is
  checked the way ``create_task`` checks it (``kanban_guards``), and an
  ``edited`` event records the old and new values. The task keeps its id, runs,
  comments, attachments and links; a blocked task stays blocked until unblocked.
- ``apply_patch`` is the dashboard's ``PATCH /tasks/{id}`` step: 404 for an
  unknown task, 409 while it runs, 400 for a workspace that can't be built.
- ``install_tools`` registers two orchestrator-only tools beside
  ``kanban_unblock``: ``kanban_update`` (the same change) and ``kanban_archive``
  (refuses a running task).

Defaults when a field is left out: changing the kind drops the stored path
(the dispatcher stamps the last resolved path there) and, for ``scratch``, the
project link, since a project would turn ``scratch`` back into ``worktree``.
"""
from __future__ import annotations

import os
from typing import Any, Optional

UNSET: Any = object()
FIELDS = ("workspace_kind", "workspace_path", "project_id", "branch_name")


class UnknownTask(ValueError):
    pass


class TaskRunning(ValueError):
    pass


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None


def set_task_workspace(
    conn: Any, task_id: str, *, workspace_kind: Any = UNSET, workspace_path: Any = UNSET,
    project_id: Any = UNSET, board: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Change a task's workspace; returns ``{field: new value}`` for what changed."""
    from hermes_cli import kanban_db as _kb

    task = _kb.get_task(conn, task_id)
    if task is None:
        raise UnknownTask(f"unknown task {task_id}")
    if task.status == "running":
        raise TaskRunning(f"task {task_id} is running; change its workspace once the run ends or is stopped")
    if task.status == "archived":
        raise ValueError(f"task {task_id} is archived")

    kind = task.workspace_kind or "scratch"
    if workspace_kind is not UNSET and workspace_kind is not None:
        kind = str(workspace_kind).strip()
    if kind not in _kb.VALID_WORKSPACE_KINDS:
        raise ValueError(f"workspace_kind must be one of {sorted(_kb.VALID_WORKSPACE_KINDS)}, got {kind!r}")
    kind_changed = kind != (task.workspace_kind or "scratch")

    if project_id is not UNSET:
        new_project = _clean(project_id)
    elif kind == "scratch" and kind_changed:
        new_project = None
    else:
        new_project = task.project_id
    project_changed = new_project != task.project_id

    if workspace_path is not UNSET:
        path = _clean(workspace_path)
    elif kind_changed or project_changed:
        path = None
    else:
        path = task.workspace_path

    project_obj, project_repo = None, None
    if new_project is not None:
        pid, project_obj, project_repo, kind = _kb._resolve_project_link(conn, new_project, None, kind, path)
        if pid is None and project_changed:
            raise ValueError(f"unknown project {new_project!r}")
        new_project = pid or new_project

    if path is None and project_repo is None and kind in {"dir", "worktree"}:
        path = _clean(_kb._board_meta_for(board).get("default_workdir"))
    try:
        from hermes_cli.kanban_guards import check_filing_workspace
    except ImportError:  # guards overlay not installed
        check_filing_workspace = None
    if check_filing_workspace is not None:
        check_filing_workspace(workspace_kind=kind, workspace_path=path, project_repo=project_repo, board=board)
    if project_repo and kind == "worktree" and not path:
        path = os.path.join(project_repo, ".worktrees", task_id)

    if kind != "worktree":
        branch = None
    elif project_obj is not None and (project_changed or kind_changed or not task.branch_name):
        branch = _kb._project_branch_name(project_obj, task_id, task.title)
    elif kind_changed:
        branch = None
    else:
        branch = task.branch_name

    before = {f: getattr(task, f) for f in FIELDS}
    after = {"workspace_kind": kind, "workspace_path": path, "project_id": new_project, "branch_name": branch}
    changed = {f: after[f] for f in FIELDS if after[f] != before[f]}
    if not changed:
        return {}
    with _kb.write_txn(conn):
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(f'{f} = ?' for f in changed)} "
            "WHERE id = ? AND status NOT IN ('running', 'archived')",
            (*changed.values(), task_id),
        )
        if cur.rowcount != 1:
            raise TaskRunning(f"task {task_id} started running; change its workspace once the run ends")
        _kb._append_event(conn, task_id, "edited", {
            "fields": list(changed),
            "from": {f: before[f] for f in changed},
            "to": changed,
        })
    _kb.notify_task_updated(conn, task_id, list(changed), board=board)
    return changed


def apply_patch(conn: Any, task_id: str, payload: Any, board: Optional[str]) -> None:
    """PATCH /tasks/{id} workspace step; no-op when the body sets none of the fields."""
    from fastapi import HTTPException

    kwargs: dict[str, Any] = {}
    if getattr(payload, "workspace_kind", None) is not None:
        kwargs["workspace_kind"] = payload.workspace_kind
    if getattr(payload, "clear_workspace_path", False):
        if getattr(payload, "workspace_path", None) is not None:
            raise HTTPException(status_code=400, detail="send workspace_path or clear_workspace_path, not both")
        kwargs["workspace_path"] = None
    elif getattr(payload, "workspace_path", None) is not None:
        kwargs["workspace_path"] = payload.workspace_path
    if getattr(payload, "project_id", None) is not None:
        kwargs["project_id"] = payload.project_id  # "" clears the link
    if not kwargs:
        return
    try:
        set_task_workspace(conn, task_id, board=board, **kwargs)
    except UnknownTask as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TaskRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_ORCH_NOTE = (
    " Orchestrator-only: profiles with the kanban toolset see it; "
    "dispatcher-spawned task workers never do."
)


def _schemas(kt: Any) -> tuple[dict, dict]:
    from tools import kanban_tools_schemas as _s

    update = _s._schema(
        "kanban_update",
        (
            "Change a task's workspace while it isn't running, for example after it "
            "gave up because its workspace couldn't be built. The task keeps its id, "
            "history, comments and links. The new workspace is checked like "
            "kanban_create's. A blocked task stays blocked: call kanban_unblock next."
            + _ORCH_NOTE
        ),
        {
            "task_id": _s._prop("string", "Task to change. Must not be running or archived."),
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "New workspace flavor. Changing it drops the old workspace_path unless "
                    "you pass one; 'scratch' also drops the project link unless you pass project_id."
                ),
            },
            "workspace_path": _s._prop(
                "string", "Absolute path for 'dir' or 'worktree'. An empty string clears it."),
            "project_id": _s._prop(
                "string", "Project id or slug to link (its repo becomes the worktree). An empty string unlinks."),
        },
        ["task_id"],
    )
    archive = _s._schema(
        "kanban_archive",
        (
            "Archive a task that isn't running, such as the original after you filed a "
            "replacement. Archived tasks drop off the board and no longer hold back "
            "their children. To stop a running task, block it first." + _ORCH_NOTE
        ),
        {"task_id": _s._prop("string", "Task to archive.")},
        ["task_id"],
    )
    return update, archive


def install_tools(kt: Any) -> None:
    """Register kanban_update and kanban_archive with the kanban tools module ``kt``."""
    if getattr(kt, "_assistant_kanban_edit_installed", False):
        return
    update_schema, archive_schema = _schemas(kt)

    def _handle_update(args: dict, **kw) -> str:
        kt._reject_delegated_child_mutation("kanban_update")
        kt._require_orchestrator_tool("kanban_update")
        tid = args.get("task_id")
        kt._check(tid, "task_id is required")
        tid = str(tid)
        kt._enforce_worker_task_ownership(tid)
        # A null is "not sent"; an empty string is the explicit clear.
        kwargs = {k: args[k] for k in ("workspace_kind", "workspace_path", "project_id")
                  if args.get(k) is not None}
        kt._check(kwargs, "pass workspace_kind, workspace_path or project_id. Nothing changed.")
        with kt._board(args.get("board")) as (kb, conn):
            changed = set_task_workspace(conn, tid, board=args.get("board"), **kwargs)
            task = kb.get_task(conn, tid)
            return kt._ok(task_id=tid, changed=sorted(changed),
                          **kt._fields(task, ("status", "workspace_kind", "workspace_path", "project_id")))

    def _handle_archive(args: dict, **kw) -> str:
        kt._reject_delegated_child_mutation("kanban_archive")
        kt._require_orchestrator_tool("kanban_archive")
        tid = args.get("task_id")
        kt._check(tid, "task_id is required")
        tid = str(tid)
        kt._enforce_worker_task_ownership(tid)
        with kt._board(args.get("board")) as (kb, conn):
            task = kb.get_task(conn, tid)
            kt._check(task is not None, f"unknown task {tid}")
            kt._check(task.status != "running",
                      f"task {tid} is running; block it (or wait for the run) before archiving. Nothing changed.")
            kt._check(task.status != "archived", f"task {tid} is already archived")
            kt._check(kb.archive_task(conn, tid), f"could not archive {tid}")
            return kt._ok(task_id=tid, status="archived")

    for name, schema, handler, emoji in (
        ("kanban_update", update_schema, _handle_update, "✏"),
        ("kanban_archive", archive_schema, _handle_archive, "🗄"),
    ):
        kt.registry.register(name=name, toolset="kanban", schema=schema,
                             handler=kt._kanban_handler(name)(handler), emoji=emoji,
                             check_fn=kt._check_kanban_orchestrator_mode)
    kt._assistant_kanban_edit_installed = True
