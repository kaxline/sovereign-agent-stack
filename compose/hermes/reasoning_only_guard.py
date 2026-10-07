"""Detect tool-call markup in a reasoning-only reply.

Compatibility overlay for this stack. Hermes v2026.9+ promotes a reply that
has reasoning but no content and no tool_calls (finish_reason "stop") to the
final answer. Some local models (qwen3.6 via llm-proxy) write a malformed
tool call inside their reasoning instead, so the turn ends on the model's
planning text. When the reasoning carries tool-call markup, the conversation
loop skips the promotion and lets the empty-response ladder re-prompt.

This module is stdlib-only so host tests can import it without Hermes.
"""

from __future__ import annotations

import re

# Skip the promotion at most this many times per turn, then let upstream
# promote so a model that keeps doing it can still end the turn.
MAX_HOLDS = 2

# Hermes/Qwen XML tool-call shape: <tool_call>, <function=name>, <parameter=x>
# and their closing tags. Partial markup counts: the opening tags are often
# missing from the reasoning text.
_TOOL_MARKUP_RE = re.compile(
    r"<\s*/?\s*tool_call\s*>"
    r"|<\s*function\s*="
    r"|<\s*/\s*function\s*>"
    r"|<\s*parameter\s*="
    r"|<\s*/\s*parameter\s*>",
    re.IGNORECASE,
)


def has_tool_call_markup(text: str) -> bool:
    """True when ``text`` contains Hermes/Qwen XML tool-call tags."""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_TOOL_MARKUP_RE.search(text))


def should_hold_promotion(reasoning: str, *, holds: int) -> bool:
    """True when a reasoning-only stop should be re-prompted, not promoted."""
    return holds < MAX_HOLDS and has_tool_call_markup(reasoning)
