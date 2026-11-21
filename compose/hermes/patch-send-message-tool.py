#!/usr/bin/env python3
"""WebUI send_message overlays for send_message_tool.py + toolsets.py.

1. Synthesize Signal PlatformConfig from SIGNAL_* env when the browser
   adapter is disabled (default owns the SSE lock; WebUI still needs outbound).
   Installs signal_env.py (shared with patch-cron-scheduler-delivery.py) and
   calls it from a _signal_env_pconfig helper beside Weixin's.
2. Re-register send_message as an agent-callable tool and include it in core
   toolsets (upstream leaves it unregistered on purpose).

Formerly patch-signal-send-disabled-adapter.py + patch-reregister-send-message.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

SIGNAL_ENV_SRC = Path("/bootstrap/signal_env.py")
SIGNAL_ENV_DST = Path("/opt/hermes/agent/signal_env.py")

# v0.23.2 moved tools/send_message_tool.py to gateway/outbound/send.py unchanged
# where these anchors sit.
TOOL_TARGET = next((p for p in (Path("/opt/hermes/tools/send_message_tool.py"),
                                Path("/opt/hermes/gateway/outbound/send.py")) if p.is_file()),
                   Path("/opt/hermes/gateway/outbound/send.py"))
TOOLSETS_TARGET = Path("/opt/hermes/toolsets.py")

SIGNAL_MARKER = "# assistant-stack: synthesize Signal from env when adapter disabled"
TOOL_MARKER = "# assistant-stack: re-register send_message for agent"
TOOLSETS_MARKER = "# assistant-stack: include send_message in core tools"

# --- Signal env synthesis (helper next to Weixin) ---

WEIXIN_HELPER = '''def _weixin_env_pconfig():
    """Synthesize a Weixin PlatformConfig from .env secrets, or None."""
    wx_token = get_secret("WEIXIN_TOKEN", "").strip()
    wx_account = get_secret("WEIXIN_ACCOUNT_ID", "").strip()
    if not (wx_token and wx_account):
        return None
    from gateway.config import PlatformConfig
    return PlatformConfig(enabled=True, token=wx_token, extra={
        "account_id": wx_account, "base_url": get_secret("WEIXIN_BASE_URL", "").strip(),
        "cdn_base_url": get_secret("WEIXIN_CDN_BASE_URL", "").strip()})


'''

SIGNAL_HELPER = '''def _weixin_env_pconfig():
    """Synthesize a Weixin PlatformConfig from .env secrets, or None."""
    wx_token = get_secret("WEIXIN_TOKEN", "").strip()
    wx_account = get_secret("WEIXIN_ACCOUNT_ID", "").strip()
    if not (wx_token and wx_account):
        return None
    from gateway.config import PlatformConfig
    return PlatformConfig(enabled=True, token=wx_token, extra={
        "account_id": wx_account, "base_url": get_secret("WEIXIN_BASE_URL", "").strip(),
        "cdn_base_url": get_secret("WEIXIN_CDN_BASE_URL", "").strip()})


def _signal_env_pconfig():
    """Synthesize a Signal PlatformConfig from env when adapter is disabled."""
    # assistant-stack: synthesize Signal from env when adapter disabled
    from agent.signal_env import signal_env_pconfig
    return signal_env_pconfig()


'''

CALL_OLD = '''    if not pconfig or not pconfig.enabled:
        pconfig = _weixin_env_pconfig() if platform_name == "weixin" else None
'''

CALL_NEW = '''    if not pconfig or not pconfig.enabled:
        if platform_name == "weixin":
            pconfig = _weixin_env_pconfig()
        elif platform_name == "signal":
            pconfig = _signal_env_pconfig()
        else:
            pconfig = None
'''

# --- Re-register send_message ---
# v0.22 deleted SEND_MESSAGE_SCHEMA from send_message_tool.py (it lived in the
# plugin-compat block), so the overlay carries the schema itself.

REGISTER_BLOCK = '''from tools.registry import registry, tool_error

SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\\n\\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\\n"
        "If the user just says a platform name like 'send to telegram', send directly "
        "to the home channel without listing first.\\n"
        "Signal: when the user asks for a Signal message, call this with target='signal' "
        "(their Signal home channel) even if Signal is not among the connected platforms "
        "for this chat; delivery is set up separately. Don't suggest another platform "
        "unless the call fails."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list", "react", "unreact"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms. 'react' attaches an emoji reaction to a message (platforms that support it, e.g. photon/iMessage tapbacks). 'unreact' retracts a previously-added reaction."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', 'platform:chat_id', or 'platform:chat_id:thread_id' for Telegram topics and Discord threads. Examples: 'signal', 'signal:+155****4567', 'telegram:-1001234567890:17585', 'discord:#bot-home', 'slack:#engineering', 'matrix:!roomid:server.org'"
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/report.pdf') in the message; the platform delivers it as a native media attachment."
            },
            "emoji": {
                "type": "string",
                "description": "For action='react': the emoji to react with."
            },
            "message_id": {
                "type": "string",
                "description": "For action='react'/'unreact': id of the message to react to. Omit to target the most recent message received in that chat."
            }
        },
        "required": []
    }
}

# assistant-stack: re-register send_message for agent
# Upstream leaves this unregistered; WebUI (hermes-cli) needs the model tool
# so users can ask to deliver to Signal and other platforms. Cron / hermes send
# still import the helpers directly.
registry.register(
    name="send_message",
    toolset="send_message",
    schema=SEND_MESSAGE_SCHEMA,
    handler=lambda args, **kw: send_message_tool(args, **kw),
    check_fn=lambda: True,  # always expose in WebUI/api_server tool lists
    emoji="📨",
)

'''

# v0.22.3 put _SLACK_USER_ID_RE between the tool_error import and this note, so
# anchor on the note alone; REGISTER_BLOCK imports registry itself.
NOTE = '''# NOTE: ``send_message`` is intentionally NOT registered as an agent-callable model tool
# (the agent must not fire cross-platform messages on its own); cron delivery, the
# ``hermes send`` CLI, the kanban notifier and the opt-in MCP server import the helpers.

'''

TOOLSETS_OLD = '''    "clarify",
    "execute_code", "delegate_task",
'''

TOOLSETS_NEW = '''    "clarify",
    "send_message",  # assistant-stack: include send_message in core tools
    "execute_code", "delegate_task",
'''


def _install_signal_env() -> str | None:
    """Copy signal_env.py into the agent package; a skip message when it can't."""
    if not SIGNAL_ENV_SRC.is_file():
        return f"skip: missing {SIGNAL_ENV_SRC}"
    shutil.copyfile(SIGNAL_ENV_SRC, SIGNAL_ENV_DST)
    return None


