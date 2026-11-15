"""browser_vault sign-in actions for bwbroker: logins (CR 0033).

The Bitwarden broker signs the agent in with its own Chromium, in a container
the agent cannot reach, and hands the site's session to the agent's tab. The
password and the TOTP code never enter hermes. These seven actions are the
agent's side of the broker's /signin/ routes:

    sign_in         POST /signin/start      opens the item's first URI and runs
                                            the broker's automatic sequence
    signin_snapshot POST /signin/snapshot   reads the page
    signin_click    POST /signin/click      clicks a ref from a snapshot
    signin_type     POST /signin/type       types agent text into a field
    signin_fill     POST /signin/fill       writes the password or the TOTP code
    signin_finish   POST /signin/finish     hands the session to the agent's tab
    signin_cancel   POST /signin/cancel     ends the session

sign_in and signin_finish carry the CDP target id of the agent's tab and the
name of the browser provider it runs in, from the fill overlay's _bwbroker_tab:
sign_in for the item's first origin,
signin_finish for the origin the last snapshot reported. Results carry the
snapshot the broker returned (origin, and role, name, text and ref per node),
the handover's cookie count, or a fixed text per error code. Nothing from a
broker reply reaches the agent unchecked: the origin must normalize to itself,
node fields are rebuilt by type and the error code must be one of the fixed set.

A cron turn gets the no-such-login refusal on all seven actions. The backend
already lists no items there, which stops sign_in, but the broker's session is
one per stack and bound to no task, so a cron turn running beside a person's
sign-in could otherwise drive that session and take its cookies.

Installed as /opt/hermes/tools/vault_bitwarden_signin.py by
patch-bitwarden-signin.py. Run scripts/test-bitwarden-fill.py on Hermes bumps.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ACTIONS = ("sign_in", "signin_snapshot", "signin_click", "signin_type", "signin_fill",
           "signin_finish", "signin_cancel")
HANDLE_PREFIX = "bwbroker:"
BACKEND_NAME = "bwbroker"
# The broker bounds one action at 30 s; this leaves room for its own call to the signer.
CALL_TIMEOUT = 45.0
MAX_NODES = 400
MAX_TEXT = 200
MAX_TYPE_TEXT = 4096  # the broker refuses a longer one
FIELDS = ("password", "totp")

UNAVAILABLE = "Bitwarden is unavailable right now. Try again shortly, or ask the user to check make doctor."
OTHER = "Bitwarden could not run this sign-in step. Try again shortly, or ask the user to check make doctor."
ERRORS = {
    "busy": "Another Bitwarden sign-in is running. Wait for it, or call browser_vault action=signin_cancel.",
    "no_session": "No Bitwarden sign-in is open. Call browser_vault action=sign_in with the handle first.",
    "stale_ref": "That ref is from an older snapshot. Call browser_vault action=signin_snapshot and use its refs.",
    "origin_mismatch": "The sign-in page left the login's saved origins. Call browser_vault action=signin_cancel "
                       "and tell the user.",
    "not_a_secret_field": "That element is not a field this value may go into. Take a snapshot, pick the right "
                          "field, or cancel.",
    "no_totp": "This Bitwarden login has no authenticator key.",
    "no_password": "This Bitwarden login has no password the agent account can read. Retrying will not help: "
                   "tell the user to check the item has a password, and that the collection holding it does not "
                   "hide passwords from the agent's account.",
    "handover_refused": "The browser tab is not on the login's origin, so nothing was handed over. Open the "
                        "login's page in the tab and call signin_finish again.",
    "page_error": "The sign-in page failed. Call browser_vault action=signin_cancel and tell the user.",
    "timeout": "The sign-in step took too long and was stopped. Take a snapshot to see where the page is.",
    "signer_unavailable": "The Bitwarden sign-in browser is not running. Tell the user to sign in themselves, "
                          "and to check make doctor.",
    "sandbox_unavailable": "The Bitwarden sign-in browser cannot start on this host. Tell the user to sign in "
                           "themselves, and to check make doctor.",
    "unknown_handle": "No Bitwarden login with that handle. Use browser_vault action=list.",
    "account_locked": "The Bitwarden account holding this login is locked. Ask the user to run make vault-unlock.",
    "sign_in_only": "This Bitwarden login is set up for sign_in. Call browser_vault action=sign_in with this handle.",
    "bad_request": OTHER,
}
NEXT_SNAPSHOT = ("Read the nodes. Call signin_click, signin_type or signin_fill with a ref from THIS snapshot, "
                 "then signin_snapshot again. A CAPTCHA, an emailed link or a device check needs the user: "
                 "call signin_cancel and tell them.")
NEXT_DONE = "The browser tab now holds the site's session. Reload or open the page and check it yourself."

SCHEMA_SENTENCE = (
    " sign_in (a Bitwarden login marked sign_in_only: the broker signs in in its own browser and hands the "
    "session to your tab; it returns done, or a page snapshot to carry on with signin_snapshot, signin_click, "
    "signin_type (never a password), signin_fill (ref + field), signin_finish and signin_cancel; refs come from "
    "the latest snapshot)."
)

# Per task, the origin of the last snapshot a broker reply carried: the tab signin_finish hands
# over to. Keyed by task, so a second task's finish never uses the first task's origin.
_last_origin: dict[str, str] = {}
MAX_TASKS = 64


# ── schema ────────────────────────────────────────────────────────────────────────────────────

def extend_schema(schema: dict) -> None:
    """Add the sign-in actions to BROWSER_VAULT_SCHEMA in place (before registration)."""
    props = schema.setdefault("parameters", {}).setdefault("properties", {})
    action = props.setdefault("action", {"type": "string", "enum": []})
    enum = action.setdefault("enum", [])
    for name in ACTIONS:
        if name not in enum:
            enum.append(name)
    # Each property is left alone when the schema already defines it, so a Hermes bump that adds
    # `ref`, `text` or `field` keeps its own type and description.
    if not isinstance(props.get("ref"), dict):
        props["ref"] = {"type": "integer", "description": (
            "signin_click / signin_type / signin_fill: a ref from the latest snapshot.")}
    if not isinstance(props.get("text"), dict):
        props["text"] = {"type": "string", "description": (
            "signin_type only: the text to type. Never a password, code or other secret.")}
    if not isinstance(props.get("field"), dict):
        props["field"] = {"type": "string", "enum": list(FIELDS), "description": (
            "signin_fill only: 'password' or 'totp' (the saved authenticator code).")}
    if SCHEMA_SENTENCE.strip() not in schema.get("description", ""):
        schema["description"] = str(schema.get("description") or "").rstrip() + SCHEMA_SENTENCE


# ── broker replies ────────────────────────────────────────────────────────────────────────────

def _refusal(code: str) -> str:
    if code == "unavailable":
        return json.dumps({"success": False, "error": UNAVAILABLE})
    return json.dumps({"success": False, "error_type": code, "error": ERRORS.get(code, OTHER)})


def _node(raw: Any) -> Optional[dict]:
    """One snapshot node, rebuilt field by field; None when it is not one."""
    if not isinstance(raw, dict) or not isinstance(raw.get("ref"), int) or isinstance(raw.get("ref"), bool):
        return None
    out = {"ref": raw["ref"]}
    for key in ("role", "name", "text"):
        value = raw.get(key)
        out[key] = value[:MAX_TEXT] if isinstance(value, str) else ""
    return out


def _result(payload: Any, tid: str) -> str:
    """The tool result for a 200 reply: a snapshot, the handover, or the cancel."""
    from tools.browser_vault_tool import _bwbroker_origin

    if not isinstance(payload, dict):
        return _refusal("unavailable")
    if payload.get("cancelled") is not None:
        _last_origin.pop(tid, None)
        return json.dumps({"success": True, "cancelled": bool(payload.get("cancelled"))})
    origin = _bwbroker_origin(payload.get("origin"))
    if payload.get("done") is True:
        _last_origin.pop(tid, None)
        cookies = payload.get("cookies")
        return json.dumps({"success": True, "done": True, "origin": origin or "",
                           "cookies": cookies if isinstance(cookies, int) and not isinstance(cookies, bool) else 0,
                           "next": NEXT_DONE})
    nodes = [n for n in map(_node, payload.get("nodes") or ()) if n is not None][:MAX_NODES]
    if origin:
        if len(_last_origin) >= MAX_TASKS and tid not in _last_origin:
            _last_origin.clear()  # a long-lived worker's tasks, not a cache worth keeping
        _last_origin[tid] = origin
    return json.dumps({"success": True, "origin": origin or "", "nodes": nodes, "next": NEXT_SNAPSHOT})


def _call(path: str, body: dict, tid: str) -> str:
    from tools.browser_vault_tool import _bwbroker_call, _bwbroker_code

    reply = _bwbroker_call("POST", path, body, timeout=CALL_TIMEOUT)
    if reply is None:
        return json.dumps({"success": False, "error": UNAVAILABLE})
    status, payload = reply
    if status != 200:
        return _refusal(_bwbroker_code(payload))
    return _result(payload, tid)


# ── actions ───────────────────────────────────────────────────────────────────────────────────

def _item_origins(handle: str) -> Optional[tuple[str, ...]]:
    """The saved origins of a bwbroker: login, or None when nothing is listed under that handle.

    A cron turn, a released item and a broker that is down all read as None here, because the
    backend lists no items in any of them.
    """
    from agent.vault_backends import backend_for_handle
    from tools.browser_vault_context import saved_origins

    if not handle.startswith(HANDLE_PREFIX):
        return None
    try:
        backend = backend_for_handle(handle)
        if backend is None or getattr(backend, "name", None) != BACKEND_NAME:
            return None
        meta = backend.get_meta(handle)
    except Exception:  # health: allow BLE001 -- no metadata means no sign-in; reported as unknown_handle
        logger.debug("bitwarden sign-in: metadata lookup failed", exc_info=True)
        return None
    if meta is None:
        return None
    return tuple(saved_origins(meta)) or None


def _tab(task_id: str, origin: str, require_origin: bool = False) -> Any:
    """``(target_id, port)`` of the tab the session is handed to, or the refusal to return."""
    from tools.browser_vault_tool import _bwbroker_tab

    # No probe: the agent's tab on the origin need not hold a login form.
    return _bwbroker_tab(task_id, origin, "", require_origin)


def _in_cron_turn() -> bool:
    """True in a cron turn, and when that cannot be told.

    The broker holds one sign-in session for the whole stack, bound to no task, so a cron turn
    could otherwise drive a session a person's turn opened and finish its cookies into its own
    tab. The backend's own fence only hides items, which stops sign_in alone.
    """
    import sys

    module = next((m for name, m in list(sys.modules.items())
                   if name.rsplit(".", 1)[-1] == "bitwarden_broker_backend"
                   and hasattr(m, "in_cron_turn")), None)
    if module is None:
        try:
            from importlib import import_module
            module = import_module("plugins.bwbroker.bitwarden_broker_backend")
        except Exception:  # health: allow BLE001 -- without the backend there is no sign-in anyway
            logger.debug("bitwarden sign-in: no backend module to read the cron turn from", exc_info=True)
            return True
    try:
        return bool(module.in_cron_turn())
    except Exception:  # health: allow BLE001 -- can't tell: refuse
        logger.debug("bitwarden sign-in: cron check failed", exc_info=True)
        return True


def _sign_in(args: dict, tid: str) -> str:
    handle = str(args.get("handle") or "")
    origins = _item_origins(handle)
    if origins is None:
        return _refusal("unknown_handle")
    # require_origin: the signer types the password into the live site before anything checks
    # where the session can go, so a start with no tab on the item's origin signs in for nothing
    # and ends in handover_refused. Refuse here instead, with no session opened.
    tab = _tab(tid, origins[0], require_origin=True)
    if isinstance(tab, dict):
        return json.dumps(tab)
    return _call("/signin/start", {"handle": handle, "target_id": tab[0], "browser": tab[1]}, tid)


def _finish(tid: str) -> str:
    origin = _last_origin.get(tid)
    if origin is None:
        # No snapshot in this task yet (a restarted worker, or a session started elsewhere):
        # ask the broker where the page is, which also reports no_session.
        raw = _call("/signin/snapshot", {}, tid)
        origin = _last_origin.get(tid)
        if origin is None:
            return raw
    tab = _tab(tid, origin)
    if isinstance(tab, dict):
        return json.dumps(tab)
    return _call("/signin/finish", {"target_id": tab[0], "browser": tab[1]}, tid)


def _ref(args: dict) -> Any:
    ref = args.get("ref")
    if isinstance(ref, str) and ref.strip().lstrip("+").isdigit():  # a model that quotes its numbers
        ref = int(ref.strip())
    if not isinstance(ref, int) or isinstance(ref, bool) or ref <= 0:
        return None
    return ref


def handle_action(action: str, args: dict, task_id: Optional[str]) -> str:
    """browser_vault's dispatch entry for the sign-in actions."""
    tid = task_id or "default"
    if _in_cron_turn():
        return _refusal("unknown_handle")
    if action == "sign_in":
        return _sign_in(args, tid)
    if action == "signin_snapshot":
        return _call("/signin/snapshot", {}, tid)
    if action == "signin_cancel":
        return _call("/signin/cancel", {}, tid)
    if action == "signin_finish":
        return _finish(tid)
    ref = _ref(args)
    if ref is None:
        return json.dumps({"success": False, "error_type": "bad_request",
                           "error": "ref must be a node number from the latest snapshot."})
    if action == "signin_click":
        return _call("/signin/click", {"ref": ref}, tid)
    if action == "signin_type":
        text = args.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TYPE_TEXT:
            return json.dumps({"success": False, "error_type": "bad_request",
                               "error": "text must be the text to type, and never a secret."})
        return _call("/signin/type", {"ref": ref, "text": text}, tid)
    if action == "signin_fill":
        field = str(args.get("field") or "password")
        if field not in FIELDS:
            return json.dumps({"success": False, "error_type": "bad_request",
                               "error": "field must be 'password' or 'totp'."})
        return _call("/signin/fill", {"ref": ref, "field": field}, tid)
    return json.dumps({"success": False, "error": f"Unknown action {action!r}."})
