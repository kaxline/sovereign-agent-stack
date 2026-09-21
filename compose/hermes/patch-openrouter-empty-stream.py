#!/usr/bin/env python3
"""Bust OpenRouter response cache on EmptyStreamError retries.

When OpenRouter returns HTTP 200 with an empty SSE body and
X-OpenRouter-Cache-Status: HIT, Hermes' stream retries replay the same
cached empty body. Inject X-OpenRouter-Cache-Clear on the next attempt and
surface a clearer exhausted-stream error for OpenRouter routes.
"""
from __future__ import annotations

from pathlib import Path

TARGET_AGENT = Path("/opt/hermes/run_agent.py")
TARGET_CREDITS = Path("/opt/hermes/agent/rate_limit_credits.py")
TARGET_HELPERS = Path("/opt/hermes/agent/chat_completion_helpers.py")

MARKER_STATUS = "# assistant-stack: track OpenRouter cache status"
MARKER_RETRY = "# assistant-stack: bust OpenRouter cache on empty stream"
MARKER_MSG = "# assistant-stack: OpenRouter empty-stream exhausted message"

STATUS_OLD = '''            if status.upper() == "HIT":
                self._or_cache_hits += 1
                logger.info("OpenRouter response cache HIT (total: %d)", self._or_cache_hits)
            else:
                logger.debug("OpenRouter response cache %s", status.upper())
'''

STATUS_NEW = '''            # assistant-stack: track OpenRouter cache status
            status_u = status.upper()
            self._or_last_cache_status = status_u
            if status_u == "HIT":
                self._or_cache_hits += 1
                logger.info("OpenRouter response cache HIT (total: %d)", self._or_cache_hits)
            else:
                logger.debug("OpenRouter response cache %s", status_u)
                # Drop a one-shot Cache-Clear after a successful MISS so later
                # requests can use the cache again.
                if getattr(self, "_or_cache_bust_pending", False):
                    headers = self._client_kwargs.get("default_headers")
                    if isinstance(headers, dict):
                        headers = dict(headers)
                        headers.pop("X-OpenRouter-Cache-Clear", None)
                        self._client_kwargs["default_headers"] = headers
                    self._or_cache_bust_pending = False
'''

RETRY_OLD = '''                    if (
                        _is_timeout
                        or _is_conn_err
                        or _is_sse_conn_err
                        or _is_stream_parse_err
                        or _is_empty_stream
                    ):
                        # Transient network / timeout error. Retry the
                        # streaming request with a fresh connection first.
                        if _stream_attempt < _max_stream_retries:
                            agent._emit_stream_drop(
                                error=e,
                                attempt=_stream_attempt + 2,
                                max_attempts=_max_stream_retries + 1,
                                mid_tool_call=False,
                                diag=request_client_holder.get("diag"),
                            )
'''

RETRY_NEW = '''                    if (
                        _is_timeout
                        or _is_conn_err
                        or _is_sse_conn_err
                        or _is_stream_parse_err
                        or _is_empty_stream
                    ):
                        # Transient network / timeout error. Retry the
                        # streaming request with a fresh connection first.
                        if _stream_attempt < _max_stream_retries:
                            # assistant-stack: bust OpenRouter cache on empty stream
                            # Cached empty SSE bodies replay forever without this.
                            if (
                                _is_empty_stream
                                and getattr(agent, "_or_last_cache_status", "") == "HIT"
                            ):
                                try:
                                    headers = dict(
                                        agent._client_kwargs.get("default_headers")
                                        or {}
                                    )
                                    headers["X-OpenRouter-Cache"] = "true"
                                    headers["X-OpenRouter-Cache-Clear"] = "true"
                                    agent._client_kwargs["default_headers"] = headers
                                    agent._or_cache_bust_pending = True
                                    logger.warning(
                                        "EmptyStreamError after OpenRouter cache HIT "
                                        "— sending X-OpenRouter-Cache-Clear on retry "
                                        "(attempt %d/%d)",
                                        _stream_attempt + 2,
                                        _max_stream_retries + 1,
                                    )
                                    agent._buffer_status(
                                        "↻ OpenRouter returned a cached empty stream — "
                                        "busting cache and retrying"
                                    )
                                except Exception:
                                    logger.debug(
                                        "OpenRouter cache bust failed",
                                        exc_info=True,
                                    )
                            agent._emit_stream_drop(
                                error=e,
                                attempt=_stream_attempt + 2,
                                max_attempts=_max_stream_retries + 1,
                                mid_tool_call=False,
                                diag=request_client_holder.get("diag"),
                            )
'''