def _patch_signal_synth() -> str:
    if not TOOL_TARGET.is_file():
        return f"skip: missing {TOOL_TARGET}"
    skip = _install_signal_env()
    if skip:
        return skip
    text = TOOL_TARGET.read_text(encoding="utf-8")
    if SIGNAL_MARKER in text:
        return f"already patched (signal synth): {TOOL_TARGET}"
    if WEIXIN_HELPER in text and CALL_OLD in text:
        text = text.replace(WEIXIN_HELPER, SIGNAL_HELPER, 1)
        text = text.replace(CALL_OLD, CALL_NEW, 1)
        TOOL_TARGET.write_text(text, encoding="utf-8")
        return f"patched (signal synth): {TOOL_TARGET}"
    return f"skip: signal synth snippet not found in {TOOL_TARGET}"


def _patch_reregister() -> str:
    if not TOOL_TARGET.is_file():
        return f"skip: missing {TOOL_TARGET}"
    text = TOOL_TARGET.read_text(encoding="utf-8")
    if TOOL_MARKER in text:
        return f"already patched (reregister): {TOOL_TARGET}"
    if "SEND_MESSAGE_SCHEMA = {" in text:
        return f"skip: upstream SEND_MESSAGE_SCHEMA present again, re-check {TOOL_TARGET}"
    if NOTE not in text:
        return f"skip: reregister snippet not found in {TOOL_TARGET}"
    TOOL_TARGET.write_text(text.replace(NOTE, REGISTER_BLOCK, 1), encoding="utf-8")
    return f"patched (reregister): {TOOL_TARGET}"


def _patch_toolsets() -> str:
    if not TOOLSETS_TARGET.is_file():
        return f"skip: missing {TOOLSETS_TARGET}"
    text = TOOLSETS_TARGET.read_text(encoding="utf-8")
    if TOOLSETS_MARKER in text:
        return f"already patched: {TOOLSETS_TARGET}"
    if TOOLSETS_OLD in text:
        TOOLSETS_TARGET.write_text(text.replace(TOOLSETS_OLD, TOOLSETS_NEW, 1), encoding="utf-8")
        return f"patched: {TOOLSETS_TARGET}"
    return f"skip: expected snippet not found in {TOOLSETS_TARGET}"


def main() -> int:
    print(_patch_signal_synth())
    print(_patch_reregister())
    print(_patch_toolsets())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
