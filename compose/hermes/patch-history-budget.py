#!/usr/bin/env python3
"""Per-session history budget on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies history_budget.py and
history_budget_hook.py into the agent package and patches the request
path so a session budget windows the prompt without archiving older rows.

The wire contract fields (v2026.9+ params models refuse unknown keys with
4000) and the session.context_breakdown fields are installed at runtime by
history_budget_hook.install, from one call appended to tui_gateway/server.py.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, SERVER_TAIL_ANCHOR, FileEdit, edit_files, fail, install, install_tail

TAG = "[patch-history-budget]"
AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"


SELECT_CONTEXT_HOOK = '''

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


INSTALL_TAG = "# assistant-stack: history budget install"
INSTALL = install_tail(INSTALL_TAG, "history_budget_hook", "history budget",
                       alias="_history_budget_install")


def main() -> None:
    install(BOOTSTRAP / "history_budget.py", AGENT / "history_budget.py", TAG)
    install(BOOTSTRAP / "history_budget_hook.py", AGENT / "history_budget_hook.py", TAG)
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/registry.py): not supported")
    # _make_agent lives in tui_gateway/agent_factory.py; bind_module rebinds it
    # against server.py globals, so logger and _get_db resolve.
    factory = GATEWAY / "agent_factory.py"
    # Every anchor in all eight files is checked before any is written.
    edit_files([
        FileEdit(GATEWAY / "methods_session.py", [('''def _create_session(rid, params: dict, *, copy_parent_history: bool = False) -> dict:
    """``session.create``; ``copy_parent_history`` (``session.branch_stored``) reads the parent's
    transcript server-side and omits it from the reply."""
''',
                '''def _create_session(rid, params: dict, *, copy_parent_history: bool = False) -> dict:
    """``session.create``; ``copy_parent_history`` (``session.branch_stored``) reads the parent's
    transcript server-side and omits it from the reply."""
    # assistant-stack: history budget
    try:
        from agent.history_budget import coerce_history_budget
        history_budget = (
            coerce_history_budget(params.get("history_budget"))
            if "history_budget" in params else None
        )
    except ValueError as exc:
        return _err(rid, 4028, str(exc))
''')],
                 label="session.create", marker="coerce_history_budget(params.get(\"history_budget\"))"),
        FileEdit(GATEWAY / "methods_session.py", [('''            "follow_profile_config": _flag(params, "follow_profile_config"),
''',
                '''            "follow_profile_config": _flag(params, "follow_profile_config"),
            "history_budget": history_budget,  # assistant-stack: history budget
''')],
                 label="session.create record", marker='"history_budget": history_budget'),
        FileEdit(GATEWAY / "methods_session.py", [('''                model_config.get("composer_override_profile") if overrides and overrides.get("model_override") else None))
        return record

    def claim(self, sid: str, record: dict) -> dict | None:
''',
                '''                model_config.get("composer_override_profile") if overrides and overrides.get("model_override") else None))
        # assistant-stack: history budget
        record["history_budget"] = getattr(self, "history_budget", None)
        record["history_summary"] = getattr(self, "history_summary", None)
        record["history_summary_through_row_id"] = getattr(self, "history_summary_through_row_id", None)
        return record

    def claim(self, sid: str, record: dict) -> dict | None:
''')],
                 label="session.resume record", marker='record["history_budget"]'),
        FileEdit(GATEWAY / "methods_session.py", [('''def _resume_live_unpersisted(ctx: _Resume, live_sid: str, live: dict) -> dict:
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
''')],
                 label="resume unpersisted", marker="_apply_resume_history_budget(ctx, (live_sid, live))"),
        FileEdit(GATEWAY / "methods_session.py", [('''            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
        if live is not None:
            return _resume_reuse_live(ctx, *live)
''',
                '''            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
        # assistant-stack: history budget
        if (budget_err := _apply_resume_history_budget(ctx, live)) is not None:
            return budget_err
        if live is not None:
            return _resume_reuse_live(ctx, *live)
''')],
                 label="session.resume", marker="_apply_resume_history_budget(ctx, live)"),
        FileEdit(GATEWAY / "methods_session.py", [('''                session.update(display_history_prefix=display_history_prefix, active_session_lease=None)
''',
                '''                session.update(
                    display_history_prefix=display_history_prefix, active_session_lease=None,
                    history_budget=getattr(ctx, "history_budget", None),
                    history_summary=getattr(ctx, "history_summary", None),
                    history_summary_through_row_id=getattr(ctx, "history_summary_through_row_id", None))
''')],
                 label="eager resume session", marker='history_budget=getattr(ctx, "history_budget", None)'),
        FileEdit(GATEWAY / "methods_session.py", [('''@method("session.resume")
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
''')],
                 label="resume helper", marker="def _apply_resume_history_budget"),
        FileEdit(GATEWAY / "session_workdir.py", [('''    from tools.approval_yolo import with_session_yolo  # a /yolo toggled before the row existed
    return row_model, with_session_yolo(model_config, session.get("session_key") or "")
''',
                '''    # assistant-stack: history budget
    budget = session.get("history_budget")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        model_config["history_budget"] = budget
    summary = session.get("history_summary")
    if isinstance(summary, str) and summary:
        model_config["history_summary"] = summary
    through = session.get("history_summary_through_row_id")
    if isinstance(through, int) and not isinstance(through, bool):
        model_config["history_summary_through_row_id"] = through
    from tools.approval_yolo import with_session_yolo  # a /yolo toggled before the row existed
    return row_model, with_session_yolo(model_config, session.get("session_key") or "")
''')],
                 label="model_config", marker='model_config["history_budget"]'),
        FileEdit(factory, [('''        agent._pending_fallback_notice = fallback_notice
    return agent
''',
                '''        agent._pending_fallback_notice = fallback_notice
    # assistant-stack: history budget
    try:
        from agent.history_budget_hook import stamp_agent_budget
        stamp_agent_budget(
            agent, session, session_db if session_db is not None else _get_db(), session_id or key)
    except Exception:
        logger.debug("history budget stamp failed", exc_info=True)
        agent.history_budget = None
    return agent
''')],
                 label="make_agent", marker="stamp_agent_budget("),
        FileEdit(GATEWAY / "prompt_turn.py", [('''    payload = {"text": raw, "usage": _get_usage(agent), "status": status}
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
''')],
                 label="message.complete", marker="complete_fields(agent)"),
        FileEdit(AGENT / "conversation_compression.py", [('''    """
    attempt = _begin_compression_attempt(
        agent, force=force, defer_notification=defer_context_engine_notification, trigger=trigger,
''',
                '''    """
    # assistant-stack: history budget windows the request and must not archive rows.
    try:
        from agent.history_budget import agent_has_history_budget
        if agent_has_history_budget(agent):
            return messages, system_message
    except Exception:
        pass
    attempt = _begin_compression_attempt(
        agent, force=force, defer_notification=defer_context_engine_notification, trigger=trigger,
''')],
                 label="compress_context", marker="agent_has_history_budget(agent)"),
        FileEdit(AGENT / "micro_compaction.py", [('''        if not self._micro_compact_enabled:
            return messages

        # Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
''',
                '''        if not self._micro_compact_enabled or getattr(self, "_history_budget_blocks_archive", False):
            return messages

        # Cadence gate: each pass breaks prompt cache once. Counted per invocation so a no-op turn
''')],
                 label="micro_compact", marker="_history_budget_blocks_archive"),
        FileEdit(AGENT / "micro_compaction.py", [('''        (nothing written); the caller must then discard the pass. Any other outcome returns True."""
        session_db, session_id = getattr(self, "_session_db", None), getattr(self, "_session_id", "")
''',
                '''        (nothing written); the caller must then discard the pass. Any other outcome returns True."""
        # assistant-stack: history budget — do not archive the stored transcript.
        if getattr(self, "_history_budget_blocks_archive", False):
            return True
        session_db, session_id = getattr(self, "_session_db", None), getattr(self, "_session_id", "")
''')],
                 label="micro_compact db", marker="history budget — do not archive"),
        FileEdit(AGENT / "context_compressor.py", label="select_context", append=SELECT_CONTEXT_HOOK,
                 requires=["class ContextCompressor"], marker="_history_budget_select_context"),
        FileEdit(GATEWAY / "server.py", label="server install", append=INSTALL, requires=[SERVER_TAIL_ANCHOR],
                 marker=INSTALL_TAG),
    ], INSTALL_TAG, TAG)


if __name__ == "__main__":
    main()
