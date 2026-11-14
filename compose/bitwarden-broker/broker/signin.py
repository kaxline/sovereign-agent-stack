"""Broker-side sign-in: one signer session, the automatic sequence, the handover.

The agent drives the signer's private Chromium through the broker and never
sees a value: ``fill`` fetches the item's password or current TOTP code from
bw, hands it to the signer for one write and drops it. Replies carry the
signer's snapshot as it sent it (origin, and role, name, text and a ref per
node) or a fixed error code.

``allowed`` is the item's origins plus the origins from its ``agent-signin-idp``
field, so a session can follow a redirect to an identity provider. ``fill`` and
the export pass the item's origins alone, so neither a value nor the site's
session leaves on an IdP page.

One session and one call at a time: a call that arrives while another runs gets
``busy``, as does ``start`` while a session is open. The broker holds the item,
the signer holds the browser. The broker ends the signer's session when it
abandons one (an open that failed, a timeout, a page error, any finish but a
refused handover) and once at startup, so a session left by a crashed turn does
not block the next sign-in.

The automatic sequence reads the snapshot the signer returns, which has no
attributes, so it picks fields by role and by the accessible name the page's own
label gives them. The signer classifies the element before it writes, so a
wrong guess gets ``not_a_secret_field`` and the sequence hands the page back to
the agent rather than writing anywhere else.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading

from . import cdp
from . import origins as origins_mod
from .bwcli import BwError
from .signin_client import HEALTH_TIMEOUT_SECONDS, SignerUnavailable

LOG = logging.getLogger("broker.signin")

HANDOVER_TIMEOUT = 10.0
ROUNDS = 4
MAX_URL = 8192  # the signer refuses a longer one (signer/server.py)
# Snapshot roles, lowercased: the signer's field roles (signer/snapshot.py) and
# the roles Chromium gives a button.
FIELD_ROLES = {"textbox", "searchbox", "combobox", "spinbutton", "textfield"}
BUTTON_ROLES = {"button", "pushbutton"}

_USERNAME = re.compile(r"user|e-?mail|login|account|phone", re.I)
_PASSWORD = re.compile(r"pass(word|phrase)?\b|pwd", re.I)
_OTP = re.compile(r"one[\s-]?time|verification|two[\s-]?factor|2fa|mfa|totp|otp|passcode"
                  r"|authenticat|security code|\bcode\b|\bdigit", re.I)
_SUBMIT = re.compile(r"sign[\s_-]?(in|on)\b|log[\s_-]?in\b|next|continue|submit|verify", re.I)

STATUS = {
    "busy": 409, "no_session": 409, "stale_ref": 409, "origin_mismatch": 403,
    "not_a_secret_field": 422, "no_totp": 400, "handover_refused": 409, "page_error": 502,
    "timeout": 504, "signer_unavailable": 503, "sandbox_unavailable": 503,
    "account_locked": 503, "unknown_handle": 404, "unavailable": 503,
}
# Codes the signer raises after ending its own session.
ENDED_BY_SIGNER = ("timeout", "page_error")
# Codes that leave the session usable, so the agent can go on, retry or cancel.
# A candidate the signer would not load: try the next one rather than give up.
_LADDER_MISS = ("page_error", "origin_mismatch")

KEEPS_SESSION = ("origin_mismatch", "stale_ref", "not_a_secret_field", "no_totp", "handover_refused")


class SignInError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.status = STATUS.get(code, 503)


def _kind(node) -> str | None:
    """What the sequence takes a node for: a username, password or one-time-code field."""
    if not isinstance(node, dict) or not isinstance(node.get("ref"), int):
        return None
    if str(node.get("role") or "").lower() not in FIELD_ROLES:
        return None
    name = f"{node.get('name') or ''} {node.get('text') or ''}"
    if _OTP.search(name):
        return "totp"
    if _PASSWORD.search(name):
        return "password"
    if _USERNAME.search(name):
        return "username"
    return None


def _find(nodes, kind: str) -> int | None:
    for node in nodes or ():
        if _kind(node) == kind:
            return node["ref"]
    return None


def _last_field(nodes) -> int | None:
    """The ref of the last field the sequence recognises, or None."""
    refs = [n["ref"] for n in nodes or () if _kind(n) is not None]
    return max(refs) if refs else None


def _submit(nodes, after: int | None = None) -> int | None:
    """The form's submit control: a button named like one, else the first button.

    Refs run in document order, so a button after the sign-in fields is part of
    the form while an earlier one belongs to the page's navigation. Buttons
    after ``after`` are therefore searched first, by name and then by position,
    before the page's buttons as a whole.
    """
    buttons = [n for n in nodes or () if isinstance(n, dict) and isinstance(n.get("ref"), int)
               and str(n.get("role") or "").lower() in BUTTON_ROLES]
    below = buttons if after is None else [n for n in buttons if n["ref"] > after]
    for group in (below, buttons):
        for node in group:
            if _SUBMIT.search(f"{node.get('name') or ''} {node.get('text') or ''}"):
                return node["ref"]
    for group in (below, buttons):
        if group:
            return group[0]["ref"]
    return None


class SignIn:
    def __init__(self, vault, bws, client, touch=None):
        self.vault = vault
        self.bws = bws
        self.client = client
        # Called with an account slug after a value is read, so the idle lock does
        # not lock a manual account in the middle of a sign-in (Broker._touch).
        self.touch = touch
        self._lock = threading.Lock()
        self._item = None  # the ItemMeta of the open session, or None

    # --- routes -------------------------------------------------------------

    def start(self, handle: str, target_id: str, port: int) -> dict:
        with self._guard():
            if self._item is not None:
                raise SignInError("busy")
            item = self.vault.get(handle)
            if item is None:
                raise SignInError("unknown_handle")
            snapshot = self._open(item)
            self._item = item
            try:
                return self._sequence(item, snapshot, target_id, port)
            except SignInError as exc:
                if exc.code not in KEEPS_SESSION:
                    self._abandon()
                raise

    def snapshot(self) -> dict:
        with self._guard():
            item = self._session()
            return self._action("/session/snapshot", {"allowed": self._allowed(item)})

    def click(self, ref: int) -> dict:
        with self._guard():
            item = self._session()
            return self._action("/session/click", {"ref": ref, "allowed": self._allowed(item)})

    def type(self, ref: int, text: str) -> dict:
        with self._guard():
            item = self._session()
            return self._action("/session/type", {"ref": ref, "text": text, "allowed": self._allowed(item)})

    def fill(self, ref: int, field: str) -> dict:
        with self._guard():
            return self._fill(self._session(), ref, field)

    def finish(self, target_id: str, port: int) -> dict:
        with self._guard():
            return self._finish(self._session(), target_id, port)

    def cancel(self) -> dict:
        with self._guard():
            self._forget()
            try:
                self.client.call("POST", "/session/end", {})
            except SignerUnavailable:
                raise SignInError("signer_unavailable") from None
            return {"cancelled": True}

    def status(self) -> str:
        """For /health: ``ready``, ``busy``, ``sandbox_unavailable`` or ``down``."""
        try:
            status, body = self.client.call("GET", "/health", timeout=HEALTH_TIMEOUT_SECONDS)
        except SignerUnavailable:
            return "down"
        state = body.get("status") if status == 200 else None
        return state if state in ("ready", "busy", "sandbox_unavailable") else "down"

    def end_stale(self) -> None:
        """At startup: end the session a previous broker run left in the signer.

        Under the lock, and only with no session of this run: a ``start`` that
        lands while this call is out must not have its new session ended.
        """
        try:
            with self._guard():
                if self._item is not None:
                    return
                status, body = self.client.call("POST", "/session/end", {})
        except SignInError:
            return  # a call got there first and owns the signer's session
        except SignerUnavailable:
            return
        if status == 200 and body.get("ended"):
            LOG.info("ended a sign-in session left open at startup")

    # --- the automatic sequence ---------------------------------------------

    def _sequence(self, item, snapshot: dict, target_id: str, port: int) -> dict:
        """Type the username, fill the password and the code, submit after each.

        A round that has nothing left to fill ends it: the page is handed over
        when it is on an item origin, and otherwise returned as it is, for the
        agent to carry on with the other routes.
        """
        typed_user = False
        filled: set[str] = set()
        for _ in range(ROUNDS):
            nodes = snapshot.get("nodes") or []
            acted = False
            if item.username and not typed_user:
                ref = _find(nodes, "username")
                if ref is not None:
                    typed_user, acted = True, True
                    try:
                        snapshot = self._action("/session/type", {"ref": ref, "text": item.username,
                                                                  "allowed": self._allowed(item)})
                        nodes = snapshot.get("nodes") or []
                    except SignInError as exc:
                        if exc.code not in ("not_a_secret_field", "stale_ref"):
                            raise
            for field in ("password", "totp"):
                if field in filled or (field == "totp" and not item.has_totp):
                    continue
                ref = _find(nodes, field)
                if ref is None:
                    continue
                try:
                    snapshot = self._fill(item, ref, field)
                except SignInError as exc:
                    # not_a_secret_field / stale_ref: the page is not what it looked like.
                    # origin_mismatch: the field is on an IdP origin, where _fill will not write a
                    # secret. Either way the sequence cannot continue, so it returns the snapshot
                    # and the agent carries on with the other routes.
                    if exc.code in ("not_a_secret_field", "stale_ref", "origin_mismatch"):
                        return snapshot
                    raise
                filled.add(field)
                acted = True
                nodes = snapshot.get("nodes") or []
                break
            if not acted:
                break
            ref = _submit(nodes, _last_field(nodes))
            if ref is None:
                break
            snapshot = self._action("/session/click", {"ref": ref, "allowed": self._allowed(item)})
            if not self._needs_secret(item, snapshot) and snapshot.get("origin") in item.origins:
                return self._finish(item, target_id, port)
        return snapshot

    def _has_login_form(self, snapshot: dict) -> bool:
        """A page the ladder can stop on: it asks for a password, or for an identifier.

        The username case is a two-step login, which ``_sequence`` already drives.
        """
        nodes = snapshot.get("nodes") or []
        return _find(nodes, "password") is not None or _find(nodes, "username") is not None

    def _needs_secret(self, item, snapshot: dict) -> bool:
        nodes = snapshot.get("nodes") or []
        if _find(nodes, "password") is not None:
            return True
        return item.has_totp and _find(nodes, "totp") is not None

    # --- actions ------------------------------------------------------------

    def _open(self, item) -> dict:
        """Walk the item's candidate login pages, keeping the first with a login form.

        People save a login the first time they see one, so the stored URI is often
        an invitation or a reset link that opens no form. ``login_candidates`` puts
        the item's reusable URIs first, then the site root, then the well-known
        login paths; every one is on an origin the signer allows. A candidate that
        will not load is a miss, and when none holds a form the last snapshot is
        returned for the agent to carry on from.
        """
        candidates = origins_mod.login_candidates(item.uris or (), item.origins[0])
        allowed = self._allowed(item)
        snapshot = None
        last_error = None
        for index, url in enumerate(candidates):
            route = "/session/open" if index == 0 else "/session/navigate"
            try:
                snapshot = self._call(route, {"url": url, "allowed": allowed})
            except SignInError as exc:
                if exc.code in _LADDER_MISS:
                    last_error = exc
                    continue
                # The signer keeps the session of an open that failed, and a session it
                # already had refuses this one; either way the next sign-in gets a browser.
                self._end_session()
                raise
            if self._has_login_form(snapshot):
                return snapshot
        if snapshot is None:
            self._end_session()
            raise last_error or SignInError("page_error")
        return snapshot

    def _fill(self, item, ref: int, field: str) -> dict:
        if field == "totp" and not item.has_totp:
            raise SignInError("no_totp")
        if self.vault.get(item.handle) is None:
            raise SignInError("unknown_handle")  # released while the session ran
        bw = self.bws[item.account]
        value = None
        try:
            try:
                value = bw.get_totp(item.item_id) if field == "totp" else bw.get_password(item.item_id)
            except BwError as exc:
                if field == "totp" and exc.error_class == "unknown":
                    raise SignInError("no_totp") from None
                raise SignInError("account_locked" if exc.error_class in ("locked", "auth")
                                  else "unavailable") from None
            if not value:
                raise SignInError("unavailable")
            if self.vault.get(item.handle) is None:
                raise SignInError("unknown_handle")  # a release landed while bw ran; it wins
            if self.touch is not None:
                self.touch(item.account)
            body = {"ref": ref, "field": field, "value": value, "allowed": list(item.origins)}
            try:
                return self._action("/session/fill", body)
            finally:
                body["value"] = None
        finally:
            value = None

    def _finish(self, item, target_id: str, port: int) -> dict:
        try:
            exported = self._action("/session/export", {"allowed": list(item.origins),
                                                        "item_origins": list(item.origins)})
        except SignInError:
            self._abandon()
            raise
        result = cdp.handover(port, target_id, tuple(item.origins), exported.get("cookies") or [],
                              exported.get("localStorage") or [], exported.get("sessionStorage") or [],
                              timeout=HANDOVER_TIMEOUT)
        if result.error == cdp.HANDOVER_REFUSED:
            # The session stays open: the agent can open the item's page and finish again.
            # The origin is the one thing that says why, and the agent's own snapshots
            # already carry it, so it goes in the log: without it a refusal is unreadable.
            LOG.info("handover refused: the agent's tab is on %r, not an item origin %r",
                     result.origin, sorted(item.origins))
            raise SignInError("handover_refused")
        self._abandon()
        if result.error is not None:
            LOG.info("handover failed (%s)", result.error)
            raise SignInError("page_error")
        return {"done": True, "origin": result.origin, "cookies": result.cookies}

    # --- session and transport ----------------------------------------------

    @contextlib.contextmanager
    def _guard(self):
        if not self._lock.acquire(blocking=False):
            raise SignInError("busy")
        try:
            yield
        finally:
            self._lock.release()

    def _session(self):
        if self._item is None:
            raise SignInError("no_session")
        return self._item

    def _allowed(self, item) -> list[str]:
        return list(item.origins) + list(item.idp_origins)

    def _forget(self) -> None:
        self._item = None

    def _end_session(self) -> None:
        try:
            self.client.call("POST", "/session/end", {})
        except SignerUnavailable:
            pass

    def _abandon(self) -> None:
        self._forget()
        self._end_session()

    def _action(self, path: str, body: dict) -> dict:
        try:
            return self._call(path, body)
        except SignInError as exc:
            if exc.code in ENDED_BY_SIGNER:
                self._abandon()
            elif exc.code in ("signer_unavailable", "no_session"):
                self._forget()
            raise

    def _call(self, path: str, body: dict) -> dict:
        try:
            status, reply = self.client.call("POST", path, body)
        except SignerUnavailable:
            raise SignInError("signer_unavailable") from None
        if status != 200:
            code = reply.get("error")
            if code not in STATUS:
                LOG.warning("the signer answered %d on %s", status, path)
                code = "signer_unavailable"
            raise SignInError(code)
        return reply
