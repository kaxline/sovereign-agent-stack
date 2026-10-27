"""Kanban workspace checks at filing time, final workspace failures, and a gave-up comment.

Compatibility overlay (not upstream), for Boundary change request 0006.
patch-kanban-guards.py copies this file to ``hermes_cli/kanban_guards.py``.

- ``check_filing_workspace`` runs inside ``create_task`` (R1), after the project
  is resolved. It refuses a ``dir`` or ``worktree`` workspace the dispatcher
  could never build, so ``kanban_create`` returns a tool error and
  ``POST /tasks`` a 400 instead of the task giving up minutes later. It runs the
  checks ``kanban_db_workspace`` runs at spawn time, plus one it doesn't: the
  repo (and its git dir) must be writable by this process, since
  ``git worktree add`` writes to both. It creates nothing.
- ``workspace_failure_is_final`` classifies a spawn-time workspace error (R3).
  The dispatcher blocks the task on the first one that can't change between
  ticks, sticky, with ``"retryable": false`` in the ``gave_up`` payload.
- ``install_dispatch`` wraps ``_record_task_failure`` (R4, comment part). When a
  task gives up and it was filed by another task's worker, the creator task
  gets a comment naming the task and its error.

``HERMES_KANBAN_GUARDS=0`` turns all three off.
"""
from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV = "HERMES_KANBAN_GUARDS"
COMMENT_AUTHOR = "kanban-dispatcher"
FIX_HINT = "Pass workspace_kind='scratch', or a project or path in a git repo this container can write to."
# git's stderr when the repo can't be written; these never heal between ticks.
_FINAL_GIT_ERRORS = ("Read-only file system", "Permission denied", "Operation not permitted")
_FINAL_ERRNOS = {errno.EROFS, errno.EACCES, errno.EPERM}


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip() != "0"


def _refuse(message: str) -> None:
    raise ValueError(f"{message} {FIX_HINT}")


def _absolute(path: str, label: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        _refuse(f"{label} {path!r} is not absolute.")
    return p


def _require_writable(path: Path, label: str) -> None:
    from hermes_cli import kanban_db_workspace as _kbw

    probe = _kbw._nearest_existing_path(path)
    if not os.access(probe, os.W_OK):
        _refuse(f"{label} {str(path)!r} is not writable here ({str(probe)!r} is read-only to this container).")


def check_filing_workspace(
    *, workspace_kind: Optional[str], workspace_path: Optional[str],
    project_repo: Optional[str], board: Optional[str],
) -> None:
    """Raise ValueError when the dispatcher could not build this task's workspace."""
    if not enabled():
        return
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_workspace as _kbw

    kind = workspace_kind or "scratch"
    if kind == "scratch":
        if workspace_path:
            _absolute(workspace_path, "scratch workspace_path")
        return

    board_slug = board if board else _kb.get_current_board()
    board_default = None
    if not workspace_path and not project_repo:
        board_default = (_kb.read_board_metadata(board_slug).get("default_workdir") or "").strip() or None

    if kind == "dir":
        path = workspace_path or board_default
        if not path:
            _refuse(f"workspace_kind='dir' needs a workspace_path, and board {board_slug!r} has no default_workdir.")
        _require_writable(_absolute(path, "dir workspace_path"), "dir workspace_path")
        return

    if kind != "worktree":
        return  # create_task already refused unknown kinds
    if project_repo and not workspace_path:
        path, label = project_repo, "project repo"
    elif workspace_path:
        path, label = workspace_path, "worktree workspace_path"
    elif board_default:
        path, label = board_default, f"board {board_slug!r} default_workdir"
    else:
        _refuse(
            f"workspace_kind='worktree' needs a workspace_path or a project, "
            f"and board {board_slug!r} has no default_workdir."
        )
    requested = _absolute(path, label)
    repo = _kbw._repo_root_for_worktree_target(requested)
    if repo is None:
        _refuse(f"{label} {path!r} is not inside a git repo.")
    # git worktree add writes the new checkout (under the repo, or at the path
    # itself) and its admin files under the repo's common git dir.
    target = repo / ".worktrees" if requested.resolve(strict=False) == repo else requested
    _require_writable(target, label)
    common = _kbw._git_common_dir(repo)
    if common is not None:
        _require_writable(common, f"git dir of {label}")


def workspace_failure_is_final(exc: BaseException) -> bool:
    """True when a spawn-time workspace error will fail the same way on every tick."""
    if not enabled():
        return False
    if isinstance(exc, ValueError):
        return True  # missing path, no default_workdir, not absolute, not in a repo
    if isinstance(exc, OSError) and exc.errno in _FINAL_ERRNOS:
        return True  # mkdir on a read-only mount or a directory we can't write
    if isinstance(exc, RuntimeError) and str(exc).startswith("git worktree add failed"):
        return any(marker in str(exc) for marker in _FINAL_GIT_ERRORS)
    return False


def _creator_task_id(conn: Any, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id LIMIT 1", (task_id,),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        creator = json.loads(row["payload"]).get("creator_task_id")
    except (TypeError, ValueError):
        return None
    return creator if isinstance(creator, str) and creator and creator != task_id else None


def comment_on_creator(conn: Any, task_id: str, error: str) -> Optional[int]:
    """Comment on the task whose worker filed ``task_id``; returns the comment id."""
    from hermes_cli import kanban_db as _kb

    creator = _creator_task_id(conn, task_id)
    if creator is None:
        return None
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (creator,)).fetchone()
    if row is None or row["status"] == "archived":
        return None
    title_row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
    title = title_row["title"] if title_row else ""
    body = (
        f"Task {task_id} ({title!r}), which this task filed, gave up and is blocked.\n"
        f"Error: {error}\n"
        "Fix the cause and run kanban_unblock on it, or file a replacement."
    )
    return _kb.add_comment(conn, creator, COMMENT_AUTHOR, body)


def install_dispatch(module: Any) -> None:
    """Wrap ``module._record_task_failure`` so a give-up comments on the creator task."""
    original = module._record_task_failure
    if getattr(original, "_assistant_kanban_guards", False):
        return

    def _record_task_failure(conn, task_id, error, *args, **kwargs):
        tripped = original(conn, task_id, error, *args, **kwargs)
        if tripped and enabled():
            try:
                comment_on_creator(conn, task_id, str(error)[:500])
            except Exception:
                logger.exception("kanban guards: creator comment for %s failed", task_id)
        return tripped

    _record_task_failure.__doc__ = original.__doc__
    _record_task_failure._assistant_kanban_guards = True  # type: ignore[attr-defined]
    module._record_task_failure = _record_task_failure
