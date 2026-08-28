#!/usr/bin/env python3
"""Synthesize Signal PlatformConfig for cron delivery when adapter is disabled.

Browser/WebUI keeps platforms.signal.enabled=false so default owns the SSE
lock; cron delivery still needs credentials from SIGNAL_* env (same model as
send_message_tool patch).
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/cron/scheduler.py")
MARKER = "# assistant-stack: cron synthesize Signal from env when adapter disabled"

OLD = '''        if transport is not None and transport.is_relay:
            # A relay transport carries the RELAY adapter's config, and
            # resolve_delivery_transport already applied relay's enablement
            # rule (config block absent OR enabled). The logical platform is
            # deliberately NOT natively enabled in a relay-fronted deployment
            # (its credential lives in the connector), so the native
            # configured/enabled gate below must not apply — it used to
            # reject exactly the targets the relay was resolved to serve.
            if pconfig is None:
                from gateway.config import PlatformConfig
                pconfig = PlatformConfig(enabled=True)
        elif not pconfig or not pconfig.enabled:
            msg = f"platform '{platform_name}' not configured/enabled"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue
'''

SYNTH_BLOCK = '''        # assistant-stack: cron synthesize Signal from env when adapter disabled
        if platform_name == "signal" and (not pconfig or not pconfig.enabled):
            import os
            from gateway.config import HomeChannel, PlatformConfig
            from agent.secret_scope import get_secret
            signal_url = (
                os.getenv("SIGNAL_HTTP_URL", "") or get_secret("SIGNAL_HTTP_URL", "")
            ).strip()
            signal_account = (
                os.getenv("SIGNAL_ACCOUNT", "") or get_secret("SIGNAL_ACCOUNT", "")
            ).strip()
            if signal_url and signal_account:
                pconfig = PlatformConfig(
                    enabled=True,
                    extra={
                        "http_url": signal_url,
                        "account": signal_account,
                    },
                )
                signal_home = (
                    os.getenv("SIGNAL_HOME_CHANNEL", "")
                    or get_secret("SIGNAL_HOME_CHANNEL", "")
                ).strip()
                if signal_home:
                    pconfig.home_channel = HomeChannel(
                        platform=platform,
                        chat_id=signal_home,
                        name=os.getenv("SIGNAL_HOME_CHANNEL_NAME", "Home"),
                    )

'''

RELAY_TAIL = '''        if transport is not None and transport.is_relay:
            # A relay transport carries the RELAY adapter's config, and
            # resolve_delivery_transport already applied relay's enablement
            # rule (config block absent OR enabled). The logical platform is
            # deliberately NOT natively enabled in a relay-fronted deployment
            # (its credential lives in the connector), so the native
            # configured/enabled gate below must not apply — it used to
            # reject exactly the targets the relay was resolved to serve.
            if pconfig is None:
                from gateway.config import PlatformConfig
                pconfig = PlatformConfig(enabled=True)
        elif not pconfig or not pconfig.enabled:
            msg = f"platform '{platform_name}' not configured/enabled"
            logger.warning("Job '%s': %s", job["id"], msg)
            delivery_errors.append(msg)
            continue
'''

NEW = SYNTH_BLOCK + RELAY_TAIL


def _replace_existing_synth_block(text: str) -> str | None:
    """Replace a previously patched synthesize block (any version) with CURRENT."""
    start = text.find(MARKER)
    if start == -1:
        return None
    line_start = text.rfind("\n", 0, start) + 1
    relay = text.find("\n        if transport is not None and transport.is_relay:", start)
    if relay == -1:
        return None
    return text[:line_start] + SYNTH_BLOCK + text[relay + 1 :]


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        # Upgrade: strip debug instrumentation and/or fix wrong get_secret import.
        needs_upgrade = (
            "_assistant_stack_dbg" in text
            or "from gateway.config import HomeChannel, PlatformConfig, get_secret" in text
            or "from agent.secret_scope import get_secret" not in text
        )
        if not needs_upgrade:
            print(f"already patched: {TARGET}")
            return 0
        upgraded = _replace_existing_synth_block(text)
        if upgraded is None:
            print(f"skip: could not upgrade existing synth block in {TARGET}")
            return 0
        TARGET.write_text(upgraded, encoding="utf-8")
        print(f"upgraded (removed debug instrumentation): {TARGET}")
        return 0
    if OLD not in text:
        print(f"skip: expected snippet not found in {TARGET}")
        return 0
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
