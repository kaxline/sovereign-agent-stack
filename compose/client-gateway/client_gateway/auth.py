"""Settings from the environment, the bearer check and the Origin check."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field

MIN_TOKEN_LEN = 32


class SettingsError(Exception):
    """The environment can't start a gateway; the message says why."""


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    host: str = "0.0.0.0"
    port: int = 8650
    hermes_url: str = "http://hermes:9119"
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    validate_frames: bool = False


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    token = env.get("CLIENT_API_TOKEN", "")
    if not token:
        raise SettingsError("CLIENT_API_TOKEN is not set; run scripts/setup.sh or set it in .env")
    if len(token) < MIN_TOKEN_LEN:
        raise SettingsError(
            f"CLIENT_API_TOKEN is {len(token)} characters; it needs at least {MIN_TOKEN_LEN}"
        )
    origins = frozenset(
        o.strip().rstrip("/") for o in env.get("CLIENT_API_ALLOWED_ORIGINS", "").split(",") if o.strip()
    )
    return Settings(
        token=token,
        allowed_origins=origins,
        host=env.get("CLIENT_GATEWAY_HOST", "0.0.0.0"),
        port=int(env.get("CLIENT_GATEWAY_LISTEN_PORT", "8650")),
        hermes_url=env.get("CLIENT_GATEWAY_HERMES_URL", "http://hermes:9119").rstrip("/"),
        dashboard_user=env.get("HERMES_DASHBOARD_USER", "admin"),
        dashboard_password=env.get("HERMES_DASHBOARD_PASSWORD", ""),
        validate_frames=env.get("CLIENT_GATEWAY_VALIDATE", "0") == "1",
    )


def bearer_ok(authorization: str | None, token: str) -> bool:
    """True when the header is ``Bearer <token>``, compared in constant time."""
    if not authorization:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def origin_ok(origin: str | None, allowed: frozenset[str]) -> bool:
    """No Origin (Electron main, native clients) passes; a browser origin must be listed."""
    if origin is None:
        return True
    return origin.rstrip("/") in allowed
