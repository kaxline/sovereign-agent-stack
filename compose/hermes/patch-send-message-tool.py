#!/usr/bin/env python3
"""WebUI send_message overlays for send_message_tool.py + toolsets.py.

1. Synthesize Signal PlatformConfig from SIGNAL_* env when the browser
   adapter is disabled (default owns the SSE lock; WebUI still needs outbound).
2. Re-register send_message as an agent-callable tool and include it in core
   toolsets (upstream leaves it unregistered on purpose).

Formerly patch-signal-send-disabled-adapter.py + patch-reregister-send-message.py.
"""
from __future__ import annotations

from pathlib import Path

TOOL_TARGET = Path("/opt/hermes/tools/send_message_tool.py")
TOOLSETS_TARGET = Path("/opt/hermes/toolsets.py")

SIGNAL_MARKER = "# assistant-stack: synthesize Signal from env when adapter disabled"
TOOL_MARKER = "# assistant-stack: re-register send_message for agent"
TOOLSETS_MARKER = "# assistant-stack: include send_message in core tools"

# --- Signal env synthesis (v2026.9+ helper next to Weixin) ---

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
    signal_url = (
        os.getenv("SIGNAL_HTTP_URL", "") or get_secret("SIGNAL_HTTP_URL", "")
    ).strip()
    signal_account = (
        os.getenv("SIGNAL_ACCOUNT", "") or get_secret("SIGNAL_ACCOUNT", "")
    ).strip()
    if not (signal_url and signal_account):
        return None
    from gateway.config import HomeChannel, PlatformConfig
    pconfig = PlatformConfig(
        enabled=True,
        extra={"http_url": signal_url, "account": signal_account},
    )
    signal_home = (
        os.getenv("SIGNAL_HOME_CHANNEL", "") or get_secret("SIGNAL_HOME_CHANNEL", "")
    ).strip()
    if signal_home:
        from gateway.config import Platform
        pconfig.home_channel = HomeChannel(
            platform=Platform.SIGNAL,
            chat_id=signal_home,
            name=os.getenv("SIGNAL_HOME_CHANNEL_NAME", "Home"),
        )
    return pconfig


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

OLD_LEGACY = '''        else:
            return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter
'''

NEW_LEGACY = '''        elif platform_name == "signal":
            # assistant-stack: synthesize Signal from env when adapter disabled
            # Browser/WebUI keeps platforms.signal.enabled=false so default owns
            # the SSE lock; outbound send_message still needs credentials.
            signal_url = (
                os.getenv("SIGNAL_HTTP_URL", "") or get_secret("SIGNAL_HTTP_URL", "")
            ).strip()
            signal_account = (
                os.getenv("SIGNAL_ACCOUNT", "") or get_secret("SIGNAL_ACCOUNT", "")
            ).strip()
            if signal_url and signal_account:
                from gateway.config import PlatformConfig
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
                    from gateway.config import HomeChannel
                    pconfig.home_channel = HomeChannel(
                        platform=platform,
                        chat_id=signal_home,
                        name=os.getenv("SIGNAL_HOME_CHANNEL_NAME", "Home"),
                    )
            else:
                return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")
        else:
            return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter
'''

# --- Re-register send_message ---

REGISTER_BLOCK = '''
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

NOTE_V9 = '''from tools.registry import tool_error

# NOTE: ``send_message`` is intentionally NOT registered as an agent-callable model tool
# (the agent must not fire cross-platform messages on its own); cron delivery, the
# ``hermes send`` CLI, the kanban notifier and the opt-in MCP server import the helpers.

'''

NOTE_V9_REPLACED = '''from tools.registry import registry, tool_error

'''

NOTE_LEGACY = '''# --- Registry ---
from tools.registry import tool_error

# NOTE: ``send_message`` is intentionally NOT registered as an agent-callable
# model tool. The agent should not decide on its own to fire off cross-platform
# messages or reactions. The send engine in this module (``_send_to_platform``,
# ``_send_via_adapter``, ``_parse_target_ref``, the per-platform ``_send_*``
# helpers) remains the shared transport used by:
#   - cron delivery (cron/scheduler.py)
#   - the ``hermes send`` CLI command (hermes_cli/send_cmd.py)
#   - the gateway kanban notifier (dashboard-toggled, outside agent control)
#   - the standalone MCP server (mcp_serve.py), which is an opt-in surface
# Those callers import the helpers directly; none of them need the registry
# entry.
'''

NOTE_LEGACY_REPLACED = '''# --- Registry ---
from tools.registry import registry, tool_error
''' + REGISTER_BLOCK

SCHEMA_TAIL = '''        "required": []
    }
}


_PLUGIN_COMPAT_LAZY = {
'''

SCHEMA_TAIL_WITH_REGISTER = (
    '''        "required": []
    }
}

'''
    + REGISTER_BLOCK
    + '''
