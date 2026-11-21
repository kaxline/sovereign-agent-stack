#!/usr/bin/env python3
"""Route browser_vault fills of bwbroker: handles to the Bitwarden broker.

Compatibility overlay (not upstream). The broker (compose/bitwarden-broker)
runs in its own container and writes the value into the page over its own CDP
connection, so the gateway never holds it. In tools/browser_vault_tool.py this:

- sends a bwbroker: fill, after core's origin pre-check and inside the
  backend's fill_context, to POST /fill with the CDP target id of the tab the
  supervisor focused and the name of the browser provider the task's cdp_url
  points at, registers the returned redaction entry with agent.redact
  (patch-digest-redaction.py), and shapes the result like core's;
- sends enter_code for a bwbroker: login with a TOTP key the same way with
  field "totp"; a login without one keeps core's ask-the-user path;
- adds a "bitwarden" entry to browser_vault_list naming locked accounts and
  make vault-unlock, or {"unavailable": true} when the broker does not answer,
  and sets sign_in_only on each listed bwbroker: item from /items.

_bwbroker_tab picks the tab a broker write lands in; patch-bitwarden-signin.py
uses it for the sign-in actions. Broker calls go through the bwbroker plugin's
backend module (bitwarden_broker_backend.py); only the agent-facing texts are
kept here.

A bwbroker: fill whose metadata read came back empty because the broker is
down gets the "Bitwarden is unavailable" result instead of core's unknown-handle
text.

Run scripts/test-bitwarden-fill.py on Hermes bumps.
"""
from __future__ import annotations

from overlay_patch import ROOT, FileEdit, edit_files, one_of

TOOLS = ROOT / "tools"
TAG = "[patch-bitwarden-broker-fill]"
MARKER = "# assistant-stack: bitwarden broker fill"

# v0.22.7, then v0.23.0+ (with_identifier for the Desktop key button; the broker fills the password only).
# On v0.23's target='preview' path _page_backend() returns the in-app page, so _bwbroker_fill refuses
# with supervisor_required instead of writing into the managed browser's tab.
FILL_ANCHORS = (
    "        return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id)\n",
    "        return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id, with_identifier)\n",
)
FILL = (
    f"        {MARKER} (route)\n"
    "        if _bwbroker_owns(backend, handle):\n"
    "            return _bwbroker_fill(handle, meta, page_origin, effective_task_id, field=\"password\")\n"
)

META_NONE_ANCHOR = (
    "    if meta is None:\n"
    "        return json.dumps(\n"
    "            {\n"
    "                \"success\": False,\n"
    "                \"error\": (\n"
    "                    f\"No vault item with handle {handle!r}. Use browser_vault action=list. \"\n"
)
META_NONE = (
    f"    {MARKER} (broker down)\n"
    "    if meta is None and _bwbroker_owns(backend, handle) and _bwbroker_down():\n"
    "        return json.dumps({\"success\": False, \"error\": _BWBROKER_UNAVAILABLE})\n"
)

CODE_ANCHOR = (
    "    if meta is not None:  # a saved login on its bound origin: mint + write inside the backend's fill_context\n"
)
CODE = (
    f"    {MARKER} (code)\n"
    "    if meta is not None and _bwbroker_owns(backend, handle) and meta.has_otp is True:\n"
    "        with backend.fill_context():\n"
    "            refusal = snapshot_refusal(backend, handle, meta, origin)\n"
    "            if refusal is not None:\n"
    "                return json.dumps(refusal)\n"
    "            return _bwbroker_fill(handle, meta, origin, effective_task_id, field=\"totp\")\n"
)

LIST_ANCHOR = (
    "    if errors:\n"
    "        out[\"errors\"] = errors\n"
    "    return json.dumps(out, ensure_ascii=False)\n"
)
LIST = (
    "    if errors:\n"
    "        out[\"errors\"] = errors\n"
    f"    {MARKER} (list)\n"
    "    _bwbroker_list_hint(out)\n"
    "    return json.dumps(out, ensure_ascii=False)\n"
)

