"""Hermes login backend over the Bitwarden broker's loopback API (bwbroker).

The broker (compose/bitwarden-broker) shares the hermes container's network
namespace and listens on 127.0.0.1:8099. No route on that port returns a
password, TOTP code or seed, so this backend lists metadata only and every
resolve_* call raises. Fills reach the broker through the fill overlay, which
calls POST /fill and lets the broker write the page itself.

The bwbroker plugin (compose/hermes/plugins/bwbroker) imports this module from
beside its __init__.py; 14-install-bwbroker-plugin.sh puts both there.

A broker that is down, or answers with something other than JSON, lists no
items and gives no metadata rather than raising: a raise would surface in
browser_vault_list as an error. Nothing is listed during a cron turn either,
because the add-on's manifest keeps credentials out of cron and a login
backend is otherwise on for every turn the profile runs.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from agent.vault_backends.base import LoginBackend
from agent.vault_store import VaultItemMeta

BROKER_URL = "http://127.0.0.1:8099"
TIMEOUT = 5
HANDLE_PREFIX = "bwbroker:"
NO_VALUES = "bwbroker never returns values"
# Replies are small JSON; /items for a large vault stays well under this.
MAX_REPLY = 8 * 1024 * 1024
# The broker's error codes; anything else in an error body is not passed on.
ERROR_CODE = re.compile(r"[a-z][a-z_]{0,39}")

# No proxy: an HTTP(S)_PROXY in the container must not see loopback calls.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BrokerUnavailable(Exception):
    """The broker did not answer, or answered with something other than JSON."""


def call(method: str, path: str, body: Optional[dict] = None, *, base: str = BROKER_URL,
         timeout: float = TIMEOUT) -> tuple[int, Any]:
    """(HTTP status, decoded JSON) for one broker call; BrokerUnavailable otherwise."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        try:
            with _OPENER.open(request, timeout=timeout) as resp:
                status, raw = resp.status, resp.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(MAX_REPLY + 1)
    except (OSError, http.client.HTTPException, ValueError):
        raise BrokerUnavailable() from None
    if len(raw) > MAX_REPLY:
        raise BrokerUnavailable()
    try:
        return status, json.loads(raw)
    except ValueError:
        raise BrokerUnavailable() from None


def error_code(payload: Any) -> str:
    """The broker's fixed error code from an error body, or "unavailable"."""
    code = payload.get("error") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else "unavailable"


def in_cron_turn() -> bool:
    """True in a cron fire, including a delegate_task child that a cron job started.

    current_cron_execution() reads None inside a delegated child, but the
    child's thread runs in a copy of the job's context, so the ContextVar
    behind it still holds the job. When that variable can't be found, a
    delegated child counts as cron.
    """
    from agent.delegation_context import is_delegated_child_context
    from cron import execution_identity

    if execution_identity.current_cron_execution() is not None:
        return True
    if not is_delegated_child_context():
        return False
    current = getattr(execution_identity, "_CURRENT", None)
    return current is None or current.get() is not None


def item_meta(entry: Any) -> Optional[VaultItemMeta]:
    """VaultItemMeta for one GET /items entry, or None when it is malformed."""
    if not isinstance(entry, dict):
        return None
    handle, label, username = entry.get("handle"), entry.get("label"), entry.get("username")
    origins, has_totp = entry.get("origins"), entry.get("has_totp")
    if not (type(handle) is str and handle.startswith(HANDLE_PREFIX) and type(label) is str
            and (username is None or type(username) is str) and type(has_totp) is bool
            and isinstance(origins, list) and all(type(o) is str for o in origins)):
        return None
    return VaultItemMeta(
        id=handle, kind="login", label=label, origin=origins[0] if origins else None, created_at="",
        identifier_type="username" if username else None, identifier=username or None,
        has_otp=has_totp, allowed_origins=tuple(origins))


class BrokerLoginBackend(LoginBackend):
    # Plain class attributes: Hermes refuses descriptors on these three.
    name = "bwbroker"
    prefix = "bwbroker:"
    display_name = "Bitwarden"
    needs_unlock = False

    def __init__(self, in_cron: Callable[[], bool] = in_cron_turn, base_url: str = BROKER_URL):
        self._in_cron = in_cron
        self._base = base_url

    def _cron(self) -> bool:
        try:
            return bool(self._in_cron())
        except Exception:  # can't tell: list nothing
            return True

    def list_items(self) -> list[VaultItemMeta]:
        if self._cron():
            return []
        try:
            status, payload = call("GET", "/items", base=self._base)
        except BrokerUnavailable:
            return []
        if status != 200 or not isinstance(payload, list):
            return []
        return [m for m in map(item_meta, payload) if m is not None]

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str) -> str:
        raise RuntimeError(NO_VALUES)

    def resolve_otp(self, handle: str) -> Optional[str]:
        raise RuntimeError(NO_VALUES)

    def resolve_secret(self, handle: str) -> dict[str, str]:
        raise RuntimeError(NO_VALUES)

    def fill_context(self):
        return contextlib.nullcontext()
