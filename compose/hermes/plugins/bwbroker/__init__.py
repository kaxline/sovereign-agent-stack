"""bwbroker: Bitwarden logins for Hermes's browser vault, from the stack's broker.

Registers the broker's login backend (bitwarden_broker_backend.py, installed
beside this file) and two tools in the ``bitwarden`` toolset. The bootstrap's
cron fence keeps that toolset out of cron. Neither tool can widen the agent's
access: a request only records that the agent asked, and the user approves it
with ``make vault-grant`` or by moving the login in Bitwarden. A release
narrows access at once.

Tool results are JSON strings. A broker error comes back as
``{"success": false, "error": <code>}`` with the broker's fixed code,
``broker_unavailable`` when the broker does not answer.
"""

from __future__ import annotations

import json

from . import bitwarden_broker_backend as broker

TOOLSET = "bitwarden"
# A release runs bw (read the item, edit its collections, refresh the list) before
# it answers, which took over 5 s against a local Vaultwarden; the default 5 s
# timeout reported broker_unavailable for a release that went through.
RELEASE_TIMEOUT = 60
APPROVE = ("make vault-grant SITE={site}, or move the login into the agent's folder or collection "
           "in Bitwarden")

REQUEST_SCHEMA = {
    "name": "bitwarden_request_access",
    "description": (
        "Ask the user for a Bitwarden login you don't have, for a site you need to sign in to. "
        "Requesting never grants access by itself: the user approves outside this chat, and until then "
        "the login does not appear in browser_vault action=list. Tell the user you asked and what for. "
        "Use this only when browser_vault action=list has no login for the site."),
    "parameters": {
        "type": "object",
        "properties": {
            "site": {"type": "string", "description": "The site's hostname, for example example.com."},
            "reason": {"type": "string", "maxLength": 200,
                       "description": "One sentence on why you need the login. The user sees it as your words."},
            "account": {"type": "string",
                        "description": "The Bitwarden account to ask in, when the stack has more than one. "
                                       "Leave it out otherwise."},
        },
        "required": ["site", "reason"],
    },
}

RELEASE_SCHEMA = {
    "name": "bitwarden_release_access",
    "description": (
        "Give a Bitwarden login back when you no longer need it. The broker stops listing and filling it "
        "at once and moves it out of your folder or collection. Getting it back takes a new "
        "bitwarden_request_access, and requesting never grants access by itself: the user approves outside "
        "this chat."),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "The login's id from browser_vault action=list (bwbroker:...)."},
        },
        "required": ["handle"],
    },
}


def _refused(code: str) -> str:
    return json.dumps({"success": False, "error": code})


def request_access(args: dict, **_kwargs) -> str:
    site, reason, account = args.get("site"), args.get("reason"), args.get("account")
    if not isinstance(site, str) or not site.strip():
        return _refused("invalid_site")
    if not isinstance(reason, str) or not reason.strip():
        return _refused("invalid_reason")
    body = {"site": site.strip(), "reason": reason}
    if isinstance(account, str) and account.strip():
        body["account"] = account.strip()
    try:
        status, payload = broker.call("POST", "/requests", body)
    except broker.BrokerUnavailable:
        return _refused("broker_unavailable")
    request_id = payload.get("request_id") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(request_id, str):
        return _refused(broker.error_code(payload))
    return json.dumps({"pending": True, "request_id": request_id, "approve": APPROVE.format(site=body["site"])})


def release_access(args: dict, **_kwargs) -> str:
    handle = args.get("handle")
    if not isinstance(handle, str) or not handle.startswith(broker.HANDLE_PREFIX):
        return _refused("unknown_handle")
    try:
        status, payload = broker.call("POST", "/release", {"handle": handle}, timeout=RELEASE_TIMEOUT)
    except broker.BrokerUnavailable:
        return _refused("broker_unavailable")
    if status != 200 or not isinstance(payload, dict) \
            or type(payload.get("released")) is not bool or type(payload.get("returned")) is not bool:
        return _refused(broker.error_code(payload))
    return json.dumps({"released": payload["released"], "returned": payload["returned"]})


def register(ctx) -> None:
    ctx.register_login_backend(broker.BrokerLoginBackend())
    for schema, handler in ((REQUEST_SCHEMA, request_access), (RELEASE_SCHEMA, release_access)):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"])
