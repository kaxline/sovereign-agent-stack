"""Pick the cookies that belong to a vault item's sign-in origins.

A cookie is kept when its domain (leading dot stripped) equals the host of an
item origin, or is a parent domain of that host and is a domain cookie. CDP
reports host-only cookies without a leading dot and domain cookies with one.
A single-label domain such as ``com`` is never a parent match.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_KEEP = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def normalize_origin(url: str) -> str | None:
    """Copy of broker/origins.py:normalize_origin (the signer cannot import the broker)."""
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


def _hosts(item_origins) -> set[str]:
    hosts = set()
    for origin in item_origins:
        norm = normalize_origin(origin)
        if norm:
            hosts.add((urlsplit(norm).hostname or "").lower())
    hosts.discard("")
    return hosts


def select(cookies: list[dict], item_origins: tuple[str, ...]) -> list[dict]:
    hosts = _hosts(item_origins)
    out = []
    for cookie in cookies:
        raw = str(cookie.get("domain", "")).lower()
        domain_cookie = raw.startswith(".")
        domain = raw.lstrip(".").rstrip(".")
        if not domain:
            continue
        keep = domain in hosts
        if not keep and domain_cookie and "." in domain:
            keep = any(h.endswith("." + domain) for h in hosts)
        if keep:
            out.append({k: cookie.get(k) for k in _KEEP if k in cookie})
    return out
