#!/usr/bin/env python3
"""Tell the model when a browser URL's hostname doesn't exist.

Compatibility overlay. With a remote browser (this stack drives host Chrome
over CDP), browser_exec refuses a URL whose hostname doesn't resolve, as it
should: is_safe_url fails closed on DNS errors. But it reports that with the
same text as a real private target, "Blocked: URL targets a private or
internal address". On 2026-11-05 a turn guessed csc.utah.gov, got that
error, read it as the browser being restricted, and went back to searxng.

This patches tools/browser_url_policy.py so that, when the private-address
check fails and the hostname has no DNS answer, the error names the hostname
and says the address is probably wrong. The URL is still blocked. Literal IPs
and hostnames that resolve to private addresses keep upstream's text.

Run scripts/test-browser-unresolvable-host.py on Hermes bumps.
"""
from __future__ import annotations

from overlay_patch import ROOT, edit

POLICY = ROOT / "tools" / "browser_url_policy.py"
TAG = "[patch-browser-unresolvable-host]"
MARKER = "# assistant-stack: name hostnames that don't resolve"

OLD = '        return _err("Blocked: URL targets a private or internal address")\n'
NEW = (
    '        return _unresolvable_host_error(url) or _err("Blocked: URL targets a private or internal address")\n'
)

HELPER = f'''

{MARKER}
def _unresolvable_host_error(url: str) -> Optional[dict]:
    """The blocked-URL error for a hostname with no DNS answer, else None (keep upstream's text)."""
    from tools import url_safety
    host = _url_hostname(url)
    if not host or _parse_ip(host) is not None:
        return None
    try:
        url_safety._getaddrinfo(host)
    except socket.gaierror:
        return _err(
            f"Blocked: the website {{host}} doesn't exist (no DNS record). The address is probably "
            "wrong or made up; search for the site's real URL instead of guessing it. The browser "
            "itself works."
        )
    except Exception:
        return None
    return None
'''


def main() -> None:
    # HELPER reads these upstream names; each must occur once.
    needed = ("\nimport socket\n", "    _parse_ip,\n", "    _url_hostname,\n", "from typing import Callable, Optional\n")
    edit(POLICY, [(OLD, NEW), *((n, n) for n in needed)], MARKER, TAG, append=HELPER)


if __name__ == "__main__":
    main()
