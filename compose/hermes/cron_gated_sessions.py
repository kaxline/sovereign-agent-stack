"""Gated cron fires and typed turns in cron sessions (assistant-stack overlay).

Not upstream Hermes. Boundary CR 0033.

R1. The gateway commits a cron fire's session before ``run_job`` runs its
gates (monitor unchanged, ``wakeAgent: false``, empty payload, a blocked
prompt, a refused setup). Those return before ``run_job`` opens the session
store, and ``_finalize_cron_session`` (title, end) runs only after that, so
each gated fire left an untitled, never-ended, empty session in every
client's list. ``install_cron`` wraps ``gateway.session_cron.execute``: when
the fire never opened the store and its session has no messages, the session
is titled like a finished run, hidden from the default ``session.list`` and
ended with ``end_reason: cron_skipped``. A fire that opened the store is left
to upstream's own finalize, including one whose agent outlived ``run_job``.

R2. ``gateway.errors.cron_session_read_only`` is only raised for admissions
the gateway runs itself. A cron session resumed over the dashboard's
``/api/ws`` ran typed prompts as normal turns. ``install_gateway`` marks a
resumed session whose stored row has ``source: cron``, and ``prompt.submit``
on it answers error 4087 with the same text and ``error_surface`` as the
gateway's refusal, before any row is written.

Both installs wrap at runtime; the upstream files only gain an install tail.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

SKIPPED_END_REASON = "cron_skipped"
READ_ONLY_CODE = 4087
READ_ONLY_SURFACE = {"layer": "gateway", "code": "cron_session_read_only", "retryable": False}
CRON_SOURCE = "cron"

# Runtime session key this overlay owns.
SESSION_KEY = "_assistant_stack_cron_read_only"

# The fire being executed in this context; run_job's thread sees the same dict
# because asyncio.to_thread copies the context.
_FIRE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("assistant_stack_cron_fire", default=None)


# --- R1: gated fires -------------------------------------------------------

def wrap_open_store(open_store: Callable) -> Callable:
    """``cron.scheduler._open_cron_session_db``: note that this fire reached the agent."""
    def opened(*args, **kwargs):
        fire = _FIRE.get()
        if fire is not None:
            fire["opened"] = True
        return open_store(*args, **kwargs)

    opened.__wrapped__ = open_store
    return opened


def _hook_open_store() -> None:
    # run_job looks the helper up in its module globals at call time.
    from cron import scheduler

    if not hasattr(scheduler._open_cron_session_db, "__wrapped__"):
        scheduler._open_cron_session_db = wrap_open_store(scheduler._open_cron_session_db)


def skipped_title(job: dict) -> str:
    """The title ``_finalize_cron_session`` gives a finished run."""
    from hermes_time import now, safe_strftime

    job_id = str(job.get("id") or "")
    name = str(job.get("name") or job.get("prompt") or job_id or "cron job")
    base = " ".join(name.split())[:60].strip() or f"cron {job_id}"
    return f"{base} · {safe_strftime(now(), '%b %d %H:%M')}"


def settle_skipped(db: Any, session_id: str, job: dict) -> bool:
    """Title, hide and end an empty cron session. Returns False when it is not empty."""
    row = db.get_session(session_id)
    if row is None or int(row.get("message_count") or 0) > 0:
        return False
    if not (row.get("title") or "").strip():
        try:
            from cron.scheduler import _set_cron_session_title
            _set_cron_session_title(db, session_id, skipped_title(job))
        except Exception:
            logger.warning("cron %s: could not title skipped session %s", job.get("id"), session_id, exc_info=True)
    db.set_session_hidden(session_id, True)
    db.end_session(session_id, SKIPPED_END_REASON)
    return True


def _own_fire(row: dict, policy: Any) -> dict | None:
    """The frozen job when *row* is the fire this session was made for, else None
    (a typed message, which upstream's execute refuses)."""
    try:
        data = json.loads(policy.request_json)
    except (TypeError, ValueError, AttributeError):
        return None
    if row.get("request_id") != data.get("request_id"):
        return None
    job = data.get("cron_job")
    return job if isinstance(job, dict) else None


def wrap_execute(execute: Callable) -> Callable:
    async def run(authority, ref, row, policy):
        _hook_open_store()
        fire = {"opened": False}
        token = _FIRE.set(fire)
        try:
            return await execute(authority, ref, row, policy)
        finally:
            _FIRE.reset(token)
            job = None if fire["opened"] else _own_fire(row, policy)
            if job is not None:
                try:
                    if await asyncio.to_thread(settle_skipped, authority.db, ref.session_id, job):
                        logger.info("cron %s: gated fire, session %s hidden", job.get("id"), ref.session_id)
                except Exception:
                    logger.warning("cron %s: could not settle skipped session %s",
                                   job.get("id"), ref.session_id, exc_info=True)

    run.__wrapped__ = execute
    return run


def install_cron(module: Any) -> None:
    """Called from the tail of gateway/session_cron.py with that module."""
    if hasattr(module.execute, "__wrapped__"):
        return
    module.execute = wrap_execute(module.execute)


# --- R2: typed turns in a resumed cron session -------------------------------

def _stored_source(server: Any, params: dict, ids: list[str]) -> str | None:
    with server._profile_db(params) as db:
        if db is None:
            return None
        for sid in ids:
            row = db.get_session(sid) if sid else None
            if row is not None:
                return (row.get("source") or "").strip().lower()
    return None


def wrap_resume(server: Any, handler: Callable) -> Callable:
    def resume(rid, params: dict) -> dict:
        response = handler(rid, params)
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or not isinstance(params, dict):
            return response
        record = server._sessions.get(result.get("session_id") or "")
        if record is None:
            return response
        try:
            ids = [result.get("resumed") or "", params.get("session_id") or ""]
            if _stored_source(server, params, ids) == CRON_SOURCE:
                record[SESSION_KEY] = True
        except Exception:
            logger.warning("cron read-only: source lookup failed for %s", params.get("session_id"), exc_info=True)
        return response

    resume.__wrapped__ = handler
    return resume


def read_only_error(server: Any, rid: Any) -> dict:
    from agent.i18n import t

    return server._err(rid, READ_ONLY_CODE, t("gateway.errors.cron_session_read_only"),
                       {"error_surface": dict(READ_ONLY_SURFACE)})


def wrap_prompt_submit(server: Any, handler: Callable) -> Callable:
    def submit(rid, params: dict) -> dict:
        sid = params.get("session_id") if isinstance(params, dict) else None
        record = server._sessions.get(sid or "")
        if record is not None and record.get(SESSION_KEY):
            return read_only_error(server, rid)
        return handler(rid, params)

    submit.__wrapped__ = handler
    return submit


def install_gateway(server: Any) -> None:
    """Called from the tail of tui_gateway/server.py with that module."""
    if getattr(server, "_cron_read_only_installed", False):
        return
    methods = server._methods
    methods["session.resume"] = wrap_resume(server, methods["session.resume"])
    methods["prompt.submit"] = wrap_prompt_submit(server, methods["prompt.submit"])
    server._cron_read_only_installed = True
