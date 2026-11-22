# Upstream proposal: the filed task id in session history

**Status: draft. Not filed.** Nothing here has been posted to GitHub. Filing the issue needs explicit approval in chat.

The stack works around this with `compose/hermes/history_task_ids.py` and `patch-history-task-ids.py` (Boundary CR 0005 R3). Once upstream offers it, delete both, their mount and `run_patch` lines and `scripts/test-history-task-ids.py`, and tell Boundary the field name if it differs.

## Before filing

Checked on 2026-11-22 against `nousresearch/hermes-agent:v0.23.2` only, not upstream `main`. Re-check the line numbers. A search of upstream issues for "session history tool result task_id" found nothing.

## Issue text

**Title:** session.history drops kanban_create's task id, so clients can't link a reloaded chat to the tasks it filed

`_history_to_messages` (`tui_gateway/session_history.py:297`) keeps a tool row's name, arguments and a short context, and its result only for `write_file`, `patch`, `skill_manage` and `clarify`. `kanban_create` answers `{"ok": true, "task_id": …}` (`tools/kanban_tools.py`, `_ok`), and that id is the one link between a conversation and the task it filed. A client that renders filed tasks as live cards has it while the turn streams and loses it on reload; matching by title and assignee instead swaps cards when one turn files two tasks with the same title.

Proposed: add `"result": {"task_id": …}` to `kanban_create` rows when the call succeeded, or more generally let a tool declare a small structured result the projection keeps. The stack reads the first JSON object in the stored result (it may sit inside the `<untrusted_tool_result>` wrapper) and keeps only an id of the form `t_<hex>` when `ok` is true.
