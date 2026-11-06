#!/usr/bin/env python3
"""Point the model at the browser when searxng's web_url_read is refused.

Compatibility overlay (not upstream). Copies blocked_fetch_hint.py into the
agent package and calls it from tool_executor.py's _commit_tool_result just
before the live-notes queue is attached to the tool message, so the note sits
after the <untrusted_tool_result> wrapper. See blocked_fetch_hint.py.

Run scripts/test-blocked-fetch-hint.py on Hermes bumps.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
BOOTSTRAP = Path(os.environ.get("BLOCKED_FETCH_HINT_BOOTSTRAP", "/bootstrap"))
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
    src = BOOTSTRAP / "blocked_fetch_hint.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, AGENT / "blocked_fetch_hint.py")
    print(f"{TAG} installed {AGENT / 'blocked_fetch_hint.py'}")

    executor = AGENT / "tool_executor.py"
    text = executor.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TAG} already applied")
        return
    missing = [n.strip() for n in NEEDED if n not in text]
    if missing or text.count(ANCHOR) != 1:
        raise SystemExit(
            f"{TAG} expected code missing in {executor}: {missing or ['live-notes attach anchor']}. "
            "Hermes image may have changed — update this patch."
        )
    executor.write_text(text.replace(ANCHOR, INSERT + ANCHOR, 1), encoding="utf-8")
    print(f"{TAG} patched {executor}")


if __name__ == "__main__":
    main()
