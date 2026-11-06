"""Point the model at the browser when searxng's page reader is refused.

Compatibility overlay (not upstream). ``web_url_read`` on the searxng MCP
server is a plain HTTP fetch from the mcp-searxng container: no JavaScript,
no browser fingerprint. Sites with bot protection answer it with 403 (or 429,
or a 503 challenge), and pages built by JavaScript convert to nothing. The
server reports these as fixed strings (mcp-searxng ``error-handler.js``):

* ``Website Error (403): Access blocked (bot detection or geo-restriction)``
* ``Website Error (429): Rate limit exceeded``
* ``Website Error (503): Internal server error``
* ``Content Warning: Page fetched but appears empty after conversion (...)``

On 2026-11-05 a research turn met these on the Utah business registry, Yelp
and Google Maps, then kept retrying web_url_read and searching for other
copies of the same pages. Each refusal also counts toward searxng's circuit
breaker, which pauses the whole server after 3 failures in a row.

``queue_hint`` recognizes those results, and pages that came back as a
Cloudflare-style challenge, and queues a note on the agent's live-notes
queue. tool_executor.py appends that queue to the tool message after the
``<untrusted_tool_result>`` wrapper, so the model reads the note as stack
guidance rather than as text from the fetched page. The note names the URL to
open with ``browser_exec`` and says not to try to get past a CAPTCHA or
"verify you are human" check. Notes are keyed by host, so several refusals
from one site in a tool batch give one note.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

TOOL_NAMES = frozenset({"mcp__searxng__web_url_read", "mcp_searxng_web_url_read"})
NOTE_KEY_PREFIX = "blocked-fetch:"

_REFUSED_RE = re.compile(r"Website Error \((403|429|503)\)")
_EMPTY_MARK = "appears empty after conversion"
# Text a bot-challenge interstitial converts to; matched only on short results.
_CHALLENGE_MARKS = (
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "verify you are human",
)
_CHALLENGE_MAX_CHARS = 4000

_REASONS = {
    "403": "refused it (HTTP 403, most likely bot detection)",
    "429": "refused it (HTTP 429, rate limited)",
    "503": "answered HTTP 503, which bot-protected sites also use for challenge pages",
}

_OPEN = (
    "If you have browser_exec, open the page there: new_tab('{url}'), wait_for_load(), then "
    "print(page_info()) to read it. Don't retry web_url_read on this site or search for another copy "
    "of the same page."
)
_CAPTCHA = (
    "If the browser shows a CAPTCHA or a \"verify you are human\" check, don't try to get past it: "
    "note the source as unavailable and continue with what you have."
)


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)
    if isinstance(result, list):
        return "\n".join(
            str(part.get("text", "")) for part in result if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _decoded(text: str) -> str:
    """The error or result string inside a ``{"error": ...}`` / ``{"result": ...}`` envelope, else ``text``."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(payload, dict):
        for key in ("error", "result"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return text


def _url_from(args: Any) -> Optional[str]:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return None
    url = args.get("url") if isinstance(args, dict) else None
    if not isinstance(url, str):
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c in url for c in "'\\\n\r"):
        return None
    return url


def hint_for(function_name: str, function_args: Any, function_result: Any) -> Optional[tuple[str, str]]:
    """``(host, note)`` when this call is a refused or empty searxng page read, else None."""
    if function_name not in TOOL_NAMES:
        return None
    url = _url_from(function_args)
    if url is None:
        return None
    host = urlsplit(url).hostname or ""
    text = _decoded(_result_text(function_result))
    refused = _REFUSED_RE.search(text)
    if refused:
        why = f"{host} {_REASONS[refused.group(1)]}."
    elif _EMPTY_MARK in text:
        why = f"{host} gave web_url_read no readable text; the page most likely builds its content with JavaScript."
    elif len(text) <= _CHALLENGE_MAX_CHARS and any(mark in text.lower() for mark in _CHALLENGE_MARKS):
        why = f"{host} returned a bot-check page instead of its content."
    else:
        return None
    note = (
        f"[Stack note] web_url_read cannot read {url}: {why} It is a plain fetch with no JavaScript, "
        f"so it will keep failing here. {_OPEN.format(url=url)} {_CAPTCHA}"
    )
    return host, note


def queue_hint(agent: Any, function_name: str, function_args: Any, function_result: Any, tool_call_id: str) -> bool:
    """Queue the note on ``agent``'s live notes; True when one was queued."""
    found = hint_for(function_name, function_args, function_result)
    if found is None:
        return False
    host, note = found
    from agent.plugin_live_notes import queue_plugin_notice

    queue_plugin_notice(agent, NOTE_KEY_PREFIX + host, note)
    return True
