"""Detect assistant text that claims a tool result without a tool call.

Compatibility overlay for this stack. Some models close the loop in prose
(``<action>`` / ``<result>``, "file written successfully") and stop. The
conversation loop uses this to re-prompt instead of persisting the claim.

This module is stdlib-only so host tests can import it without Hermes.
"""

from __future__ import annotations

import re

PLACEHOLDER_CONTENT = "No tool was called."

FABRICATED_RESULT_NUDGE = (
    "No tool was called. Your previous message was not executed and no file "
    "was written. Call the tool now. Do not claim a result until a tool returns one."
)

_RESULT_TAG_RE = re.compile(r"<\s*result\b", re.IGNORECASE)

_ACTION_BLOCK_RE = re.compile(
    r"<\s*action\b[^>]*>(.*?)<\s*/\s*action\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ACTION_VERB_RE = re.compile(r"\b(?:write|create|save|patch)\b", re.IGNORECASE)

_SUCCESS_PHRASE_RE = re.compile(
    r"\b(?:file written successfully|wrote the file successfully|"
    r"created the file successfully|saved the file successfully)\b",
    re.IGNORECASE,
)

# Past-tense mutation plus a path within a short window. Future tense
# ("I'll create") is left to intent-ack continuation.
_PAST_MUTATION_RE = re.compile(
    r"\b(?:i['’]ve|i have|i)\s+(?:created|written|wrote|saved|updated)\b"
    r".{0,160}?"
    r"(?:(?:~|/|\./)[\w./~-]+|\b[\w.-]+\.(?:txt|md|py|json|ya?ml|toml)\b)",
    re.IGNORECASE | re.DOTALL,
)


def claims_unexecuted_result(text: str, *, has_tool_calls: bool) -> bool:
    """True when ``text`` asserts a completed mutation and no tool ran."""
    if has_tool_calls:
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    if _RESULT_TAG_RE.search(text):
        return True
    if _SUCCESS_PHRASE_RE.search(text):
        return True
    for match in _ACTION_BLOCK_RE.finditer(text):
        if _ACTION_VERB_RE.search(match.group(1) or ""):
            return True
    if _PAST_MUTATION_RE.search(text):
        return True
    return False
