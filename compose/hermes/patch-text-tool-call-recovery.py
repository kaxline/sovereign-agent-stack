#!/usr/bin/env python3
"""Install text-mimicked tool-call recovery into Hermes conversation_loop.

Compatibility overlay (not for Hermes upstream — see #29115 / #35129).
Copies the helper module into /opt/hermes/agent/ and inserts a call site
immediately before the structured tool_calls dispatch check.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HELPER_SRC = Path("/bootstrap/text_tool_call_recovery.py")
HELPER_DST = Path("/opt/hermes/agent/text_tool_call_recovery.py")
TARGET = Path("/opt/hermes/agent/conversation_loop.py")
MARKER = "# assistant-stack: text-mimicked tool-call recovery"

OLD = """            # Check for tool calls
            if assistant_message.tool_calls:
"""

NEW = """            # assistant-stack: text-mimicked tool-call recovery
            # Weak models (e.g. Llama 3.3 via OpenRouter) often emit
            # search_files(...) or <function/name=...> as plain text under
            # Hermes' large WebUI prompt. Promote the first valid mimic into
            # structured tool_calls so the normal dispatch path runs.
            # Also reads _current_streamed_assistant_text when content is empty
            # so streamed XML mimic is not classified as an empty response.
            if not getattr(assistant_message, "tool_calls", None):
                try:
                    from agent.text_tool_call_recovery import (
                        try_recover_assistant_tool_calls,
                    )

                    try_recover_assistant_tool_calls(agent, assistant_message)
                except Exception:
                    logger.debug(
                        "text-mimicked tool-call recovery failed",
                        exc_info=True,
                    )

            # Check for tool calls
            if assistant_message.tool_calls:
"""


def main() -> None:
    if not HELPER_SRC.is_file():
        raise SystemExit(f"[patch-text-tool-call-recovery] missing {HELPER_SRC}")
    HELPER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SRC, HELPER_DST)
    print(f"[patch-text-tool-call-recovery] installed {HELPER_DST}")

    text = TARGET.read_text()
    if MARKER in text:
        print(f"[patch-text-tool-call-recovery] already applied ({TARGET})")
        return
    if OLD not in text:
        raise SystemExit(
            f"[patch-text-tool-call-recovery] expected tool_calls block missing in {TARGET}; "
            "Hermes image may have changed — update this patch."
        )
    TARGET.write_text(text.replace(OLD, NEW, 1))
    print(f"[patch-text-tool-call-recovery] patched {TARGET}")


if __name__ == "__main__":
    main()
