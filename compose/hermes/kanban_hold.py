"""Stop a kanban run and hold its task (Boundary CR 0005 R2).

Stack overlay, installed as hermes_cli/kanban_hold.py by patch-kanban-hold.py.
``POST /runs/{run_id}/terminate`` with ``hold: true`` calls :func:`stop_and_hold`
instead of ``reclaim_task``.

Upstream's terminate is ``reclaim_task``: it stops the worker and returns the task
to ``ready`` (or ``review``), so a client that wants the task to stay stopped has
to send a block afterwards, and a dispatcher tick between the two calls can claim
the task again. Here one transaction first moves the task from ``running`` to
``blocked``, ends the run and writes a ``blocked`` event; the worker is stopped
after it commits. The dispatcher only claims ``ready``/``review`` tasks and its
crash and stale sweeps only look at ``running`` ones, so no tick can act on the
task in between. The ``blocked`` event makes the block sticky
(``_has_sticky_block``), so it lasts until an operator unblocks the task.

The run ends with outcome ``blocked`` (upstream's run-outcome map knows it), and
``operator_stop: true`` with the reason in the run's metadata and in the event
tells an operator stop from a worker's own block, a crash or a reclaim. The
termination report is added to the run's metadata once the worker is stopped.
The event carries ``source_status`` so an unblock sends a held review run back
to ``review``. ``_end_run`` keeps the worker's pid and fingerprint on the closed
run row, so a worker that survives the signals, or writes after the block, is
refused by the board and ended by ``reap_terminal_workers``, as after
``block_task``.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from hermes_cli import kanban_db as kb


def stop_and_hold(
    conn: sqlite3.Connection, task_id: str, *, run_id: int, reason: Optional[str] = None, signal_fn=None,
) -> bool:
    """Leave *task_id* sticky ``blocked``, then stop *run_id*'s worker.

    False, with nothing changed, when the task is not running that run.
    """
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["status"] != "running" or row["current_run_id"] != int(run_id):
        return False
    lock = row["claim_lock"]
    stop = {"operator_stop": True, "reason": reason}
    with kb.write_txn(conn):
        source_status = kb._retry_status_for_run(conn, task_id, int(run_id))
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, block_kind = NULL "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ? AND current_run_id = ?",
            (task_id, lock, int(run_id)),
        )
        if cur.rowcount != 1:
            return False
        ended = kb._end_run(conn, task_id, outcome="blocked", status="blocked", summary=reason, metadata=stop)
        kb._append_event(conn, task_id, "blocked", {**stop, "kind": None, "source_status": source_status},
                         run_id=ended)
        task = kb.get_task(conn, task_id)
    termination = kb._terminate_reclaimed_worker(
        row["worker_pid"], lock, signal_fn=signal_fn, started_at=row["worker_started_at"])
    if ended is not None:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                         (json.dumps({**stop, **termination}), ended))
    kb._fire_task_hook("kanban_task_blocked", task, task_id, ended, reason=reason)
    return True
