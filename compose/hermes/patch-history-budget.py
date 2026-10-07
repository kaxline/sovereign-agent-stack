#!/usr/bin/env python3
"""Per-session history budget on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies history_budget.py and
history_budget_hook.py into the agent package and patches the request
path so a session budget windows the prompt without archiving older rows.

Also declares the new fields on the gateway wire contract (v2026.9+), whose
params models reject unknown keys with 4000: without that, session.create and
session.resume refuse history_budget and clients fall back to no budget.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"
BOOTSTRAP = Path(os.environ.get("HISTORY_BUDGET_BOOTSTRAP", "/bootstrap"))


def _install(name: str) -> None:
    src = BOOTSTRAP / name
    dst = AGENT / name
    if not src.is_file():
        raise SystemExit(f"[patch-history-budget] missing {src}")
    shutil.copyfile(src, dst)
    print(f"[patch-history-budget] installed {dst}")


def _replace(path: Path, old: str, new: str, *, label: str, done: str) -> None:
    text = path.read_text()
    if done in text:
        print(f"[patch-history-budget] already applied ({label})")
        return
    if old not in text:
        raise SystemExit(
            f"[patch-history-budget] expected block missing: {label} in {path}. "
            "Hermes image may have changed — update this patch."
        )
    path.write_text(text.replace(old, new, 1))
    print(f"[patch-history-budget] patched {label}")


_CONTRACT_FIELDS = (
    "    history_budget: int | None = None  # assistant-stack: history budget\n"
    "    history_tokens: int | None = None\n"
    "    history_first_row_id: int | None = None\n"
    "    history_summarised: bool | None = None\n"
)
_BUDGET_PARAM = "    history_budget: int | None = None  # assistant-stack: history budget\n"
_RESUME_DOC = (
    '    """``session_id`` is the STORED id (or an exact title); '
    "the reply's ``session_id`` is the runtime id.\"\"\"\n"
)


def _patch_contracts() -> None:
    contracts = GATEWAY / "contracts"
    if not (contracts / "sessions.py").is_file():
        print("[patch-history-budget] skip contracts (pre-v2026.9 gateway)")
        return
    create = "class SessionCreateParams(ProfileParams):\n    cols: int | None = None\n"
    _replace(
        contracts / "sessions.py", create, create + _BUDGET_PARAM,
        label="session.create contract", done=create + _BUDGET_PARAM,
    )
    resume = "class SessionResumeParams(SessionParams):\n" + _RESUME_DOC + "\n    cols: int | None = None\n"
    _replace(
        contracts / "sessions.py", resume, resume + _BUDGET_PARAM,
        label="session.resume contract", done=resume + _BUDGET_PARAM,
    )
    breakdown = "    context_source: str\n    model: str\n"
    breakdown_tail = '\n\nmethod("session.context_breakdown"'
    _replace(
        contracts / "sessions.py", breakdown + breakdown_tail,
        breakdown + _CONTRACT_FIELDS + breakdown_tail,
        label="session.context_breakdown contract", done=breakdown + _CONTRACT_FIELDS,
    )
    complete = "    partial: bool | None = None\n"
    complete_tail = '\n\nevent("message.complete"'
    _replace(
        contracts / "events.py", complete + complete_tail,
        complete + _CONTRACT_FIELDS + complete_tail,
        label="message.complete contract", done=complete + _CONTRACT_FIELDS,
    )


