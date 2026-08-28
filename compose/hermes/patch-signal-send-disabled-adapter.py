#!/usr/bin/env python3
"""Allow Signal send_message when the gateway adapter is disabled.

WebUI runs on the browser profile. That profile needs SIGNAL_* credentials for
outbound send_message, but must keep platforms.signal.enabled=false so it does
not fight the default gateway for the Signal phone lock (--replace tears down
the whole browser gateway).

Upstream already synthesizes a Weixin PlatformConfig from env when the platform
is disabled. Mirror that for Signal.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("/opt/hermes/tools/send_message_tool.py")
MARKER = "# assistant-stack: synthesize Signal from env when adapter disabled"

OLD = '''        else:
            return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter
'''

NEW = '''        elif platform_name == "signal":
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


def main() -> int:
    if not TARGET.is_file():
        print(f"skip: missing {TARGET}")
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"already patched: {TARGET}")
        return 0
    if OLD not in text:
        print(f"skip: expected snippet not found in {TARGET}")
        return 0
    # Ensure os is imported in the module (it already is in upstream).
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched: {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
