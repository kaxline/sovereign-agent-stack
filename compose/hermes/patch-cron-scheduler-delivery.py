#!/usr/bin/env python3
"""WebUI cron delivery overlays for cron/scheduler_delivery.py.

1. Reject undeliverable cron origins (api_server) and fall back to home channels
   (upstream #69304). Redundant on Hermes v0.22+, whose _resolve_origin already
   drops api_server origins (_NON_PUSH_ORIGIN_PLATFORMS); kept while it applies.
2. Synthesize Signal PlatformConfig from SIGNAL_* env when the browser adapter
   is disabled, through signal_env.py (installed here and by
   patch-send-message-tool.py, so both read the same secrets the same way).

Formerly patch-cron-api-server-origin.py + patch-cron-signal-synthesize.py.
"""
from __future__ import annotations

from pathlib import Path

from overlay_patch import ROOT, edit, install

SIGNAL_ENV_SRC = Path("/bootstrap/signal_env.py")
SIGNAL_ENV_DST = ROOT / "agent" / "signal_env.py"
TARGET = ROOT / "cron" / "scheduler_delivery.py"
TAG = "[patch-cron-scheduler-delivery]"

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


BARE_OLD_V9 = '''    if origin and origin.get("platform") == platform_name:
        chat_id = _get_home_target_chat_id(platform_name)
'''

BARE_NEW_V9 = '''    if origin and origin.get("platform") == platform_name and _is_known_delivery_platform(platform_name):
        chat_id = _get_home_target_chat_id(platform_name)
'''


# --- Signal synth ---

SYNTH_BLOCK = '''        # assistant-stack: cron synthesize Signal from env when adapter disabled
        if platform_name == "signal" and (not pconfig or not pconfig.enabled):
            from agent.signal_env import signal_env_pconfig
            pconfig = signal_env_pconfig() or pconfig

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


def main() -> None:
    edit(TARGET, [(ORIGIN_OLD_V9, ORIGIN_NEW_V9), (BARE_OLD_V9, BARE_NEW_V9)], ORIGIN_MARKER, TAG,
         label=f"(origin) {TARGET}")
    install(SIGNAL_ENV_SRC, SIGNAL_ENV_DST, TAG)
    edit(TARGET, [(OLD_V9, NEW_V9)], SYNTH_MARKER, TAG, label=f"(synth) {TARGET}")


if __name__ == "__main__":
    main()
