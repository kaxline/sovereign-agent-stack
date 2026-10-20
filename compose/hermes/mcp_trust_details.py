"""What an MCP trust-gate approval says about the call it gates.

Installed into the agent package by patch-mcp-trust-details.py. Hermes asks
before a write-capable tool on a ``trust: untrusted`` server runs, but the
approval carried only the tool and server names, so a card could not show
what was about to change. ``approval_details`` returns:

- a one-line argument summary for the prompt text. Approvals with identical
  text are coalesced, so without it two different calls to one tool shared a
  prompt;
- structured fields for the approval entry, under ``mcp``: ``server``,
  ``tool``, ``arguments`` and ``tool_call_id`` (the id ``tool.start`` carried
  for the same call). ``approval.pending`` replays the entry as stored, so
  arguments are redacted here, before they are stored.

Redaction: values under a credential-looking key are replaced, every string
goes through Hermes' ``redact_sensitive_text(force=True)`` (the same pass as
command previews), and strings, containers and depth are capped.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

STRING_CAP = 500
ITEMS_CAP = 50
DEPTH_CAP = 4
SUMMARY_CAP = 400
REDACTED = "[REDACTED]"

# Substrings that mark a credential wherever they appear, and short names that do
# only on their own (``auth``, not ``author``).
_SECRET_KEY = re.compile(r"password|passwd|passphrase|secret|token|api[-_]?key|authorization|credential|cookie"
                         r"|private[-_]?key|^(auth|pass|pwd|pin)$", re.I)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact_arguments(value: Any, redact: Callable[[str], str], depth: int = 0) -> Any:
    """A JSON-safe copy of ``value`` with secrets masked and sizes capped."""
    if isinstance(value, str):
        return _cap(redact(value), STRING_CAP)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= DEPTH_CAP:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= ITEMS_CAP:
                out["…"] = f"{len(value) - ITEMS_CAP} more"
                break
            name = _cap(str(key), 100)
            out[name] = REDACTED if _SECRET_KEY.search(name) else redact_arguments(item, redact, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [redact_arguments(item, redact, depth + 1) for item in list(value)[:ITEMS_CAP]]
        if len(value) > ITEMS_CAP:
            items.append(f"… {len(value) - ITEMS_CAP} more")
        return items
    return _cap(redact(str(value)), STRING_CAP)


def approval_details(server: str, tool: str, args: Any, tool_call_id: str = "",
                     redact: Callable[[str], str] | None = None) -> tuple[str, dict]:
    """``(summary, fields)`` for one gated call. ``fields`` merges into the approval entry."""
    if redact is None:
        from agent.redact import redact_sensitive_text

        def redact(text: str) -> str:
            return redact_sensitive_text(text, force=True)
    arguments = redact_arguments(args if isinstance(args, dict) else {}, redact)
    summary = _cap(json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str), SUMMARY_CAP)
    fields = {
        "mcp": {"server": server, "tool": tool, "arguments": arguments, "tool_call_id": tool_call_id or None},
        # The trust gate accepts "session" and "always" as a single approval and remembers
        # neither, so offer only what it honours.
        "allow_session": False,
        "allow_permanent": False,
    }
    return summary, fields
