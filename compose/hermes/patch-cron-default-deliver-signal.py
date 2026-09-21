#!/usr/bin/env python3
"""Default WebUI cron jobs to deliver via Signal when SIGNAL_HOME_CHANNEL is set.

Jobs created from api_server (WebUI) sessions default to deliver=origin, which
cannot send. When Signal home channel env is present, store deliver=signal.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/cronjob_tools.py")
MARKER = "# assistant-stack: default WebUI cron deliver to signal"

HELPER = '''
def _assistant_stack_default_deliver_signal(deliver, origin):  # assistant-stack: default WebUI cron deliver to signal
    """Default api_server/WebUI cron delivery to Signal when configured."""
    if not origin or str(origin.get("platform") or "").lower() != "api_server":
        return deliver
    normalized = (deliver or "").strip().lower()
    if normalized not in ("", "origin"):
        return deliver
    import os
    from agent.secret_scope import get_secret
    home = (
        os.getenv("SIGNAL_HOME_CHANNEL", "")
        or get_secret("SIGNAL_HOME_CHANNEL", "")
    ).strip()
    if home:
        return "signal"
    return deliver

'''

# v2026.9+ compacted create path.
CREATE_OLD_V9 = '''    from cron.scheduler import CronSchedulerRegistrationError, create_job_with_scheduler_registration
    try:
        job = create_job_with_scheduler_registration(
            prompt=prompt or "", schedule=a["schedule"], name=a["name"], repeat=a["repeat"],
            deliver=_resolve_cron_context_deliver(deliver), origin=_origin_from_env(), skills=canonical_skills,
'''

CREATE_NEW_V9 = '''    from cron.scheduler import CronSchedulerRegistrationError, create_job_with_scheduler_registration
    _assistant_stack_origin = _origin_from_env()
    deliver = _assistant_stack_default_deliver_signal(deliver, _assistant_stack_origin)
    try:
        job = create_job_with_scheduler_registration(
            prompt=prompt or "", schedule=a["schedule"], name=a["name"], repeat=a["repeat"],
            deliver=_resolve_cron_context_deliver(deliver), origin=_assistant_stack_origin, skills=canonical_skills,
'''

UPDATE_OLD_V9 = '''    if deliver is not None:
        bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
        if bot_chat_error:
            return bot_chat_error
        updates["deliver"] = _resolve_cron_context_deliver(_normalize_deliver_param(deliver))
'''

UPDATE_NEW_V9 = '''    if deliver is not None:
        bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
        if bot_chat_error:
            return bot_chat_error
        updates["deliver"] = _resolve_cron_context_deliver(
            _assistant_stack_default_deliver_signal(
                _normalize_deliver_param(deliver),
                _origin_from_env(),
            )
        )
'''

# Pre-v2026.9 expanded create/update.
CREATE_OLD_LEGACY = '''            from cron.scheduler import (
                CronSchedulerRegistrationError,
                create_job_with_scheduler_registration,
            )

            try:
                job = create_job_with_scheduler_registration(
                    prompt=prompt or "",
                    schedule=schedule,
                    name=name,
                    repeat=repeat,
                    deliver=_resolve_cron_context_deliver(
                        _normalize_deliver_param(deliver)
                    ),
                    origin=_origin_from_env(),
'''

CREATE_NEW_LEGACY = '''            from cron.scheduler import (
                CronSchedulerRegistrationError,
                create_job_with_scheduler_registration,
            )

            _assistant_stack_origin = _origin_from_env()
            _assistant_stack_deliver = _assistant_stack_default_deliver_signal(
                _normalize_deliver_param(deliver),
                _assistant_stack_origin,
            )

            try:
                job = create_job_with_scheduler_registration(
                    prompt=prompt or "",
                    schedule=schedule,
                    name=name,
                    repeat=repeat,
                    deliver=_resolve_cron_context_deliver(
                        _assistant_stack_deliver
                    ),
                    origin=_assistant_stack_origin,
'''

UPDATE_OLD_LEGACY = '''            if deliver is not None:
                bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
                if bot_chat_error:
                    return tool_error(bot_chat_error, success=False)
                updates["deliver"] = _resolve_cron_context_deliver(
                    _normalize_deliver_param(deliver)
                )
'''

UPDATE_NEW_LEGACY = '''            if deliver is not None:
                bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
                if bot_chat_error:
                    return tool_error(bot_chat_error, success=False)
                updates["deliver"] = _resolve_cron_context_deliver(
                    _assistant_stack_default_deliver_signal(
                        _normalize_deliver_param(deliver),
                        _origin_from_env(),
                    )
                )
'''

INSERT_BEFORE = "def _dumps(payload: Dict[str, Any]) -> str:"


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if INSERT_BEFORE not in text:
        print(f"skip: helper insert point not found in {TARGET}")
        return 0

    if CREATE_OLD_V9 in text and UPDATE_OLD_V9 in text:
        text = text.replace(INSERT_BEFORE, HELPER + INSERT_BEFORE, 1)
        text = text.replace(CREATE_OLD_V9, CREATE_NEW_V9, 1)
        text = text.replace(UPDATE_OLD_V9, UPDATE_NEW_V9, 1)
        TARGET.write_text(text, encoding="utf-8")
        print(f"patched: {TARGET}")
        return 0

    if CREATE_OLD_LEGACY in text and UPDATE_OLD_LEGACY in text:
        text = text.replace(INSERT_BEFORE, HELPER + INSERT_BEFORE, 1)
        text = text.replace(CREATE_OLD_LEGACY, CREATE_NEW_LEGACY, 1)
        text = text.replace(UPDATE_OLD_LEGACY, UPDATE_NEW_LEGACY, 1)
        TARGET.write_text(text, encoding="utf-8")
        print(f"patched: {TARGET}")
        return 0

    print(f"skip: create/update snippets not found in {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
