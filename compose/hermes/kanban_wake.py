"""Deliver kanban events to a dashboard session after compression, and say how to repair a give-up.

Compatibility overlay (not upstream), for Boundary change request 0006 R4.
patch-kanban-wake.py copies this file to ``agent/kanban_wake.py``.

A dashboard (``tui``) session that files a task is subscribed with its session
key as ``chat_id``. The gateway's per-session poller claims those events and,
once the session is idle, runs them as a turn: that turn is the wake. Context
compression rotates the session key, though, and nothing re-keys the
subscriptions, so after a compression the poller matched none of them and the
events waited forever, even across resumes. ``rotated_chat_ids`` finds the
subscriptions whose key resolves through the compression lineage to the live
session's key, so the poller claims them too.

``gave_up_suffix`` replaces the poller's "gave up after repeated spawn
failures" line: it says whether a retry can help, carries the error and names
the tools that repair the task.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

ERROR_CHARS = 500
_RESOLVE_TTL_S = 300.0
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _tui_chat_ids(db_path: Path, exclude: str) -> list[str]:
    """Distinct ``tui`` subscription keys on one board, read-only (never creates the DB)."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM kanban_notify_subs "
            "WHERE LOWER(platform) = 'tui' AND chat_id IS NOT NULL AND chat_id != ?", (exclude,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [str(r[0]) for r in rows if r[0]]


def _resolved(chat_id: str, session: dict, resolve: Callable[[str, dict], str]) -> str:
    key = (str(session.get("profile_home") or ""), chat_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[1] < _RESOLVE_TTL_S:
            return hit[0]
    tip = resolve(chat_id, session) or chat_id
    with _cache_lock:
        _cache[key] = (tip, now)
    return tip


def rotated_chat_ids(
    db_path: Path, session_key: str, session: Optional[dict],
    resolve: Callable[[str, dict], str], live_holds: Callable[[set], bool],
) -> set[str]:
    """Subscription keys on this board that compression rotated into ``session_key``.

    A key some live session still holds belongs to that session, not this one.
    """
    if not session_key or session is None:
        return set()
    out: set[str] = set()
    for chat_id in _tui_chat_ids(db_path, session_key):
        if _resolved(chat_id, session, resolve) != session_key:
            continue
        if live_holds({chat_id}):
            continue
        out.add(chat_id)
    return out


def gave_up_suffix(task: Any, payload: dict, title: str) -> str:
    """Text after "Kanban <id>" for a ``gave_up`` event; also the wake turn's text."""
    task_id = getattr(task, "id", None) or "the task"
    error = str(payload.get("error") or "").strip()
    if payload.get("retryable") is False:
        head = " gave up: it can't start until its workspace or configuration changes"
    else:
        failures = payload.get("failures")
        head = f" gave up after {failures} failed attempts" if failures else " gave up after repeated failures"
    lines = [head]
    if error:
        lines.append(f"Error: {error[:ERROR_CHARS]}")
    if error.startswith("workspace:"):
        lines.append(
            f"To repair it: kanban_update(task_id='{task_id}', workspace_kind='scratch'), "
            f"then kanban_unblock(task_id='{task_id}')."
        )
    else:
        lines.append(
            f"To repair it: kanban_show(task_id='{task_id}'), fix the cause, then "
            f"kanban_unblock(task_id='{task_id}'); or file a replacement and kanban_archive this one."
        )
    return "\n".join(lines)