def main() -> None:
    _install("history_budget.py")
    _install("history_budget_hook.py")
    _patch_contracts()

    _replace(
        GATEWAY / "methods_session.py",
        '''@method("session.create")
def _(rid, params: dict) -> dict:
    (sid, source), key = _new_runtime_ids(params), _new_session_key()
''',
        '''@method("session.create")
def _(rid, params: dict) -> dict:
    # assistant-stack: history budget
    try:
        from agent.history_budget import coerce_history_budget
        history_budget = (
            coerce_history_budget(params.get("history_budget"))
            if "history_budget" in params else None
        )
    except ValueError as exc:
        return _err(rid, 4028, str(exc))
    (sid, source), key = _new_runtime_ids(params), _new_session_key()
''',
        label="session.create",
        done="coerce_history_budget(params.get(\"history_budget\"))",
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''            "follow_profile_config": _flag(params, "follow_profile_config"),
''',
        '''            "follow_profile_config": _flag(params, "follow_profile_config"),
            "history_budget": history_budget,  # assistant-stack: history budget
''',
        label="session.create record",
        done='"history_budget": history_budget',
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''        return _deferred_session_record(
            self.target, cols=self.cols, cwd=cwd, history=history, lease=None, source=source,
            close_on_disconnect=_flag(self.params, "close_on_disconnect"),
            profile_home=self.profile_home, explicit_cwd=bool(self.profile_resume_cwd), **extra)
''',
        '''        record = _deferred_session_record(
            self.target, cols=self.cols, cwd=cwd, history=history, lease=None, source=source,
            close_on_disconnect=_flag(self.params, "close_on_disconnect"),
            profile_home=self.profile_home, explicit_cwd=bool(self.profile_resume_cwd), **extra)
        # assistant-stack: history budget
        record["history_budget"] = getattr(self, "history_budget", None)
        record["history_summary"] = getattr(self, "history_summary", None)
        record["history_summary_through_row_id"] = getattr(self, "history_summary_through_row_id", None)
        return record
''',
        label="session.resume record",
        done='record["history_budget"]',
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''def _resume_live_unpersisted(ctx: _Resume, live_sid: str, live: dict) -> dict:
    """Reattach a LIVE lazy session with no state.db row yet (every fresh Bot Chat; a 404 here killed messaging
    for never-spoken bots). Attach the transport and cancel the armed orphan-reap Timer (a WS drop may have
    sentinel-parked the record) or it fires against this client."""
    if ctx.owns_db:
''',
        '''def _resume_live_unpersisted(ctx: _Resume, live_sid: str, live: dict) -> dict:
    """Reattach a LIVE lazy session with no state.db row yet (every fresh Bot Chat; a 404 here killed messaging
    for never-spoken bots). Attach the transport and cancel the armed orphan-reap Timer (a WS drop may have
    sentinel-parked the record) or it fires against this client."""
    # assistant-stack: history budget
    if (budget_err := _apply_resume_history_budget(ctx, (live_sid, live))) is not None:
        return budget_err
    if ctx.owns_db:
''',
        label="resume unpersisted",
        done="_apply_resume_history_budget(ctx, (live_sid, live))",
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''        with _session_resume_lock:
            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
        if live is not None:
            return _resume_reuse_live(ctx, *live)
''',
        '''        with _session_resume_lock:
            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
        # assistant-stack: history budget
        if (budget_err := _apply_resume_history_budget(ctx, live)) is not None:
            return budget_err
        if live is not None:
            return _resume_reuse_live(ctx, *live)
''',
        label="session.resume",
        done="_apply_resume_history_budget(ctx, live)",
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''                session.update(display_history_prefix=display_history_prefix, active_session_lease=None)
''',
        '''                session.update(
                    display_history_prefix=display_history_prefix, active_session_lease=None,
                    history_budget=getattr(ctx, "history_budget", None),
                    history_summary=getattr(ctx, "history_summary", None),
                    history_summary_through_row_id=getattr(ctx, "history_summary_through_row_id", None))
''',
        label="eager resume session",
        done='history_budget=getattr(ctx, "history_budget", None)',
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''@method("session.resume")
def _(rid, params: dict) -> dict:
''',
        '''def _apply_resume_history_budget(ctx, live):
    """assistant-stack: history budget on session.resume.

    Omitting the field keeps the stored value. Null clears it. A positive
    integer replaces it. Invalid values are an RPC error and change nothing.
    """
    from agent.history_budget import coerce_history_budget
    from agent.history_budget_hook import stamp_agent_budget
    cfg = _parse_model_config((ctx.found or {}).get("model_config"), quiet=True)
    stored = cfg.get("history_budget")
    stored_ok = stored if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0 else None
    summary = cfg.get("history_summary") if isinstance(cfg.get("history_summary"), str) else None
    through = cfg.get("history_summary_through_row_id")
    through_ok = through if isinstance(through, int) and not isinstance(through, bool) else None
    if "history_budget" in ctx.params:
        try:
            resolved = coerce_history_budget(ctx.params.get("history_budget"))
        except ValueError as exc:
            return _err(ctx.rid, 4028, str(exc))
        if ctx.db is not None and ctx.target:
            patch = {"history_budget": resolved}
            if resolved is None:
                patch["history_summary"] = None
                patch["history_summary_through_row_id"] = None
                summary, through_ok = None, None
            try:
                ctx.db.patch_session_model_config(ctx.target, patch)
            except Exception:
                logger.debug("history_budget persist failed", exc_info=True)
    else:
        resolved = stored_ok
        if stored_ok is None and live is not None and "history_budget" in live[1]:
            resolved = live[1].get("history_budget")
            if not (isinstance(resolved, int) and not isinstance(resolved, bool) and resolved > 0):
                resolved = None
            if summary is None and isinstance(live[1].get("history_summary"), str):
                summary = live[1].get("history_summary")
            if through_ok is None and isinstance(live[1].get("history_summary_through_row_id"), int):
                through_ok = live[1].get("history_summary_through_row_id")
    ctx.history_budget = resolved
    ctx.history_summary = summary
    ctx.history_summary_through_row_id = through_ok
    if live is not None:
        session = live[1]
        session["history_budget"] = resolved
        session["history_summary"] = summary
        session["history_summary_through_row_id"] = through_ok
        agent = session.get("agent")
        if agent is not None:
            stamp_agent_budget(
                agent, session, getattr(agent, "session_db", None), getattr(agent, "session_id", None))
    return None


@method("session.resume")
def _(rid, params: dict) -> dict:
''',
        label="resume helper",
        done="def _apply_resume_history_budget",
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''@_session_method("session.context_breakdown")
def _(rid, params: dict, session: dict) -> dict:
    if (agent := session.get("agent")) is None:
        usage = _session_usage_snapshot(session) or _get_usage(None)
        return _ok(rid, {
            "categories": [], "context_max": usage.get("context_max", 0) or 0,
            "context_percent": usage.get("context_percent", 0) or 0,
            "context_used": usage.get("context_used", 0) or 0,
            "estimated_total": 0,
            "context_estimated": usage.get("context_estimated", False),
            "context_source": usage.get("context_source", "provider_usage"),
            "model": _metadata_mirror(session).get("model", "")})
    with session["history_lock"]:
        history = list(session.get("history", []))
''',
        '''def _attach_history_budget_fields(session, agent, history, payload):
    """assistant-stack: history budget fields on session.context_breakdown."""
    try:
        from agent.history_budget_hook import breakdown_fields
        payload.update(breakdown_fields(session, agent, history))
    except Exception:
        logger.debug("history budget breakdown failed", exc_info=True)
        payload.update({
            "history_budget": None, "history_tokens": None,
            "history_first_row_id": None, "history_summarised": False})
    return payload


@_session_method("session.context_breakdown")
def _(rid, params: dict, session: dict) -> dict:
    with session["history_lock"]:
        history = list(session.get("history") or [])
    if (agent := session.get("agent")) is None:
        usage = _session_usage_snapshot(session) or _get_usage(None)
        payload = {
            "categories": [], "context_max": usage.get("context_max", 0) or 0,
            "context_percent": usage.get("context_percent", 0) or 0,
            "context_used": usage.get("context_used", 0) or 0,
            "estimated_total": 0,
            "context_estimated": usage.get("context_estimated", False),
            "context_source": usage.get("context_source", "provider_usage"),
            "model": _metadata_mirror(session).get("model", "")}
        return _ok(rid, _attach_history_budget_fields(session, None, history, payload))
''',
        label="context_breakdown preamble",
        done="def _attach_history_budget_fields",
    )
    _replace(
        GATEWAY / "methods_session.py",
        '''        from agent.context_breakdown import compute_session_context_breakdown
        return _ok(rid, compute_session_context_breakdown(agent, history))
''',
        '''        from agent.context_breakdown import compute_session_context_breakdown
        payload = compute_session_context_breakdown(agent, history)
        return _ok(rid, _attach_history_budget_fields(session, agent, history, payload))
''',
        label="context_breakdown result",
        done="payload = compute_session_context_breakdown(agent, history)",
    )
    _replace(
        GATEWAY / "session_workdir.py",
        '''    for flag in ("room_plumbing", "follow_profile_config"):
        if session.get(flag):
            model_config[flag] = True
    return row_model, model_config
''',
        '''    for flag in ("room_plumbing", "follow_profile_config"):
        if session.get(flag):
            model_config[flag] = True
    # assistant-stack: history budget
    budget = session.get("history_budget")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        model_config["history_budget"] = budget
    summary = session.get("history_summary")
    if isinstance(summary, str) and summary:
        model_config["history_summary"] = summary
    through = session.get("history_summary_through_row_id")
    if isinstance(through, int) and not isinstance(through, bool):
        model_config["history_summary_through_row_id"] = through
    return row_model, model_config
''',
        label="model_config",
        done='model_config["history_budget"]',
    )
    _replace(
        GATEWAY / "server.py",
        '''    agent._context_cwd_is_launch_artifact = bool(context_cwd_is_launch_artifact)
    return agent
''',
        '''    agent._context_cwd_is_launch_artifact = bool(context_cwd_is_launch_artifact)
    # assistant-stack: history budget
    try:
        from agent.history_budget_hook import stamp_agent_budget
        stamp_agent_budget(
            agent, session, session_db if session_db is not None else _get_db(), session_id or key)
    except Exception:
        logger.debug("history budget stamp failed", exc_info=True)
        agent.history_budget = None
    return agent
''',
        label="make_agent",
        done="stamp_agent_budget(",
    )
    _replace(
        GATEWAY / "prompt_turn.py",
        '''    payload = {"text": raw, "usage": _get_usage(agent), "status": status}
''',
        '''    payload = {"text": raw, "usage": _get_usage(agent), "status": status}
    # assistant-stack: history budget
    try:
        from agent.history_budget_hook import complete_fields
        payload.update(complete_fields(agent))
    except Exception:
        payload.update({
            "history_budget": None, "history_tokens": None,
            "history_first_row_id": None, "history_summarised": False})
''',
        label="message.complete",
        done="complete_fields(agent)",
    )
    _replace(
        AGENT / "conversation_compression.py",
        '''    session state after its caller has moved on.
    """
    attempt = _begin_compression_attempt(agent, force=force, defer_notification=defer_context_engine_notification)
''',
        '''    session state after its caller has moved on.
    """
    # assistant-stack: history budget windows the request and must not archive rows.
    try:
        from agent.history_budget import agent_has_history_budget
        if agent_has_history_budget(agent):
            return messages, system_message
    except Exception:
        pass
    attempt = _begin_compression_attempt(agent, force=force, defer_notification=defer_context_engine_notification)
''',
        label="compress_context",
        done="agent_has_history_budget(agent)",
    )
    _replace(
        AGENT / "micro_compaction.py",
        '''        if not self._micro_compact_enabled:
            return messages

        # Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
''',
        '''        if not self._micro_compact_enabled or getattr(self, "_history_budget_blocks_archive", False):
            return messages

        # Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
''',
        label="micro_compact",
        done="_history_budget_blocks_archive",
    )
    _replace(
        AGENT / "micro_compaction.py",
        '''        summary and the originals."""
        session_db, session_id = getattr(self, "_session_db", None), getattr(self, "_session_id", "")
''',
        '''        summary and the originals."""
        # assistant-stack: history budget — do not archive the stored transcript.
        if getattr(self, "_history_budget_blocks_archive", False):
            return
        session_db, session_id = getattr(self, "_session_db", None), getattr(self, "_session_id", "")
''',
        label="micro_compact db",
        done="history budget — do not archive",
    )

    compressor = AGENT / "context_compressor.py"
    hook = '''

# assistant-stack: history budget select_context
def _history_budget_select_context(
    self, request_messages, *, conversation_messages=None, incoming_message=None, budget_tokens=0,
):
    from agent.history_budget_hook import select_context
    return select_context(
        self, request_messages, conversation_messages=conversation_messages,
        incoming_message=incoming_message, budget_tokens=budget_tokens,
    )


ContextCompressor.select_context = _history_budget_select_context
'''
    text = compressor.read_text()
    if "_history_budget_select_context" in text:
        print("[patch-history-budget] already applied (select_context)")
    else:
        if "class ContextCompressor" not in text:
            raise SystemExit(
                f"[patch-history-budget] ContextCompressor missing in {compressor}"
            )
        compressor.write_text(text + hook)
        print("[patch-history-budget] patched select_context")


if __name__ == "__main__":
    main()