MSG_OLD = '''                        elif _is_empty_stream:
                            # The connection SUCCEEDED (stream opened) but the
                            # provider sent no chunks — saying "connection
                            # failed" here sends users chasing network issues
                            # when the problem is the provider/endpoint.
                            _exhausted_msg = (
                                "❌ Provider returned an empty response stream "
                                f"after {_max_stream_retries + 1} attempts. "
                                "The provider may be experiencing issues — "
                                "try again in a moment."
                            )
'''

MSG_NEW = '''                        elif _is_empty_stream:
                            # The connection SUCCEEDED (stream opened) but the
                            # provider sent no chunks — saying "connection
                            # failed" here sends users chasing network issues
                            # when the problem is the provider/endpoint.
                            # assistant-stack: OpenRouter empty-stream exhausted message
                            _or_hits = int(getattr(agent, "_or_cache_hits", 0) or 0)
                            _or_route = "openrouter" in str(
                                getattr(agent, "base_url", "") or ""
                            ).lower() or str(
                                getattr(agent, "provider", "") or ""
                            ).lower() == "openrouter"
                            if _or_route or _or_hits:
                                _exhausted_msg = (
                                    "❌ OpenRouter returned an empty stream for this "
                                    f"model after {_max_stream_retries + 1} attempts"
                                    + (f" ({_or_hits} cache HIT(s))" if _or_hits else "")
                                    + ". Try again, disable response caching "
                                    "(HERMES_OPENROUTER_CACHE=0), or switch models."
                                )
                            else:
                                _exhausted_msg = (
                                    "❌ Provider returned an empty response stream "
                                    f"after {_max_stream_retries + 1} attempts. "
                                    "The provider may be experiencing issues — "
                                    "try again in a moment."
                                )
'''


def _patch(path: Path, old: str, new: str, marker: str, label: str) -> None:
    text = path.read_text()
    if marker in text:
        print(f"[patch-openrouter-empty-stream] {label}: already applied ({path})")
        return
    if old not in text:
        raise SystemExit(
            f"[patch-openrouter-empty-stream] {label}: expected block missing in {path}; "
            "Hermes image may have changed — update this patch."
        )
    path.write_text(text.replace(old, new, 1))
    print(f"[patch-openrouter-empty-stream] {label}: patched {path}")


def main() -> None:
    # v2026.9+ moved cache-status logging into rate_limit_credits.py.
    status_target = TARGET_CREDITS if TARGET_CREDITS.is_file() and STATUS_OLD in TARGET_CREDITS.read_text() else TARGET_AGENT
    _patch(status_target, STATUS_OLD, STATUS_NEW, MARKER_STATUS, "cache-status")
    if TARGET_HELPERS.is_file():
        helpers = TARGET_HELPERS.read_text()
        if RETRY_OLD in helpers or MARKER_RETRY in helpers:
            _patch(TARGET_HELPERS, RETRY_OLD, RETRY_NEW, MARKER_RETRY, "retry-bust")
        else:
            print(f"[patch-openrouter-empty-stream] retry-bust: skip (snippet missing in {TARGET_HELPERS})")
        if MSG_OLD in helpers or MARKER_MSG in helpers:
            _patch(TARGET_HELPERS, MSG_OLD, MSG_NEW, MARKER_MSG, "exhausted-msg")
        else:
            print(f"[patch-openrouter-empty-stream] exhausted-msg: skip (snippet missing in {TARGET_HELPERS})")
    else:
        print(f"[patch-openrouter-empty-stream] helpers missing: {TARGET_HELPERS}")


if __name__ == "__main__":
    main()
