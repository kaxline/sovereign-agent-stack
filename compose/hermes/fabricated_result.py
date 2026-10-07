"""Detect turn-ending assistant text that must not stand as the answer.

Compatibility overlay for this stack. Some models close the loop in prose
(``<action>`` / ``<result>``, "file written successfully") and stop. Others
give up on a login and ask the user for their password in chat. The
conversation loop uses this to re-prompt instead of persisting the reply.

This module is stdlib-only so host tests can import it without Hermes.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

PLACEHOLDER_CONTENT = "No tool was called."

FABRICATED_RESULT_NUDGE = (
    "No tool was called. Your previous message was not executed and no file "
    "was written. Call the tool now. Do not claim a result until a tool returns one."
)

SECRET_PLACEHOLDER = "Asked for a password in chat."

SECRET_REQUEST_NUDGE = (
    "Do not ask the user for a password; they must never type one in chat. "
    "Use the vault: browser_vault_list for a saved login, browser_vault_fill "
    "with its handle on the site's sign-in form, or browser_vault_save_login "
    "on that form when none is saved. If the vault cannot be used, say what "
    "failed and stop."
)

# Every nudge this module can add, for callers that filter them out.
NUDGES = (FABRICATED_RESULT_NUDGE, SECRET_REQUEST_NUDGE)

_SECRET_WORD = r"(?:password|passphrase|passcode|login credentials|credentials)"

_SECRET_REQUEST_RES = (
    # "can you tell me your LinkedIn password?"
    re.compile(
        r"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?"
        r"(?:tell|give|send|share|provide|paste|type|enter|confirm)\b"
        r"[^.?!\n]{0,60}?\b" + _SECRET_WORD + r"\b",
        re.IGNORECASE,
    ),
    # "what's your password", "what is the password for…"
    re.compile(
        r"\bwhat(?:['’]s| is)\s+(?:your|the)\b[^.?!\n]{0,40}?\b" + _SECRET_WORD + r"\b",
        re.IGNORECASE,
    ),
    # "please send me your password", "share your password with me"
    re.compile(
        r"\b(?:send|share|provide|paste|tell|give)\s+(?:me\s+)?your\b"
        r"[^.?!\n]{0,40}?\b" + _SECRET_WORD + r"\b",
        re.IGNORECASE,
    ),
    # "I need your password", "I'll need the password"
    re.compile(
        r"\bi(?:['’]ll| will)?\s+need\s+(?:your|the)\b[^.?!\n]{0,40}?\b" + _SECRET_WORD + r"\b",
        re.IGNORECASE,
    ),
)

_NEGATION_RE = re.compile(r"\b(?:never|not|don['’]t|no need|without)\b", re.IGNORECASE)

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


def asks_for_secret(text: str) -> bool:
    """True when ``text`` asks the user to give a password in chat."""
    if not isinstance(text, str) or not text.strip():
        return False
    for pattern in _SECRET_REQUEST_RES:
        for match in pattern.finditer(text):
            sentence_start = max(
                text.rfind(".", 0, match.start()),
                text.rfind("!", 0, match.start()),
                text.rfind("?", 0, match.start()),
                text.rfind("\n", 0, match.start()),
            ) + 1
            if _NEGATION_RE.search(text[sentence_start:match.end()]):
                continue
            return True
    return False


def turn_end_nudge(text: str, *, has_tool_calls: bool) -> Optional[Tuple[str, str]]:
    """``(placeholder, nudge)`` when a stop turn must be re-prompted, else None."""
    if has_tool_calls:
        return None
    if claims_unexecuted_result(text, has_tool_calls=False):
        return PLACEHOLDER_CONTENT, FABRICATED_RESULT_NUDGE
    if asks_for_secret(text):
        return SECRET_PLACEHOLDER, SECRET_REQUEST_NUDGE
    return None
