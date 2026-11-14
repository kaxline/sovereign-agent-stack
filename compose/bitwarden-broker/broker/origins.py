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

import re
from urllib.parse import parse_qsl, urlsplit

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

_TOKEN_PARAM = re.compile(r"^(token|code|invite|invitation|key|secret|otp|magic|confirm|"
                          r"confirmation|reset|verify|verification|activation|auth|signature|sig|"
                          r"hash)$", re.I)

_SEGMENT_PARTS = re.compile(r"[-_.]+")

_SINGLE_USE = re.compile(r"^(invite|invites|invitation|accept|activate|activation|reset|verify|"
                         r"verification|confirm|confirmation|magic|token|tokens)$", re.I)

# The site's own login page, for an item whose URIs do not reach one.
# Five, not six: the origin root always takes a slot, and the ladder's cap is six
# navigations, so a sixth path here could never be reached.
WELL_KNOWN_LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/auth/login", "/users/sign_in")

MAX_URL = 8192  # the signer refuses a longer one (compose/bitwarden-signin/signer/server.py)


def single_use_uri(uri: str) -> bool:
    """True for a URI that looks like a one-time link rather than a reusable login page.

    Navigating to one can spend it, so the sign-in ladder never opens these: an
    invitation, a password reset, an email confirmation, a magic link. The marks
    are a path segment naming one of those flows (in whole or in part, so
    ``accept-invite`` counts), an opaque *last* segment of 20 characters or more
    with no dot in it (how such links carry their token), a query parameter named
    for a token, or a long opaque fragment.

    An opaque segment with more path after it is a tenant, realm or workspace id,
    as in Entra's ``/<tenant-guid>/oauth2/v2.0/authorize``. Those are reusable
    login pages, and treating them as spent would leave the item nothing to open.

    A query alone is not a mark. ``/login?next=/home`` and ``?returnUrl=`` are
    ordinary login URLs, and skipping them would skip the page the person saved.
    """
    value = (uri or "").strip() if isinstance(uri, str) else ""
    if not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    for name, _ in parse_qsl(parts.query, keep_blank_values=True):
        if _TOKEN_PARAM.match(name):
            return True
    if len(parts.fragment) >= 20 and "." not in parts.fragment:
        return True
    segments = [segment for segment in parts.path.split("/") if segment]
    for segment in segments:
        # accept-invite, reset-password, verify_email: the flow names itself in
        # part of a segment, so match the parts, not the segment whole.
        if any(_SINGLE_USE.match(part) for part in _SEGMENT_PARTS.split(segment)):
            return True
    # An opaque last segment is the link's token. An opaque segment with more path
    # after it is a tenant, realm or workspace id -- the Entra and Okta login URLs
    # are shaped that way -- and discarding those would leave the item no login page.
    if segments and len(segments[-1]) >= 20 and "." not in segments[-1]:
        return True
    return False


def login_candidates(uris, origin: str, limit: int = 6) -> list[str]:
    """URLs to try for ``origin``'s login page, best first and at most ``limit``.

    The item's own reusable URIs come first, since a saved ``/login`` is the page
    the person meant. Then the origin root, which most applications redirect to
    their login page, then the well-known paths.
    """
    root = (normalize_origin(origin) or "").rstrip("/")
    if not root:
        return []
    out: list[str] = []
    for uri in list(uris or ()):
        if isinstance(uri, str) and uri.strip() and not single_use_uri(uri):
            out.append(uri.strip())
    out.append(root + "/")
    out.extend(root + path for path in WELL_KNOWN_LOGIN_PATHS)
    seen: set[str] = set()
    picked: list[str] = []
    for url in out:
        key = url.rstrip("/") or url
        if key in seen or len(url) > MAX_URL:
            continue
        seen.add(key)
        picked.append(url)
        if len(picked) == limit:
            break
    return picked