_PLUGIN_COMPAT_LAZY = {
'''
)

TOOLSETS_OLD = '''    "clarify",
    "execute_code", "delegate_task",
'''

TOOLSETS_NEW = '''    "clarify",
    "send_message",  # assistant-stack: include send_message in core tools
    "execute_code", "delegate_task",
'''

TOOLSETS_OLD_LEGACY = '''    # Clarifying questions
    "clarify",
    # Code execution + delegation
'''

TOOLSETS_NEW_LEGACY = '''    # Clarifying questions
    "clarify",
    "send_message",  # assistant-stack: include send_message in core tools
    # Code execution + delegation
'''


def _patch_signal_synth() -> str:
    if not TOOL_TARGET.is_file():
        return f"skip: missing {TOOL_TARGET}"
    text = TOOL_TARGET.read_text(encoding="utf-8")
    if SIGNAL_MARKER in text:
        return f"already patched (signal synth): {TOOL_TARGET}"
    if WEIXIN_HELPER in text and CALL_OLD in text:
        text = text.replace(WEIXIN_HELPER, SIGNAL_HELPER, 1)
        text = text.replace(CALL_OLD, CALL_NEW, 1)
        TOOL_TARGET.write_text(text, encoding="utf-8")
        return f"patched (signal synth): {TOOL_TARGET}"
    if OLD_LEGACY in text:
        TOOL_TARGET.write_text(text.replace(OLD_LEGACY, NEW_LEGACY, 1), encoding="utf-8")
        return f"patched (signal synth legacy): {TOOL_TARGET}"
    return f"skip: signal synth snippet not found in {TOOL_TARGET}"


def _patch_reregister() -> str:
    if not TOOL_TARGET.is_file():
        return f"skip: missing {TOOL_TARGET}"
    text = TOOL_TARGET.read_text(encoding="utf-8")
    if TOOL_MARKER in text and "schema=SEND_MESSAGE_SCHEMA" in text:
        marker_pos = text.find(TOOL_MARKER)
        schema_pos = text.find("SEND_MESSAGE_SCHEMA = {")
        if schema_pos != -1 and marker_pos < schema_pos:
            import re as _re

            text = _re.sub(
                r"\n?# assistant-stack: re-register send_message for agent\n"
                r".*?registry\.register\(\n"
                r".*?emoji=\"📨\",\n"
                r"\)\n+",
                "\n",
                text,
                count=1,
                flags=_re.DOTALL,
            )
            if "from tools.registry import registry, tool_error" not in text:
                text = text.replace(
                    "from tools.registry import tool_error",
                    "from tools.registry import registry, tool_error",
                    1,
                )
        else:
            return f"already patched (reregister): {TOOL_TARGET}"

    if NOTE_LEGACY in text:
        TOOL_TARGET.write_text(text.replace(NOTE_LEGACY, NOTE_LEGACY_REPLACED, 1), encoding="utf-8")
        return f"patched (reregister): {TOOL_TARGET}"

    if NOTE_V9 in text:
        text = text.replace(NOTE_V9, NOTE_V9_REPLACED, 1)
        if SCHEMA_TAIL not in text:
            TOOL_TARGET.write_text(text, encoding="utf-8")
            return f"skip: schema tail not found after note strip in {TOOL_TARGET}"
        text = text.replace(SCHEMA_TAIL, SCHEMA_TAIL_WITH_REGISTER, 1)
        TOOL_TARGET.write_text(text, encoding="utf-8")
        return f"patched (reregister): {TOOL_TARGET}"

    if TOOL_MARKER not in text and SCHEMA_TAIL in text:
        if "from tools.registry import registry" not in text:
            text = text.replace(
                "from tools.registry import tool_error",
                "from tools.registry import registry, tool_error",
                1,
            )
        text = text.replace(SCHEMA_TAIL, SCHEMA_TAIL_WITH_REGISTER, 1)
        TOOL_TARGET.write_text(text, encoding="utf-8")
        return f"patched (reregister): {TOOL_TARGET}"

    return f"skip: reregister snippet not found in {TOOL_TARGET}"


def _patch_toolsets() -> str:
    if not TOOLSETS_TARGET.is_file():
        return f"skip: missing {TOOLSETS_TARGET}"
    text = TOOLSETS_TARGET.read_text(encoding="utf-8")
    if TOOLSETS_MARKER in text:
        return f"already patched: {TOOLSETS_TARGET}"
    if TOOLSETS_OLD in text:
        TOOLSETS_TARGET.write_text(text.replace(TOOLSETS_OLD, TOOLSETS_NEW, 1), encoding="utf-8")
        return f"patched: {TOOLSETS_TARGET}"
    if TOOLSETS_OLD_LEGACY in text:
        TOOLSETS_TARGET.write_text(
            text.replace(TOOLSETS_OLD_LEGACY, TOOLSETS_NEW_LEGACY, 1), encoding="utf-8"
        )
        return f"patched: {TOOLSETS_TARGET}"
    return f"skip: expected snippet not found in {TOOLSETS_TARGET}"


def main() -> int:
    print(_patch_signal_synth())
    print(_patch_reregister())
    print(_patch_toolsets())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
