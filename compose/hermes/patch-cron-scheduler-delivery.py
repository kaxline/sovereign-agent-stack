#!/usr/bin/env python3
"""WebUI cron delivery overlays for scheduler_delivery.py (or legacy scheduler.py).

1. Reject undeliverable cron origins (api_server) and fall back to home channels
   (upstream #69304).
2. Synthesize Signal PlatformConfig from SIGNAL_* env when the browser adapter
   is disabled (same outbound-only model as send_message_tool).

Formerly patch-cron-api-server-origin.py + patch-cron-signal-synthesize.py.
Default deliver=signal at job create time remains patch-cron-default-deliver-signal.py.
"""
from __future__ import annotations

from pathlib import Path

CANDIDATES = (
    Path("/opt/hermes/cron/scheduler_delivery.py"),
    Path("/opt/hermes/cron/scheduler.py"),
)

ORIGIN_MARKER = "# assistant-stack: reject undeliverable cron origin (api_server)"
SYNTH_MARKER = "# assistant-stack: cron synthesize Signal from env when adapter disabled"

# --- Origin reject ---

ORIGIN_OLD_V9 = '''    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
                "_resolved_from": "origin",  # provenance for _target_mirror_eligible
            }
        # No origin (API/script job): fall back to a home channel instead of silently dropping.
'''

ORIGIN_NEW_V9 = '''    if deliver_value == "origin":
        if origin and _is_known_delivery_platform(origin.get("platform", "")):
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
                "_resolved_from": "origin",  # provenance for _target_mirror_eligible
            }
        # Origin missing OR points at a platform that cannot receive cron
        # delivery. The api_server trap (#69304): jobs created in WebUI/api_server
        # sessions capture origin.platform="api_server", but that adapter's
        # send() is a no-op. Treat undeliverable origins like missing ones.
        # assistant-stack: reject undeliverable cron origin (api_server)
        # No origin (API/script job): fall back to a home channel instead of silently dropping.
'''

ORIGIN_OLD_V8 = '''    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
            }
        # Origin missing (e.g. job created via API/script) — try each
        # platform's home channel as a fallback instead of silently dropping.
'''

ORIGIN_NEW_V8 = '''    if deliver_value == "origin":
        if origin and _is_known_delivery_platform(origin.get("platform", "")):
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
            }
        # Origin missing OR points at a platform that cannot receive cron
        # delivery. The api_server trap (#69304): jobs created in WebUI/api_server
        # sessions capture origin.platform="api_server", but that adapter's
        # send() is a no-op. Treat undeliverable origins like missing ones and
        # try each platform's home channel as a fallback instead of silently dropping.
        # assistant-stack: reject undeliverable cron origin (api_server)
'''

ORIGIN_OLD_MAIN = '''    if deliver_value == "origin":
        if origin:
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
                # Resolution provenance for mirror eligibility (see
                # _target_mirror_eligible): this IS the origin conversation.
                "_resolved_from": "origin",
            }
        # Origin missing (e.g. job created via API/script) — try each
        # platform's home channel as a fallback instead of silently dropping.
'''

ORIGIN_NEW_MAIN = '''    if deliver_value == "origin":
        if origin and _is_known_delivery_platform(origin.get("platform", "")):
            return {
                "platform": origin["platform"],
                "chat_id": str(origin["chat_id"]),
                "thread_id": _origin_delivery_thread(origin),
                # Resolution provenance for mirror eligibility (see
                # _target_mirror_eligible): this IS the origin conversation.
                "_resolved_from": "origin",
            }
        # Origin missing OR points at a platform that cannot receive cron
        # delivery. The api_server trap (#69304): jobs created in WebUI/api_server
        # sessions capture origin.platform="api_server", but that adapter's
        # send() is a no-op. Treat undeliverable origins like missing ones and
        # try each platform's home channel as a fallback instead of silently dropping.
        # assistant-stack: reject undeliverable cron origin (api_server)
'''

BARE_OLD_V9 = '''    if origin and origin.get("platform") == platform_name:
        chat_id = _get_home_target_chat_id(platform_name)
'''

BARE_NEW_V9 = '''    if origin and origin.get("platform") == platform_name and _is_known_delivery_platform(platform_name):
        chat_id = _get_home_target_chat_id(platform_name)
'''

BARE_OLD = '''    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name:
        chat_id = _get_home_target_chat_id(platform_name)
'''

BARE_NEW = '''    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name and _is_known_delivery_platform(platform_name):
        chat_id = _get_home_target_chat_id(platform_name)
'''

# --- Signal synth ---

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

