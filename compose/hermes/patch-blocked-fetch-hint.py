#!/usr/bin/env python3
"""Point the model at the browser when searxng's web_url_read is refused.

Compatibility overlay (not upstream). Copies blocked_fetch_hint.py into the
agent package and calls it from tool_executor.py's _commit_tool_result just
before the live-notes queue is attached to the tool message, so the note sits
after the <untrusted_tool_result> wrapper. See blocked_fetch_hint.py.

Run scripts/test-blocked-fetch-hint.py on Hermes bumps.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, edit, install

AGENT = ROOT / "agent"
TAG = "[patch-blocked-fetch-hint]"
MARKER = "# assistant-stack: blocked fetch hint"

ANCHOR = (
    "    lsp_deferred.forward_to_agent(agent)\n"
    "    plugin_live_notes.attach_to_tool_message(agent, tool_message)\n"
)
INSERT = (
    f"    {MARKER}\n"
    "    if not blocked:\n"
    "        try:\n"
    "            from agent.blocked_fetch_hint import queue_hint as _queue_blocked_fetch_hint\n"
    "            _queue_blocked_fetch_hint(agent, function_name, function_args, function_result, tool_call_id)\n"
    "        except Exception:\n"
    "            logger.debug(\"blocked fetch hint failed\", exc_info=True)\n"
)
# Names the inserted block reads from _commit_tool_result's scope.
NEEDED = (
    "def _commit_tool_result(",
    "function_name, function_args, tool_call_id, effective_task_id = ref.name, ref.args, ref.call_id, ref.task_id",
    "    blocked: bool,\n",
    "\nlogger = logging.getLogger(__name__)\n",
)


def main() -> None:
    install(BOOTSTRAP / "blocked_fetch_hint.py", AGENT / "blocked_fetch_hint.py", TAG)
    edit(AGENT / "tool_executor.py", [(ANCHOR, INSERT + ANCHOR)], MARKER, TAG, requires=NEEDED)


if __name__ == "__main__":
    main()
