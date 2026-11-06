"""Give the compaction summarizer the tool results it is summarizing.

Compatibility overlay (not upstream). ``ContextCompressor.compress()`` and the
prepared-compaction pass both call ``_plan_compaction_window``, which prunes
old tool results to one-line stubs such as ``[web_url_read] (11,104 chars
result)`` and returns that pruned copy. The summarizer is then fed the
pruned copy, so it sees only tool names and result sizes. A research turn
that read a dozen pages comes out of compaction knowing none of what the
pages said, and the agent re-reads them.

``install(ContextCompressor)`` wraps three methods:

* ``_plan_compaction_window`` records the full body of each tool result in
  the incoming (unpruned) transcript, keyed by ``tool_call_id``, on the
  compressor as ``_summary_tool_bodies``. A new dict is bound on every
  call, so a prepared-compaction worker (a ``copy.copy`` of the compressor)
  keeps the dict that matches its own window.
* ``_serialize_records_for_summary`` swaps each stub back for its recorded
  body before serializing, newest first, within ``RESTORE_BUDGET_CHARS`` in
  total and ``RESTORE_PER_RESULT_CHARS`` per result. Results that stay
  stubs keep the upstream one-line form. Only summarizer input changes; the
  pruned transcript that goes back into the session is untouched.
* ``_summary_template_sections`` adds a ``## Findings`` section before
  ``## Active State`` asking for each fact with the URL, path or command it
  came from, and for each source that failed, so the next context window
  neither loses the facts nor fetches the failed sources again.

The budgets keep the summary prompt near 15K tokens. The compression
summarizer runs on the local model and must emit its first token within the
auxiliary stream's 60 s no-progress window, which bounds prefill size.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

logger = logging.getLogger("agent.context_compressor")

INSTALLED_ATTR = "_assistant_tool_facts_installed"
BODIES_ATTR = "_summary_tool_bodies"

RESTORE_BUDGET_CHARS = 60_000
RESTORE_PER_RESULT_CHARS = 4_000
_HEAD_SHARE = 0.7

FINDINGS_HEADING = "## Findings"
FINDINGS_SECTION = f"""{FINDINGS_HEADING}
[Facts the tool results established, one per line, as "- fact — source", where source is the
exact URL, file path or command the fact came from. Copy names, dates, numbers, addresses and
identifiers verbatim. Keep facts from earlier summaries. Then list each source that failed or held
nothing relevant as "- FAILED source — error or reason", so it is not fetched again. Write "None"
only if no tool result held a usable fact.]

"""
_ANCHOR = "## Active State"


def _module_of(cls: type) -> Any:
    return sys.modules[cls.__module__]


# What _summarize_tool_result writes: "[tool] args... (4,545 chars result)", sometimes followed by a
# failure suffix. Upstream _is_summary_stub tests for " chars)" and misses this form (v0.22.7).
_STUB_RE = re.compile(r"\[[^\]\n]{1,160}\][^\n]*\(\d[\d,]* chars(?: result)?\)")
_STUB_MAX_CHARS = 600


def _is_stub(module: Any, content: Any) -> bool:
    if not isinstance(content, str) or not content:
        return False
    if content == getattr(module, "_PRUNED_TOOL_PLACEHOLDER", None):
        return True
    if len(content) < _STUB_MAX_CHARS and _STUB_RE.match(content.lstrip()):
        return True
    is_summary_stub = getattr(module, "_is_summary_stub", None)
    try:
        return bool(is_summary_stub and is_summary_stub(content))
    except Exception:
        return False


def record_bodies(module: Any, messages: Any) -> dict[str, str]:
    """``{tool_call_id: body}`` for every full (non-stub) tool result; the longest wins on repeats."""
    bodies: dict[str, str] = {}
    for msg in messages or ():
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        call_id = str(msg.get("tool_call_id") or "")
        content = msg.get("content")
        if not call_id or not isinstance(content, str) or not content:
            continue
        if content.startswith("[Duplicate tool output") or _is_stub(module, content):
            continue
        if len(content) > len(bodies.get(call_id, "")):
            bodies[call_id] = content
    return bodies


def _bounded(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    marker = f"\n...[{len(body) - limit:,} chars omitted]...\n"
    room = max(limit - len(marker), 0)
    head = int(room * _HEAD_SHARE)
    return body[:head].rstrip() + marker + body[len(body) - (room - head):].lstrip()


def restore_turns(
    module: Any, turns: list, bodies: dict[str, str],
    budget: int = RESTORE_BUDGET_CHARS, per_result: int = RESTORE_PER_RESULT_CHARS,
) -> tuple[list, int]:
    """Copy of ``turns`` with stubbed tool results swapped for their bodies, newest first.

    Returns ``(turns, restored_count)``. Input dicts are never mutated."""
    if not bodies or not turns:
        return turns, 0
    out = list(turns)
    remaining = budget
    restored = 0
    for idx in range(len(out) - 1, -1, -1):
        if remaining <= 0:
            break
        msg = out[idx]
        if not isinstance(msg, dict) or msg.get("role") != "tool" or not _is_stub(module, msg.get("content")):
            continue
        body = bodies.get(str(msg.get("tool_call_id") or ""))
        if not body:
            continue
        text = _bounded(body, min(per_result, remaining))
        out[idx] = {**msg, "content": text}
        remaining -= len(text)
        restored += 1
    return out, restored


def add_findings_section(sections: str) -> str:
    if FINDINGS_HEADING in sections or _ANCHOR not in sections:
        return sections
    return sections.replace(_ANCHOR, FINDINGS_SECTION + _ANCHOR, 1)


def install(cls: type) -> None:
    if getattr(cls, INSTALLED_ATTR, False):
        return
    module = _module_of(cls)
    plan = cls._plan_compaction_window
    serialize = cls._serialize_records_for_summary
    template = next(
        k.__dict__["_summary_template_sections"] for k in cls.__mro__ if "_summary_template_sections" in k.__dict__
    ).__func__

    def _plan_compaction_window(self, messages):
        try:
            setattr(self, BODIES_ATTR, record_bodies(module, messages))
        except Exception:
            logger.debug("tool facts: recording bodies failed", exc_info=True)
            setattr(self, BODIES_ATTR, {})
        return plan(self, messages)

    def _serialize_records_for_summary(self, turns):
        bodies = getattr(self, BODIES_ATTR, None)
        if bodies:
            try:
                turns, restored = restore_turns(module, turns, bodies)
                if restored and not getattr(self, "quiet_mode", False):
                    logger.info("Compression summary input: restored %d tool result(s)", restored)
            except Exception:
                logger.debug("tool facts: restoring bodies failed", exc_info=True)
        return serialize(self, turns)

    def _summary_template_sections(klass, *args, **kwargs):
        return add_findings_section(template(klass, *args, **kwargs))

    _plan_compaction_window.__wrapped__ = plan
    _serialize_records_for_summary.__wrapped__ = serialize
    _summary_template_sections.__wrapped__ = template
    cls._plan_compaction_window = _plan_compaction_window
    cls._serialize_records_for_summary = _serialize_records_for_summary
    cls._summary_template_sections = classmethod(_summary_template_sections)
    setattr(cls, INSTALLED_ATTR, True)
