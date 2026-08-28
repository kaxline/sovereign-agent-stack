#!/usr/bin/env python3
"""Reject undeliverable cron origins (api_server) and fall back to home channels.

WebUI jobs capture origin.platform=api_server, but that adapter cannot send().
Upstream #69304; ported from PR #69384 target resolver guard.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/cron/scheduler.py")
MARKER = "# assistant-stack: reject undeliverable cron origin (api_server)"

# v2026.8.x scheduler (no _resolved_from on origin target).
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

# Newer main-branch scheduler (_resolved_from + mirror comment).
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

ORIGIN_NEW = '''    if deliver_value == "origin":
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

BARE_OLD = '''    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name:
        chat_id = _get_home_target_chat_id(platform_name)
'''

BARE_NEW = '''    platform_name = deliver_value
    if origin and origin.get("platform") == platform_name and _is_known_delivery_platform(platform_name):
        chat_id = _get_home_target_chat_id(platform_name)
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if ORIGIN_OLD_V8 in text:
        text = text.replace(ORIGIN_OLD_V8, ORIGIN_NEW, 1)
    elif ORIGIN_OLD_MAIN in text:
        text = text.replace(ORIGIN_OLD_MAIN, ORIGIN_NEW_MAIN, 1)
    else:
        print(f"skip: origin snippet not found in {TARGET}")
        return 0
    if BARE_OLD not in text:
        print(f"skip: bare-platform snippet not found in {TARGET}")
        return 0
    text = text.replace(BARE_OLD, BARE_NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
