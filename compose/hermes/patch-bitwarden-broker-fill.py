#!/usr/bin/env python3
"""Route browser_vault fills of bwbroker: handles to the Bitwarden broker.

Compatibility overlay (not upstream). The broker (compose/bitwarden-broker)
shares the hermes container's network namespace and writes the value into the
page over its own CDP connection, so the gateway never holds it. In
tools/browser_vault_tool.py this:

- sends a bwbroker: fill, after core's origin pre-check and inside the
  backend's fill_context, to POST /fill with the CDP target id of the tab the
  supervisor focused and the port from the task's cdp_url, registers the
  returned redaction entry with agent.redact (patch-digest-redaction.py), and
  shapes the result like core's;
- sends enter_code for a bwbroker: login with a TOTP key the same way with
  field "totp"; a login without one keeps core's ask-the-user path;
- adds a "bitwarden" entry to browser_vault_list naming locked accounts and
  make vault-unlock, or {"unavailable": true} when the broker does not answer.

A bwbroker: fill whose metadata read came back empty because the broker is
down gets the "Bitwarden is unavailable" result instead of core's unknown-handle
text.

Run scripts/test-bitwarden-fill.py on Hermes bumps.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
TOOLS = ROOT / "tools"
TAG = "[patch-bitwarden-broker-fill]"
MARKER = "# assistant-stack: bitwarden broker fill"

FILL_ANCHOR = "        return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id)\n"
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
HELPERS = f"{MARKER}\n" + r'''# A bwbroker: login is filled by the Bitwarden broker on 127.0.0.1:8099, in this container's network
# namespace. It writes the value into the tab over its own CDP connection and returns a digest entry
# that agent.redact scrubs by, so the value never reaches the gateway. Results use core's shapes. From a
# broker reply only the fixed error code and a well-formed origin are passed on.
_BWBROKER_URL = "http://127.0.0.1:8099"
_BWBROKER_NAME = "bwbroker"
_BWBROKER_PREFIX = "bwbroker:"
_BWBROKER_FILL_TIMEOUT = 30.0  # the broker's own CDP deadline is 10 s, after bw has returned the value
_BWBROKER_STATUS_TIMEOUT = 2.0
_BWBROKER_MAX_REPLY = 1 << 20
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
    "account_locked": "The Bitwarden account holding this login is locked. Ask the user to run "
                      "make vault-unlock SLUG={slug}.",
}
_BWBROKER_NEXT = {
    "password": "Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle.",
    "totp": "Submit the form (many sites auto-submit when the last digit lands).",
}
_BWBROKER_OTHER_ACCOUNT = ("If this page asks for the code of a different account, call enter_code without a "
                           "handle so the user is asked.")


def _bwbroker_owns(backend: Any, handle: Any) -> bool:
    return getattr(backend, "name", None) == _BWBROKER_NAME and str(handle or "").startswith(_BWBROKER_PREFIX)


def _bwbroker_call(method: str, path: str, body: Optional[dict] = None, *,
                   timeout: float = _BWBROKER_FILL_TIMEOUT) -> Optional[tuple[int, Any]]:
    """(HTTP status, decoded JSON) from the broker, or None when it did not answer with JSON."""
    import http.client
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(_BWBROKER_URL + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback never goes through a proxy
    try:
        try:
            with opener.open(request, timeout=timeout) as resp:
                status, raw = resp.status, resp.read(_BWBROKER_MAX_REPLY + 1)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(_BWBROKER_MAX_REPLY + 1)
        if len(raw) > _BWBROKER_MAX_REPLY:
            return None
        return status, json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError):
        return None


def _bwbroker_down() -> bool:
    return _bwbroker_call("GET", "/health", timeout=_BWBROKER_STATUS_TIMEOUT) is None


def _bwbroker_code(payload: Any) -> str:
    import re

    code = payload.get("error") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z_]{0,39}", code) else "unavailable"


def _bwbroker_slug(handle: str) -> str:
    import re

    slug = handle[len(_BWBROKER_PREFIX):].partition("/")[0]
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


def _bwbroker_cdp_port(supervisor: Any, task_id: str) -> Optional[int]:
    """The DevTools port of the task's browser. The broker connects only to 127.0.0.1, so a browser
    elsewhere gives None."""
    from urllib.parse import urlsplit
    from tools.browser_session import session_for_task

    for url in (session_for_task(task_id).get("cdp_url"), getattr(supervisor, "cdp_url", "")):
        try:
            parts = urlsplit(str(url or ""))
            port = parts.port
        except ValueError:
            continue
        if port and parts.hostname in ("127.0.0.1", "localhost", "::1"):
            return port
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
    supervisor = None
    if _page_backend(task_id) is None:  # a plugin browser has no CDP endpoint the broker could reach
        try:
            supervisor = _ensure_supervisor(task_id)
        except Exception:  # health: allow BLE001 -- reported as supervisor_required
            supervisor = None
    if supervisor is None:
        return json.dumps({"success": False, "error_type": "supervisor_required",
                           "error": ("Vault fill requires the supervised browser session (direct CDP WebSocket), "
                                     "and this task has none. Open the page with browser_exec and retry.")})
    # On failure the previous page session stays; the broker re-reads the tab's URL before it writes.
    supervisor.focus_page(page_origin, accept=_TAB_PROBES.get("login" if field == "password" else "otp"))
    target_id, port = _bwbroker_target_id(supervisor), _bwbroker_cdp_port(supervisor, task_id)
    if not target_id or not port:
        return json.dumps(_bwbroker_refusal("cdp_unavailable", handle, meta, None, field))
    # Re-admit at the write, as _eval_js_secret does: a human may have taken over the screen.
    if _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return json.dumps({"success": False, "error_type": "human_has_control", "error": str(exc)})

    reply = _bwbroker_call("POST", "/fill", {"handle": handle, "target_id": target_id, "port": port,
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
    return json.dumps(_bwbroker_refusal(_bwbroker_code(payload), handle, meta, found, field))


def _bwbroker_list_hint(out: dict[str, Any]) -> None:
    """Add ``bitwarden`` to a browser_vault_list result when the bwbroker backend is enabled. Never raises."""
    import re

    try:
        from agent.vault_backends import enabled_backends
        if not any(getattr(b, "name", None) == _BWBROKER_NAME for b in enabled_backends()):
            return
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
    ),
}
ANCHORS = {
    "fill": FILL_ANCHOR,
    "meta none": META_NONE_ANCHOR,
    "enter_code": CODE_ANCHOR,
    "list": LIST_ANCHOR,
    "helpers": HELPERS_ANCHOR,
}


def main() -> None:
    target = TOOLS / "browser_vault_tool.py"
    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TAG} already applied")
        return
    sources = {"browser_vault_tool.py": text,
               "browser_supervisor.py": (TOOLS / "browser_supervisor.py").read_text(encoding="utf-8")}
    missing = [f"{name}: {n.strip()}" for name, needed in NEEDED.items() for n in needed if n not in sources[name]]
    bad = [name for name, anchor in ANCHORS.items() if text.count(anchor) != 1]
    if missing or bad:
        raise SystemExit(
            f"{TAG} expected code missing in {TOOLS}: {missing + bad}. "
            "Hermes image may have changed — update this patch."
        )
    text = text.replace(FILL_ANCHOR, FILL + FILL_ANCHOR, 1)
    text = text.replace(META_NONE_ANCHOR, META_NONE + META_NONE_ANCHOR, 1)
    text = text.replace(CODE_ANCHOR, CODE + CODE_ANCHOR, 1)
    text = text.replace(LIST_ANCHOR, LIST, 1)
    text = text.replace(HELPERS_ANCHOR, HELPERS + HELPERS_ANCHOR, 1)
    target.write_text(text, encoding="utf-8")
    print(f"{TAG} patched {target}")


if __name__ == "__main__":
    main()
