"""The browser providers the broker may write into, by name.

A fill or a sign-in handover names a provider (``browser`` in the request), never
an address: the agent cannot send the broker to an endpoint of its own. Each entry
holds the provider's CDP WebSocket URL for the credential client and the file
holding that client's token, both read when a call comes in, so rotating a token
or restarting a provider on another port needs no restart here.

``BROWSER_PROVIDERS`` is a JSON object, name to entry:

    {"stack": {"url": "ws://web-browser:9222/devtools/browser/credentials",
               "token_file": "/run/browser/stack.token"},
     "boundary": {"url_file": "/secrets/boundary.url",
                  "token_file": "/secrets/boundary.token",
                  "hosts": ["host.docker.internal", "host.containers.internal"]}}

``url`` is a fixed address (a compose service); ``url_file`` is for a provider
whose address its own client writes, such as Boundary's relay on a port it picks
at start. Exactly one of the two per entry. Neither file is ever logged.

``hosts``, optional, lists the host names an endpoint may name. The broker has a
route off the host, so a ``url_file`` naming another host would send the
provider's token there; with ``hosts`` set, such an endpoint reads as unusable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import cdp

NAME_MAX = 32
URL_MAX = 2048
TOKEN_MAX = 512


class ProviderError(Exception):
    """BROWSER_PROVIDERS cannot be read; the broker refuses to start."""


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    url_file: Path | None
    token_file: Path
    hosts: frozenset[str] = frozenset()

    def endpoint(self) -> str:
        """``<url>?token=<token>``, or "" when the URL or the token cannot be read.

        The caller reports ``cdp_unavailable``: a provider that is not running, or
        whose token file has not been written yet, is the same to a fill as a
        browser that does not answer.
        """
        url = self.url
        if self.url_file is not None:
            url = _read(self.url_file, URL_MAX)
        token = _read(self.token_file, TOKEN_MAX)
        if not url or not token or not cdp.valid_endpoint(url):
            return ""
        if self.hosts and (urlsplit(url).hostname or "").lower() not in self.hosts:
            return ""
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}token={quote(token, safe='')}"


def _read(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            value = handle.read(limit + 1)
    except (OSError, UnicodeDecodeError):
        return ""
    if len(value) > limit:
        return ""
    value = value.strip()
    return "" if any(c.isspace() for c in value) else value


def load(raw: str | None) -> dict[str, Provider]:
    """The provider table from ``BROWSER_PROVIDERS``; ``{}`` when it is unset or empty.

    An entry that cannot be read stops the broker rather than leaving a name that
    silently refuses every fill.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        table = json.loads(text)
    except ValueError:
        raise ProviderError("BROWSER_PROVIDERS must be a JSON object of provider entries") from None
    if not isinstance(table, dict):
        raise ProviderError("BROWSER_PROVIDERS must be a JSON object of provider entries")
    out = {}
    for name, entry in table.items():
        out[name] = _entry(name, entry)
    return out


def _entry(name, entry) -> Provider:
    if not isinstance(name, str) or not name or len(name) > NAME_MAX or not name.replace("-", "").isalnum():
        raise ProviderError(f"BROWSER_PROVIDERS: {name!r} is not a usable provider name")
    if not isinstance(entry, dict):
        raise ProviderError(f"BROWSER_PROVIDERS: {name} must be an object")
    extra = set(entry) - {"url", "url_file", "token_file", "hosts"}
    if extra:
        raise ProviderError(f"BROWSER_PROVIDERS: {name} has no field {', '.join(sorted(extra))}")
    url, url_file = entry.get("url") or "", entry.get("url_file") or ""
    if bool(url) == bool(url_file):
        raise ProviderError(f"BROWSER_PROVIDERS: {name} needs either url or url_file, not both")
    if url and not cdp.valid_endpoint(url):
        raise ProviderError(f"BROWSER_PROVIDERS: {name}'s url is not a "
                            "ws://host:port/devtools/browser/<client> endpoint")
    token_file = entry.get("token_file") or ""
    if not isinstance(token_file, str) or not token_file.startswith("/"):
        raise ProviderError(f"BROWSER_PROVIDERS: {name} needs token_file, an absolute path")
    if url_file and (not isinstance(url_file, str) or not url_file.startswith("/")):
        raise ProviderError(f"BROWSER_PROVIDERS: {name}'s url_file must be an absolute path")
    hosts = entry.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h and h == h.strip() for h in hosts):
        raise ProviderError(f"BROWSER_PROVIDERS: {name}'s hosts must be a list of host names")
    hosts = frozenset(h.lower() for h in hosts)
    if url and hosts and (urlsplit(url).hostname or "").lower() not in hosts:
        raise ProviderError(f"BROWSER_PROVIDERS: {name}'s url names a host outside its hosts")
    return Provider(name=name, url=url, url_file=Path(url_file) if url_file else None,
                    token_file=Path(token_file), hosts=hosts)
