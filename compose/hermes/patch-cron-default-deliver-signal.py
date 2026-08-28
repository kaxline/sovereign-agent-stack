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
def _assistant_stack_default_deliver_signal(deliver, origin):
    """Default api_server/WebUI cron delivery to Signal when configured."""
    if not origin or str(origin.get("platform") or "").lower() != "api_server":
        return deliver
    normalized = (deliver or "").strip().lower()
    if normalized not in ("", "origin"):
        return deliver
    import os
    from gateway.config import get_secret
    home = (
        os.getenv("SIGNAL_HOME_CHANNEL", "")
        or get_secret("SIGNAL_HOME_CHANNEL", "")
    ).strip()
    if home:
        return "signal"
    return deliver

'''

CREATE_OLD = '''            from cron.scheduler import (
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

CREATE_NEW = '''            from cron.scheduler import (
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

UPDATE_OLD = '''            if deliver is not None:
                bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
                if bot_chat_error:
                    return tool_error(bot_chat_error, success=False)
                updates["deliver"] = _resolve_cron_context_deliver(
                    _normalize_deliver_param(deliver)
                )
'''

UPDATE_NEW = '''            if deliver is not None:
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

INSERT_BEFORE = "def _local_delivery_notice(job: Dict[str, Any], user_deliver: Optional[str]) -> Optional[str]:"


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
    if CREATE_OLD not in text:
        print(f"skip: create snippet not found in {TARGET}")
        return 0
    if UPDATE_OLD not in text:
        print(f"skip: update snippet not found in {TARGET}")
        return 0
    helper = HELPER.replace(
        "def _assistant_stack_default_deliver_signal(deliver, origin):",
        "def _assistant_stack_default_deliver_signal(deliver, origin):  "
        + MARKER,
        1,
    )
    text = text.replace(INSERT_BEFORE, helper + INSERT_BEFORE, 1)
    text = text.replace(CREATE_OLD, CREATE_NEW, 1)
    text = text.replace(UPDATE_OLD, UPDATE_NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
