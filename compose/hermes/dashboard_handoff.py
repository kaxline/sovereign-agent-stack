"""One-time sign-in links for the Hermes dashboard (GET /auth/handoff?code=...).

Installed into hermes_cli/ by patch-dashboard-handoff.py, which also registers
the route at the end of hermes_cli/dashboard_auth/routes.py and makes the path
public in the auth gate. Boundary change request 0007 R1.

A program that can read HERMES_DASHBOARD_HANDOFF_SECRET from the stack's .env
mints a code and opens ``<dashboard>/auth/handoff?code=<code>`` in a browser.
The route checks the code, mints a session with the ``basic`` provider (the
same tokens a password sign-in gets, so they survive a restart and refresh as
usual), sets the session cookies and redirects to the code's ``next``.

Code format (``mint_code`` below; the contract is in docs/extending.md)::

    <payload>.<signature>
    payload   = base64url(JSON {"v": 1, "nonce": str, "exp": int, "next": str}), no padding
    signature = base64url(HMAC-SHA256(key=secret as UTF-8, msg=payload as ASCII)), no padding

``exp`` is unix seconds and at most MAX_TTL_SECONDS ahead. A nonce is accepted
once; used nonces are kept in memory until they expire. A bad, expired or used
code redirects to /login (with ``next`` when the signature was good). With the
secret unset or shorter than MIN_SECRET_LEN, the route answers 404.

This module imports nothing from Hermes at import time, so the host script
(scripts/dashboard-open.py) can import it to mint codes.
"""
# No ``from __future__ import annotations``: FastAPI must see the route's
# ``Request`` annotation as a class, and it is imported inside install().
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from typing import Optional, Tuple

SECRET_ENV = "HERMES_DASHBOARD_HANDOFF_SECRET"
MIN_SECRET_LEN = 32
MAX_TTL_SECONDS = 60
# Allowed clock difference between the minting program and the container.
CLOCK_SKEW_SECONDS = 5
MAX_REMEMBERED_NONCES = 4096
PROVIDER = "basic"

_log = logging.getLogger(__name__)
_used: dict[str, int] = {}
_used_lock = threading.Lock()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())


def mint_code(secret: str, next_path: str = "/", *, ttl: int = MAX_TTL_SECONDS,
              now: Optional[int] = None) -> str:
    """Return a handoff code for *next_path* that expires in *ttl* seconds."""
    if not usable_secret(secret):
        raise ValueError(f"{SECRET_ENV} must be at least {MIN_SECRET_LEN} characters and not a placeholder")
    ttl = max(1, min(int(ttl), MAX_TTL_SECONDS))
    now = int(time.time()) if now is None else now
    body = {"v": 1, "nonce": secrets.token_urlsafe(18), "exp": now + ttl, "next": next_path or "/"}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_signature(secret, payload)}"


def check_code(code: str, secret: str, *, now: Optional[int] = None,
               consume: bool = True) -> Tuple[str, str]:
    """Return ``(status, next)``. Status is ``ok``, ``invalid``, ``expired`` or ``used``.

    ``next`` is the code's raw target, empty unless the signature was good. With
    *consume*, an ``ok`` code's nonce is recorded so the same code fails next time.
    """
    now = int(time.time()) if now is None else now
    payload, _, sig = (code or "").partition(".")
    if not payload or not sig or not hmac.compare_digest(sig, _signature(secret, payload)):
        return "invalid", ""
    try:
        body = json.loads(_unb64(payload))
        nonce, exp, next_path = body["nonce"], body["exp"], body.get("next", "/")
    except (ValueError, KeyError, TypeError):
        return "invalid", ""
    if (body.get("v") != 1 or not isinstance(nonce, str) or not 16 <= len(nonce) <= 128
            or not isinstance(exp, int) or not isinstance(next_path, str)):
        return "invalid", ""
    if exp > now + MAX_TTL_SECONDS + CLOCK_SKEW_SECONDS:
        return "invalid", ""
    if exp <= now:
        return "expired", next_path
    if not consume:
        return "ok", next_path
    with _used_lock:
        for key in [k for k, until in _used.items() if until <= now]:
            del _used[key]
        if nonce in _used:
            return "used", next_path
        if len(_used) >= MAX_REMEMBERED_NONCES:
            return "invalid", ""
        _used[nonce] = exp
    return "ok", next_path


def usable_secret(secret: str) -> bool:
    """False for an unset, short or placeholder (``change-me...``) secret."""
    return len(secret) >= MIN_SECRET_LEN and not secret.lower().startswith(("change-me", "change_me"))


def handoff_secret() -> str:
    """The configured secret, or ``""`` when the route is off."""
    secret = os.environ.get(SECRET_ENV, "").strip()
    return secret if usable_secret(secret) else ""


def install(routes) -> None:
    """Register GET /auth/handoff on hermes_cli.dashboard_auth.routes' router."""
    from fastapi import Request
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote

    _no_store = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    @routes.router.get("/auth/handoff", name="auth_handoff")
    async def auth_handoff(request: Request, code: str = ""):
        secret = handoff_secret()
        provider = routes.get_provider(PROVIDER)
        mint = getattr(provider, "_mint_session", None)
        user = getattr(provider, "_username", "")
        if not secret or mint is None or not user:
            raise routes._http(404, "Not Found")
        status, next_raw = check_code(code, secret)
        if status != "ok":
            routes._login_failure(request, PROVIDER, f"handoff_{status}")
            login = f"{routes._prefix(request)}/login"
            safe_next = routes._validate_post_login_target(next_raw)
            if safe_next:
                login = f"{login}?next={quote(safe_next, safe='')}"
            return RedirectResponse(url=login, status_code=302, headers=_no_store)
        session = mint(user)
        target, _native = routes._complete_login(
            request, PROVIDER, session, broker_state="", next_raw=next_raw)
        resp = RedirectResponse(url=target, status_code=302, headers=_no_store)
        routes._set_session(resp, request, session)
        return resp

    _log.info("dashboard-handoff: GET /auth/handoff registered")
