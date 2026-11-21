"""Signal PlatformConfig built from SIGNAL_* secrets, for when the Signal adapter is off.

Compatibility overlay (not upstream). The browser profile's Signal adapter is disabled
(the default profile owns the SSE lock), so outbound sends need their config built from
the profile's secrets. patch-send-message-tool.py (send_message) and
patch-cron-scheduler-delivery.py (cron delivery) both install this file as
agent/signal_env.py and call signal_env_pconfig().

Every value goes through get_secret, so the active profile's secret scope wins and the
process env is read only where Hermes allows it (never another profile's under the
multiplexed gateway). Cron fires inside its owner profile's scope.
"""
from __future__ import annotations


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret

    return (get_secret(name, "") or "").strip()


def signal_env_pconfig():
    """A Signal PlatformConfig from SIGNAL_HTTP_URL and SIGNAL_ACCOUNT, or None when either is unset.

    SIGNAL_HOME_CHANNEL sets the home channel, named by SIGNAL_HOME_CHANNEL_NAME (default "Home").
    """
    url = _secret("SIGNAL_HTTP_URL")
    account = _secret("SIGNAL_ACCOUNT")
    if not (url and account):
        return None
    from gateway.config import HomeChannel, Platform, PlatformConfig

    pconfig = PlatformConfig(enabled=True, extra={"http_url": url, "account": account})
    home = _secret("SIGNAL_HOME_CHANNEL")
    if home:
        pconfig.home_channel = HomeChannel(
            platform=Platform.SIGNAL,
            chat_id=home,
            name=_secret("SIGNAL_HOME_CHANNEL_NAME") or "Home",
        )
    return pconfig