HELPERS_ANCHOR = "def _confirm_payment_fill(label: str, origin: str) -> bool:\n"
HELPERS = f"{MARKER}\n" + r'''# A bwbroker: login is filled by the Bitwarden broker, a container of its own on the internal
# credentials network. It writes the value into the tab over its own CDP connection and returns a
# digest entry that agent.redact scrubs by, so the value never reaches the gateway. Results use core's
# shapes. From a broker reply only the fixed error code and a well-formed origin are passed on.
# The broker client (URL, bearer token, reply cap, error-code check) is the bwbroker plugin's backend
# module, which 14-install-bwbroker-plugin.sh always installs; _bwbroker() imports it on first use, so
# this module still imports when the plugin is missing.
_BWBROKER_NAME = "bwbroker"
_BWBROKER_FILL_TIMEOUT = 30.0  # the broker's own CDP deadline is 10 s, after bw has returned the value
_BWBROKER_STATUS_TIMEOUT = 2.0
_BWBROKER_UNAVAILABLE = "Bitwarden is unavailable right now. Try again shortly, or ask the user to check make doctor."
_BWBROKER_OTHER = "Bitwarden could not fill this login. Try again shortly, or ask the user to check make doctor."
_BWBROKER_ERRORS = {
    "target_not_found": "The tab closed before the fill ran. Nothing was written. Open the login page again and retry.",
    "no_field": "No {what} field on the current page. Nothing was written.",
    "write_unconfirmed": "The {what} was written but the page did not confirm it. Check the form before submitting.",
    "page_error": "The page failed while the {what} was being written. Reload the page and retry once.",
    "cdp_unavailable": "Bitwarden could not finish the fill in this tab. Check the form, then retry once.",
    "unknown_handle": "No Bitwarden login with that handle. Use browser_vault action=list.",
    "no_totp": "This Bitwarden login has no authenticator key. Call enter_code without a handle so the user is asked.",
    "sign_in_only": "This Bitwarden login is set up for sign_in. Call browser_vault action=sign_in with this handle.",
    "no_password": "This Bitwarden login has no password the agent account can read. Retrying will not help: tell the user to check the item has a password, and that the collection holding it does not hide passwords from the agent's account.",
    "account_locked": "The Bitwarden account holding this login is locked. Ask the user to run "
                      "make vault-unlock SLUG={slug}.",
}
_BWBROKER_NEXT = {
    "password": "Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle.",
    "totp": "Submit the form (many sites auto-submit when the last digit lands).",
}
_BWBROKER_OTHER_ACCOUNT = ("If this page asks for the code of a different account, call enter_code without a "
                           "handle so the user is asked.")


def _bwbroker() -> Any:
    """The bwbroker plugin's backend module, which holds the broker client."""
    from plugins.bwbroker import bitwarden_broker_backend

    return bitwarden_broker_backend


def _bwbroker_owns(backend: Any, handle: Any) -> bool:
    return getattr(backend, "name", None) == _BWBROKER_NAME and str(handle or "").startswith(_bwbroker().PREFIX)


def _bwbroker_call(method: str, path: str, body: Optional[dict] = None, *,
                   timeout: float = _BWBROKER_FILL_TIMEOUT) -> Optional[tuple[int, Any]]:
    """(HTTP status, decoded JSON) from the broker, or None when it did not answer with JSON."""
    broker = _bwbroker()
    try:
        return broker.call(method, path, body, timeout=timeout)
    except broker.BrokerUnavailable:
        return None


def _bwbroker_down() -> bool:
    return _bwbroker_call("GET", "/health", timeout=_BWBROKER_STATUS_TIMEOUT) is None


def _bwbroker_slug(handle: str) -> str:
    import re

    slug = handle[len(_bwbroker().PREFIX):].partition("/")[0]
    return slug if re.fullmatch(r"[a-z0-9-]{1,64}", slug) else "<slug>"


def _bwbroker_origin(value: Any) -> Optional[str]:
    """``value`` when it is already a normalized origin, else None."""
    from agent.vault_store import normalize_origin

    if not isinstance(value, str) or not value or len(value) > 300:
        return None
    try:
        return value if normalize_origin(value) == value else None
    except Exception:
        return None


def _bwbroker_target_id(supervisor: Any) -> Optional[str]:
    """The target id of the supervisor's page session, asked on that session on the supervisor loop."""
    from tools.browser_supervisor import _schedule

    loop = supervisor._loop
    with supervisor._state_lock:
        session_id = supervisor._page_session_id
    if loop is None or not loop.is_running() or not session_id:
        return None
    try:
        reply = _schedule(supervisor._cdp("Target.getTargetInfo", None, session_id=session_id, timeout=5.0),
                          loop, timeout=6.0)
    except Exception:  # health: allow BLE001 -- no target id means no fill; the caller says so
        return None
    info = reply.get("result", {}).get("targetInfo") if isinstance(reply, dict) else None
    target_id = info.get("targetId") if isinstance(info, dict) else None
    return target_id if isinstance(target_id, str) and target_id else None


# The browsers the broker can be asked to write in, by the host in the task's cdp_url
# (docs/contracts/browser.md). The names are the broker's BROWSER_PROVIDERS keys: the broker holds
# each one's endpoint and token, so nothing here needs them. A host that is not listed -- a browser
# this stack launched on loopback, say, which the broker no longer shares a namespace with -- has no
# provider, and the fill is refused as cdp_unavailable.
_BWBROKER_BROWSERS = {"web-browser": "stack", "host.docker.internal": "boundary"}


def _bwbroker_browser(supervisor: Any, task_id: str) -> Optional[str]:
    """The provider name of the task's browser, or None when the broker cannot reach it."""
    from urllib.parse import urlsplit
    from tools.browser_session import session_for_task

    for url in (session_for_task(task_id).get("cdp_url"), getattr(supervisor, "cdp_url", "")):
        try:
            host = urlsplit(str(url or "")).hostname
        except ValueError:
            continue
        name = _BWBROKER_BROWSERS.get(host or "")
        if name:
            return name
    return None


def _bwbroker_register(entry: Any) -> None:
    if entry is None:
        return
    try:
        from agent.redact import register_vault_redaction_digest
        registered = register_vault_redaction_digest(entry)
    except Exception:  # health: allow BLE001 -- counted as refused below
        registered = False
    if registered is False:
        logger.warning("bitwarden fill: redaction entry refused")


def _bwbroker_refusal(code: str, handle: str, meta: Any, found: Optional[str], field: str) -> dict[str, Any]:
    if code == "unavailable":
        return {"success": False, "error": _BWBROKER_UNAVAILABLE}
    if code == "origin_mismatch":
        out = {"success": False, "error_type": "origin_mismatch",
               "error": (f"Refused: current page origin ({found or 'unknown'}) does not match the vault item's bound "
                         f"origin(s) ({', '.join(saved_origins(meta))}). Vault fills only run on the exact origin(s) "
                         "the credential was saved for.")}
        if field == "totp":
            out["next"] = _BWBROKER_OTHER_ACCOUNT
        return out
    what = "password" if field == "password" else "one-time-code"
    text = _BWBROKER_ERRORS.get(code, _BWBROKER_OTHER).format(what=what, slug=_bwbroker_slug(handle))
    return {"success": False, "error_type": code, "error": text}


def _bwbroker_tab(task_id: str, page_origin: str, accept_kind: str, require_origin: bool = False) -> Any:
    """``(target_id, browser)`` of the task's tab on ``page_origin``, or the refusal dict to return.

    ``accept_kind`` keys _TAB_PROBES ("login", "otp"); any other value focuses the first tab on the
    origin. The broker and the signer both write through this tab, so it is picked the same way for a
    fill and for a sign-in handover.

    ``require_origin`` refuses when no tab is on ``page_origin``. A fill leaves it off: core has
    already checked the page's origin, so a focus_page that misses means the right tab is attached
    already. A sign_in turns it on, because nothing checked first and the cost of guessing wrong is a
    whole real sign-in on the site that is then refused at the handover."""
    supervisor = None
    if _page_backend(task_id) is None:  # a plugin browser has no CDP endpoint the broker could reach
        try:
            supervisor = _ensure_supervisor(task_id)
        except Exception:  # health: allow BLE001 -- reported as supervisor_required
            supervisor = None
    if supervisor is None:
        return {"success": False, "error_type": "supervisor_required",
                "error": ("Bitwarden needs the supervised browser session (direct CDP WebSocket), "
                          "and this task has none. Open the page with browser_exec and retry.")}
    # On failure the previous page session stays; the broker re-reads the tab's URL before it writes.
    focused = supervisor.focus_page(page_origin, accept=_TAB_PROBES.get(accept_kind))
    if require_origin and not (isinstance(focused, dict) and focused.get("ok")):
        return {"success": False, "error_type": "no_page",
                "error": (f"Open the login page first: this task has no browser tab on {page_origin}. "
                          "Open it with browser_exec, then call sign_in again. Nothing was signed in.")}
    target_id, browser = _bwbroker_target_id(supervisor), _bwbroker_browser(supervisor, task_id)
    if not target_id or not browser:
        return {"success": False, "error_type": "cdp_unavailable", "error": _BWBROKER_ERRORS["cdp_unavailable"]}
    # Re-admit at the write, as _eval_js_secret does: a human may have taken over the screen.
    if _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return {"success": False, "error_type": "human_has_control", "error": str(exc)}
    return target_id, browser


def _bwbroker_fill(handle: str, meta: Any, page_origin: str, task_id: str, field: str = "password") -> str:
    """Have the broker write ``field`` ("password" or "totp") of a bwbroker: login into the task's tab on
    ``page_origin``. Runs where core resolves and writes: after the origin pre-check, inside fill_context.
    PluginLoginBackend.resolve_password is never called."""
    if meta is None:
        return json.dumps({"success": False, "error": _BWBROKER_UNAVAILABLE})
    try:
        from agent.redact import vault_redaction_base
        base = vault_redaction_base()
    except Exception:  # health: allow BLE001 -- without the digest overlay a filled value could not be scrubbed
        return json.dumps({"success": False, "error": _BWBROKER_UNAVAILABLE})
    # Before the tab: a sign-in-only item has one way in, and this is the refusal that names it.
    # The tab helper's own refusals read as "retry the fill", which is advice this item can never
    # satisfy, so they must not come first.
    flags = _bwbroker_sign_in_flags()
    if flags is not None and flags.get(handle):
        return json.dumps(_bwbroker_refusal("sign_in_only", handle, meta, None, field))
    tab = _bwbroker_tab(task_id, page_origin, "login" if field == "password" else "otp")
    if isinstance(tab, dict):
        return json.dumps(tab)
    target_id, browser = tab

    reply = _bwbroker_call("POST", "/fill", {"handle": handle, "target_id": target_id, "browser": browser,
                                             "field": field, "base": base})
    if reply is None:
        return json.dumps({"success": False, "error": _BWBROKER_UNAVAILABLE})
    status, payload = reply
    found = None
    if status == 200 and isinstance(payload, dict):
        # Registered for every outcome that returns an entry: the page may hold the value even when
        # the write was not confirmed.
        _bwbroker_register(payload.get("redaction"))
        found = _bwbroker_origin(payload.get("origin"))
        if payload.get("filled") is True:
            origin = found if found in saved_origins(meta) else page_origin
            return json.dumps({"success": True, "filled": [field], "origin": origin, "next": _BWBROKER_NEXT[field]})
    return json.dumps(_bwbroker_refusal(_bwbroker().error_code(payload), handle, meta, found, field))


def _bwbroker_sign_in_flags() -> Optional[Dict[str, bool]]:
    """``{handle: sign_in_only}`` as the broker reports it, or None when it cannot be read.

    The status deadline is enough here although /items can sync: both callers run after the backend
    listed the items, with ITEMS_TIMEOUT, so the local copy is fresh and this call answers from it.
    None leaves the caller with the behaviour it had before this lookup existed."""
    reply = _bwbroker_call("GET", "/items", timeout=_BWBROKER_STATUS_TIMEOUT)
    entries = reply[1] if reply is not None and reply[0] == 200 else None
    if not isinstance(entries, list):
        return None
    return {e["handle"]: bool(e.get("sign_in_only")) for e in entries
            if isinstance(e, dict) and isinstance(e.get("handle"), str)}


def _bwbroker_sign_in_only(out: dict[str, Any]) -> None:
    """Mark each listed bwbroker: item the broker reports as sign-in only: without the item's
    ``agent-in-page-fill`` field, fill_login and enter_code refuse it and sign_in is the way in."""
    flags = _bwbroker_sign_in_flags()
    if flags is None:
        return
    for item in out.get("items") or ():
        if isinstance(item, dict) and item.get("handle") in flags:
            item["sign_in_only"] = flags[item["handle"]]


def _bwbroker_list_hint(out: dict[str, Any]) -> None:
    """Add ``bitwarden`` to a browser_vault_list result when the bwbroker backend is enabled, and the
    sign-in-only flag to its items. Never raises."""
    import re

    try:
        from agent.vault_backends import enabled_backends
        if not any(getattr(b, "name", None) == _BWBROKER_NAME for b in enabled_backends()):
            return
        _bwbroker_sign_in_only(out)
        reply = _bwbroker_call("GET", "/accounts", timeout=_BWBROKER_STATUS_TIMEOUT)
        accounts = reply[1] if reply is not None and reply[0] == 200 else None
        if not isinstance(accounts, list):
            out["bitwarden"] = {"unavailable": True}
            return
        locked = sorted({a["slug"] for a in accounts if isinstance(a, dict) and a.get("state") == "locked"
                         and isinstance(a.get("slug"), str) and re.fullmatch(r"[a-z0-9-]{1,64}", a["slug"])})
        if locked:
            out["bitwarden"] = {"locked": locked, "unlock": "make vault-unlock SLUG=<slug>"}
    except Exception:  # health: allow BLE001 -- the hint is optional
        out["bitwarden"] = {"unavailable": True}


'''

