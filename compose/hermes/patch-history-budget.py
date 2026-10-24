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


INSTALL_TAG = "# assistant-stack: history budget install"
SERVER_TAIL = "_m.register(sys.modules[__name__])\ndel _m\n"
INSTALL = f'''

{INSTALL_TAG}
try:
    from agent.history_budget_hook import install as _history_budget_install
    _history_budget_install(sys.modules[__name__])
except Exception:
    logger.exception("history budget overlay failed to install")
'''


def _install_server_hook() -> None:
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        raise SystemExit("[patch-history-budget] pre-v2026.9 gateway (no contracts); not supported")
    server = GATEWAY / "server.py"
    text = server.read_text()
    if INSTALL_TAG in text:
        print("[patch-history-budget] already applied (server install)")
        return
    if SERVER_TAIL not in text:
        raise SystemExit(
            "[patch-history-budget] split-module registration loop missing in server.py. "
            "Hermes image may have changed — update this patch."
        )
    server.write_text(text.rstrip("\n") + "\n" + INSTALL)
    print("[patch-history-budget] patched server install")


def main() -> None:
    _install("history_budget.py")
    _install("history_budget_hook.py")

    _replace(
        GATEWAY / "methods_session.py",
        '''def _create_session(rid, params: dict, *, copy_parent_history: bool = False) -> dict:
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
        '''                model_config.get("composer_override_profile") if overrides and overrides.get("model_override") else None))
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
        '''            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
        if live is not None:
            return _resume_reuse_live(ctx, *live)
''',
        '''            live = _find_live_session_by_key(ctx.target, ctx.profile_home)
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
        GATEWAY / "session_workdir.py",
        '''    from tools.approval_yolo import with_session_yolo  # a /yolo toggled before the row existed
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
''',
        label="model_config",
        done='model_config["history_budget"]',
    )
    # v0.22.3 moved _make_agent into tui_gateway/agent_factory.py; bind_module
    # rebinds it against server.py globals, so logger and _get_db still resolve.
    factory = GATEWAY / "agent_factory.py"
    _replace(
        factory if factory.is_file() else GATEWAY / "server.py",
        '''        agent._pending_fallback_notice = fallback_notice
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
        '''    """
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
        '''        (nothing written); the caller must then discard the pass. Any other outcome returns True."""
        session_db, session_id = getattr(self, "_session_db", None), getattr(self, "_session_id", "")
''',
        '''        (nothing written); the caller must then discard the pass. Any other outcome returns True."""
        # assistant-stack: history budget — do not archive the stored transcript.
        if getattr(self, "_history_budget_blocks_archive", False):
            return True
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

    _install_server_hook()


if __name__ == "__main__":
    main()