OLD_V9 = '''    if transport is not None and (transport.is_relay or pconfig is None):
        # Relay transport carries the RELAY adapter's config (enablement already checked): the
        # logical platform is deliberately NOT natively enabled. A live NATIVE adapter with no
        # ``platforms.<p>`` block is the same shape — the owning process already authorized the
        # adapter; "no config" is not "disabled" (#89302).
        if pconfig is None:
            from gateway.config import PlatformConfig
            pconfig = PlatformConfig(enabled=True)
    elif not pconfig or not pconfig.enabled:
        return None, f"platform '{platform_name}' not configured/enabled"
'''

NEW_V9 = SYNTH_BLOCK + '''    if transport is not None and (transport.is_relay or pconfig is None):
        # Relay transport carries the RELAY adapter's config (enablement already checked): the
        # logical platform is deliberately NOT natively enabled. A live NATIVE adapter with no
        # ``platforms.<p>`` block is the same shape — the owning process already authorized the
        # adapter; "no config" is not "disabled" (#89302).
        if pconfig is None:
            from gateway.config import PlatformConfig
            pconfig = PlatformConfig(enabled=True)
    elif not pconfig or not pconfig.enabled:
        return None, f"platform '{platform_name}' not configured/enabled"
'''

OLD_LEGACY = '''        if transport is not None and transport.is_relay:
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

RELAY_TAIL_LEGACY = '''        if transport is not None and transport.is_relay:
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

NEW_LEGACY = SYNTH_BLOCK + RELAY_TAIL_LEGACY


def _replace_existing_synth_block(text: str) -> str | None:
    """Replace a previously patched synthesize block (any version) with CURRENT."""
    start = text.find(SYNTH_MARKER)
    if start == -1:
        return None
    line_start = text.rfind("\n", 0, start) + 1
    for needle in (
        "\n    if transport is not None and (transport.is_relay or pconfig is None):",
        "\n        if transport is not None and transport.is_relay:",
    ):
        relay = text.find(needle, start)
        if relay != -1:
            return text[:line_start] + SYNTH_BLOCK + text[relay + 1 :]
    return None


def _patch_origin(text: str, target: Path) -> tuple[str, str]:
    if ORIGIN_MARKER in text:
        return text, f"already patched (origin): {target}"

    if ORIGIN_OLD_V9 in text:
        text = text.replace(ORIGIN_OLD_V9, ORIGIN_NEW_V9, 1)
    elif ORIGIN_OLD_MAIN in text:
        text = text.replace(ORIGIN_OLD_MAIN, ORIGIN_NEW_MAIN, 1)
    elif ORIGIN_OLD_V8 in text:
        text = text.replace(ORIGIN_OLD_V8, ORIGIN_NEW_V8, 1)
    else:
        return text, f"skip: origin snippet not found in {target}"

    if BARE_OLD_V9 in text:
        text = text.replace(BARE_OLD_V9, BARE_NEW_V9, 1)
    elif BARE_OLD in text:
        text = text.replace(BARE_OLD, BARE_NEW, 1)
    else:
        return text, f"skip: bare-platform snippet not found in {target}"

    return text, f"patched (origin): {target}"


def _patch_synth(text: str, target: Path) -> tuple[str, str]:
    if SYNTH_MARKER in text:
        needs_upgrade = (
            "_assistant_stack_dbg" in text
            or "from gateway.config import HomeChannel, PlatformConfig, get_secret" in text
            or "from agent.secret_scope import get_secret" not in text
        )
        if not needs_upgrade:
            return text, f"already patched (synth): {target}"
        upgraded = _replace_existing_synth_block(text)
        if upgraded is None:
            return text, f"skip: could not upgrade existing synth block in {target}"
        return upgraded, f"upgraded (synth): {target}"
    if OLD_V9 in text:
        return text.replace(OLD_V9, NEW_V9, 1), f"patched (synth): {target}"
    if OLD_LEGACY in text:
        return text.replace(OLD_LEGACY, NEW_LEGACY, 1), f"patched (synth): {target}"
    return text, f"skip: synth snippet not found in {target}"


def main() -> int:
    target = next((p for p in CANDIDATES if p.is_file()), None)
    if target is None:
        print("skip: missing cron delivery module")
        return 0

    text = target.read_text(encoding="utf-8")
    text, msg_origin = _patch_origin(text, target)
    print(msg_origin)
    text, msg_synth = _patch_synth(text, target)
    print(msg_synth)

    changed = any(
        msg.startswith(("patched", "upgraded"))
        for msg in (msg_origin, msg_synth)
    )
    if changed:
        target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