# Names the inserted code reads, per file.
NEEDED = {
    "browser_vault_tool.py": (
        "def _ensure_supervisor(task_id: str):",
        "def _page_backend(task_id: str):",
        "def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:",
        "_TAB_PROBES = {",
        "_TAB_PROBES[\"otp\"] = ",
        "from tools.browser_vault_context import plugin_errors_as_refusal, saved_origins, snapshot_refusal",
        "from typing import Any, Dict, Optional",
        "\nlogger = logging.getLogger(__name__)\n",
        "    effective_task_id = task_id or \"default\"\n    backend, meta = _saved_login(handle)\n",
        "    with backend.fill_context():\n        refusal = snapshot_refusal(backend, handle, meta, page_origin)\n",
    ),
    "browser_supervisor.py": (
        "def _schedule(coro, loop, *, timeout: float):",
        "    def focus_page(self, origin: str, *, accept: Optional[str] = None, timeout: float = 10.0)",
        "self._page_session_id: Optional[str] = None",
        "self._state_lock = threading.Lock()",
        "async def _cdp(self, method: str, params: Optional[dict[str, Any]] = None, *,\n"
        "                   session_id: Optional[str] = None,",
        "self._loop: Optional[asyncio.AbstractEventLoop] = None",  # supervisor._loop
        "self.cdp_url = cdp_url",  # supervisor.cdp_url
    ),
    "browser_session.py": (
        "def session_for_task(task_id: Optional[str]) -> dict[str, Any]:",
    ),
}
def main() -> None:
    edit_files([
        FileEdit(TOOLS / "browser_vault_tool.py", [
            one_of(*((anchor, FILL + anchor) for anchor in FILL_ANCHORS)),
            (META_NONE_ANCHOR, META_NONE + META_NONE_ANCHOR),
            (CODE_ANCHOR, CODE + CODE_ANCHOR),
            (LIST_ANCHOR, LIST),
            (HELPERS_ANCHOR, HELPERS + HELPERS_ANCHOR),
        ], requires=NEEDED["browser_vault_tool.py"]),
        # Names the inserted code calls in modules it does not edit.
        FileEdit(TOOLS / "browser_supervisor.py", requires=NEEDED["browser_supervisor.py"]),
        FileEdit(TOOLS / "browser_session.py", requires=NEEDED["browser_session.py"]),
    ], MARKER, TAG)


if __name__ == "__main__":
    main()
