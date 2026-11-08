"""Origin normalization, the same rules as Hermes core's ``agent.vault_store.normalize_origin``.

Core raises on a value it cannot parse; this returns None instead, so a caller
can skip an item URI that is not a URL. scripts/test-bitwarden-broker.py checks
the cases, and Task 8's test compares the two functions inside the Hermes image.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(url: str) -> str | None:
    """Return ``scheme://host[:port]`` with the default port dropped, or None."""
    value = (url or "").strip() if isinstance(url, str) else ""
    if not value or "://" not in value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"
