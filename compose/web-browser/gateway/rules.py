"""What each client may call, from the browser contract (docs/contracts/browser.md).

Two clients, each with its own token and its own rules:

- ``stack`` is the agent runtime (Hermes' driver and its dialog supervisor). It
  gets the contract's refused list: whole-jar cookie reads and writes, the
  storage domains, ``Fetch`` except the dialog-bridge pattern, ``IO.read`` and
  the two ``Target`` escapes out of the gateway. Everything else passes, so a
  Hermes release that calls a new method is not broken by this file.
- ``credentials`` is the credential backend (the Bitwarden broker). It gets an
  allowlist instead: the handful of methods a fill and a sign-in handover need.
  ``Network.setCookies`` is on it, which the agent client is refused, because the
  handover writes the signer's session into the tab; the broker holds the value,
  the agent never does (docs/bitwarden.md).

``Browser.close`` is answered by the gateway for both: that client's connection
goes, the browser stays. Navigation to ``file:`` and the browser's own schemes is
refused for both, so a target can never be opened on the provider's filesystem.

Stdlib only, so scripts/test-web-browser-gateway.py checks it on the host.
"""

from __future__ import annotations

AGENT = "agent"
CREDENTIALS = "credentials"

# Path name -> role. The name is the ``<name>`` in
# ws://<host>:<port>/devtools/browser/<name>?token=<token>.
CLIENTS = {"stack": AGENT, "credentials": CREDENTIALS}

ALLOW = "allow"
ANSWER = "answer"
REFUSE = "refuse"

# Answered by the gateway itself, never forwarded.
ANSWERED = {"Browser.close"}

# Hermes installs the bridge with the pattern "http://hermes-dialog-bridge.invalid/*"
# (tools/browser_supervisor_dialogs.py, DIALOG_BRIDGE_URL_PATTERN), so the rule is
# the host prefix rather than one exact string. A CDP urlPattern matches the whole
# request URL, so a pattern starting with this prefix can only ever pause a request
# to that host.
DIALOG_BRIDGE_PREFIX = "http://hermes-dialog-bridge.invalid/"

# Whole domains the agent client cannot reach.
_AGENT_REFUSED_DOMAINS = {"DOMStorage", "IndexedDB", "CacheStorage"}

# Named methods the agent client cannot reach.
_AGENT_REFUSED = {
    "Network.getAllCookies", "Network.getCookies", "Network.setCookie", "Network.setCookies",
    "Network.deleteCookies", "Network.clearBrowserCookies",
    "Storage.getCookies", "Storage.setCookies", "Storage.clearCookies",
    "Storage.clearDataForOrigin", "Storage.clearDataForStorageKey",
    "IO.read",
    "Target.exposeDevToolsProtocol", "Target.attachToBrowserTarget",
}

# Fetch methods that only answer a request already paused by an allowed
# Fetch.enable. Anything else in the domain is refused, auth included.
_FETCH_ALLOWED = {"Fetch.disable", "Fetch.continueRequest", "Fetch.continueResponse",
                  "Fetch.fulfillRequest", "Fetch.failRequest"}

# The credential backend's whole vocabulary: attach, read and write the one
# page a fill or a handover names, then detach.
_CREDENTIALS_ALLOWED = {
    "Target.attachToTarget", "Target.detachFromTarget", "Target.getTargetInfo",
    "Runtime.evaluate", "Runtime.callFunctionOn",
    "Network.setCookies", "Page.reload",
}

# Methods that take a URL the browser then opens.
_NAVIGATION = {"Page.navigate", "Target.createTarget"}

# Schemes a navigation may never name. ``view-source:`` wraps another URL, so it
# goes with them rather than being unwrapped.
_REFUSED_SCHEMES = ("file:", "chrome:", "chrome-extension:", "chrome-untrusted:",
                    "chrome-search:", "chrome-devtools:", "devtools:", "view-source:")


def _url_verdict(params) -> tuple[str, str]:
    url = params.get("url") if isinstance(params, dict) else None
    if url is None or url == "":
        # Page.navigate without a URL is a protocol error; the browser says so.
        return ALLOW, "ok"
    if not isinstance(url, str):
        return REFUSE, "url_unreadable"
    scheme = url.strip().lower()
    if scheme.startswith(_REFUSED_SCHEMES):
        return REFUSE, "url_refused"
    return ALLOW, "ok"


def _fetch_verdict(method: str, params) -> tuple[str, str]:
    if method in _FETCH_ALLOWED:
        return ALLOW, "ok"
    if method != "Fetch.enable":
        return REFUSE, "method_refused"
    if not isinstance(params, dict) or params.get("handleAuthRequests"):
        return REFUSE, "fetch_auth"
    patterns = params.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        return REFUSE, "fetch_pattern"
    for entry in patterns:
        pattern = entry.get("urlPattern") if isinstance(entry, dict) else None
        if not isinstance(pattern, str) or not pattern.startswith(DIALOG_BRIDGE_PREFIX):
            return REFUSE, "fetch_pattern"
    return ALLOW, "ok"


def verdict(role: str, method: str, params=None) -> tuple[str, str]:
    """``(verdict, reason)`` for one call: ALLOW, ANSWER or REFUSE.

    The reason is a fixed word for the log line. Nothing from ``params`` is part
    of it, so a log holds no page data.
    """
    if not isinstance(method, str) or not method:
        return REFUSE, "no_method"
    if method in ANSWERED:
        return ANSWER, "answered_here"
    if role == CREDENTIALS:
        if method in _CREDENTIALS_ALLOWED:
            return ALLOW, "ok"
        return REFUSE, "not_allowlisted"
    if role != AGENT:
        return REFUSE, "unknown_client"
    if method in _AGENT_REFUSED or method.split(".", 1)[0] in _AGENT_REFUSED_DOMAINS:
        return REFUSE, "method_refused"
    if method.startswith("Fetch."):
        return _fetch_verdict(method, params)
    if method in _NAVIGATION:
        return _url_verdict(params)
    return ALLOW, "ok"
