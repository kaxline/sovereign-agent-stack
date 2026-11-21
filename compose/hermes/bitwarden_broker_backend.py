"""Hermes login backend over the Bitwarden broker's API (bwbroker).

The broker (compose/bitwarden-broker) runs in its own container and answers on
the internal credentials network, which only hermes joins. Every call carries
the bearer token the broker was given, read from the one file hermes mounts for
it. No route returns a password, TOTP code or seed, so this backend lists
metadata only and every resolve_* call raises. Fills reach the broker through
the fill overlay, which calls POST /fill and lets the broker write the page
itself.

The bwbroker plugin (compose/hermes/plugins/bwbroker) imports this module from
beside its __init__.py; 14-install-bwbroker-plugin.sh puts both there. This is
the one broker client: the fill overlay (patch-bitwarden-broker-fill.py) and the
sign-in actions (vault_bitwarden_signin.py) import it as
plugins.bwbroker.bitwarden_broker_backend and use its call(), error_code() and
PREFIX.

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

BROKER_URL = "http://bitwarden-broker:8099"
# The broker's bearer token. docker-compose.yml mounts the folder holding it
# read-only; without the file every call comes back 401.
TOKEN_FILE = "/run/bitwarden/api-token"
TIMEOUT = 5
# GET /items syncs the vault before it answers when the local copy is at least
# BITWARDEN_ITEMS_SYNC_SECONDS old, and a sync of a self-hosted vault takes ten
# seconds or more, so that one route gets the sign-in helper's deadline. At
# TIMEOUT the first listing of a turn timed out and listed nothing, which reads
# in the conversation as a vault with no logins in it.
ITEMS_TIMEOUT = 45
PREFIX = "bwbroker:"  # every handle this backend lists starts with it
NO_VALUES = "bwbroker never returns values"
# Replies are small JSON; /items for a large vault stays well under this. Only a memory bound:
# every caller reads /items, and a lower cap anywhere would drop a large vault's items there only.
MAX_REPLY = 8 * 1024 * 1024
# The broker's error codes; anything else in an error body is not passed on.
ERROR_CODE = re.compile(r"[a-z][a-z_]{0,39}")

# No proxy: an HTTP(S)_PROXY in the container must not see these calls.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def token() -> str:
    """The broker's bearer token, or "" when the file is missing or unreadable."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as handle:
            return handle.read(4096).strip()
    except OSError:
        return ""


class BrokerUnavailable(Exception):
    """The broker did not answer, or answered with something other than JSON."""


def call(method: str, path: str, body: Optional[dict] = None, *, base: Optional[str] = None,
         timeout: float = TIMEOUT) -> tuple[int, Any]:
    """(HTTP status, decoded JSON) for one broker call; BrokerUnavailable otherwise."""
    base = base or BROKER_URL  # read here, so a test can point the module at its own stub
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    headers["Authorization"] = "Bearer " + token()
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
    if not (type(handle) is str and handle.startswith(PREFIX) and type(label) is str
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
    prefix = PREFIX
    display_name = "Bitwarden"
    needs_unlock = False

    def __init__(self, in_cron: Callable[[], bool] = in_cron_turn, base_url: Optional[str] = None):
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
            status, payload = call("GET", "/items", base=self._base, timeout=ITEMS_TIMEOUT)
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
