"""Origin normalization, the same rules as Hermes core's ``agent.vault_store.normalize_origin``.

Core raises on a value it cannot parse; this returns None instead, so a caller
can skip an item URI that is not a URL. scripts/test-bitwarden-broker.py checks
the cases, and Task 8's test compares the two functions inside the Hermes image.

``cookie_domain_allowed`` is the second copy of the signer's cookie rule
(compose/bitwarden-signin/signer/cookies.py:select), which the broker cannot
import: the two run in different containers with no shared package. The handover
is the one call that writes into the agent's browser, so it applies the rule
again rather than trusting what the signer sent.
scripts/test-bitwarden-broker.py compares the two over one jar, as
scripts/test-bitwarden-signer.py does for the scrub encodings.
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


def origin_hosts(origins) -> set[str]:
    """The hosts of ``origins``, normalized, for ``cookie_domain_allowed``."""
    hosts = set()
    for origin in origins or ():
        norm = normalize_origin(origin)
        if norm:
            hosts.add((urlsplit(norm).hostname or "").lower())
    hosts.discard("")
    return hosts


def cookie_domain_allowed(domain, hosts: set[str]) -> bool:
    """True for a cookie domain that belongs to one of ``hosts``.

    The domain (leading dot stripped) equals a host, or is a parent domain of one
    and the cookie is a domain cookie, which CDP reports with a leading dot. A
    single-label domain such as ``com`` is never a parent match.
    """
    raw = str(domain or "").lower()
    domain_cookie = raw.startswith(".")
    name = raw.lstrip(".").rstrip(".")
    if not name:
        return False
    if name in hosts:
        return True
    return domain_cookie and "." in name and any(h.endswith("." + name) for h in hosts)
