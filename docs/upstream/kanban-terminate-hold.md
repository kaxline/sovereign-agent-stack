# Upstream proposal: terminate a kanban run without re-queuing its task

**Status: draft. Not filed.** Nothing here has been posted to GitHub. Filing the issue needs explicit approval in chat.

The stack works around this with `compose/hermes/kanban_hold.py` and `patch-kanban-hold.py` (Boundary CR 0005 R2). Once upstream offers it, delete both, their mount and runner lines and `scripts/test-kanban-hold.py`, and tell Boundary the field name if it differs.

## Before filing

Checked on 2026-11-22 against `nousresearch/hermes-agent:v0.23.2` only, not upstream `main`. Re-check the line numbers. A search of upstream issues for "kanban terminate hold blocked" found nothing.

## Issue text

**Title:** Kanban: terminating a run always re-queues the task; no way to stop it and keep it stopped

`POST /runs/{run_id}/terminate` (`plugins/kanban/dashboard/plugin_api.py:983`) calls `reclaim_task` (`hermes_cli/kanban_db.py:2475`), which stops the worker and sets the task to its retry status, `ready` or `review`. The next dispatcher tick starts it again. An operator who wants a runaway task stopped has to follow with `PATCH /tasks/{id}` `status: blocked`, and a tick between the two calls can claim the task and spawn a new worker. Blocking first does not work either: `block_task` clears `worker_pid` without stopping the worker, and the terminate that follows is refused.

Proposed: a `hold` flag on the terminate body (and `--hold` on `hermes kanban reclaim`). With it, stop the worker while the task still holds its claim (the dispatcher only claims `ready`/`review` tasks), then in one transaction set the task `blocked`, end the run with outcome `blocked` and append a `blocked` event, so `_has_sticky_block` keeps it parked until an unblock. Mark the stop in the run metadata and the event (the stack uses `operator_stop: true` plus the reason) and carry `source_status` so an unblock returns a review run to `review`. Check the claim lock in that transaction so a crash detector that got there first wins.

The stack's version is about 50 lines in a module plus two anchors in `plugin_api.py`, and is happy to become a PR.
