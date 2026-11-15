"""Write a value into a browser page over the Chrome DevTools Protocol.

The broker fills the page itself, so the agent never holds the value. ``fill``
opens the browser provider's CDP endpoint (``docs/contracts/browser.md``: one
WebSocket URL per client, with the broker's own token in it), reads the target's
URL with ``Target.getTargetInfo``, refuses a target whose origin is not allowed,
attaches a flat session to it, and calls a function on the page's document with
the value as a ``Runtime.callFunctionOn`` argument. The value is never part
of an expression or function source. ``field="password"`` writes a password
field; ``field="totp"`` writes a one-time code the way core's enter_code does.

``handover`` writes a sign-in session the signer exported into the same tab:
the same origin check (an origin outside the item's is ``handover_refused``),
then ``Network.setCookies``, one ``Runtime.callFunctionOn`` that writes both
storages from arguments, and a reload.

No HTTP route is used: ``/json/list`` and ``/json/version`` are not part of the
contract, and a provider may serve neither.

``FillResult.error`` is one of six fixed texts. ``write_unconfirmed`` means a
setter ran but the field was empty afterwards (a page handler cleared it, say),
so the page may hold the value. Nothing from the page, from a
CDP error, or the value reaches a result, an exception or a log line. The
websocket code keeps to the asyncio ``websockets.connect`` API that Debian
bookworm's websockets 10.4 has.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .origins import cookie_domain_allowed, normalize_origin, origin_hosts

TARGET_NOT_FOUND = "target_not_found"
ORIGIN_MISMATCH = "origin_mismatch"
NO_FIELD = "no_field"
WRITE_UNCONFIRMED = "write_unconfirmed"
PAGE_ERROR = "page_error"
CDP_UNAVAILABLE = "cdp_unavailable"
HANDOVER_REFUSED = "handover_refused"

_TARGET_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9_-]{1,64}")
_MAX_MESSAGE = 4 << 20

LOG = logging.getLogger("broker.cdp")

# websockets logs frames at DEBUG, and a frame can hold the value.
_WS_LOGGER = logging.getLogger("broker.cdp.websocket")
_WS_LOGGER.disabled = True
_WS_LOGGER.propagate = False

# Called on the document with (expectedOrigin, value). Mirrors core's
# build_fill_js: check the origin again right before writing, look for the
# field through open shadow roots, set the value with the native setter, and
# dispatch composed input and change events. It returns no page data.
_FILL_FUNCTION = """function (expectedOrigin, value) {
  if (window.location.origin !== expectedOrigin) return { refused: "origin_changed" };
  const roots = [];
  (function walk(root) {
    roots.push(root);
    for (const e of root.querySelectorAll("*")) if (e.shadowRoot) walk(e.shadowRoot);
  })(document);
  const fields = roots
    .flatMap((r) => Array.from(r.querySelectorAll('input[type="password"]')))
    .filter((el) => !el.disabled && !el.readOnly);
  let active = document.activeElement;
  while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;
  const el = fields.includes(active) ? active : (fields.find((f) => f.getClientRects().length > 0) || fields[0]);
  if (!el) return { filled: 0 };
  try {
    el.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (setter && setter.set) { setter.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return { filled: el.value.length > 0 ? 1 : 0, wrote: 1 };
  } catch (e) {
    // A page handler can throw with the value in its message; report only that the write failed.
    return { refused: "write_failed" };
  }
}"""


# Called on the document with (expectedOrigin, code). Mirrors core's enter_code
# (classify_otp_controls, build_otp_fills, build_fill_js in
# agent/vault_login_classifier.py): collect inputs and selects through open
# shadow roots, keep visible enabled inputs that take a code (autocomplete
# one-time-code scores 100; a text, tel, number or password input whose name or
# label says code, OTP, 2FA or verification scores 70). When there are exactly as
# many maxlength=1 code boxes as characters, in one form and adjacent, each box
# gets one character; otherwise the best-scoring field gets the whole code. With
# no code field, the focused text input takes it.
_TOTP_FUNCTION = r"""function (expectedOrigin, value) {
  if (window.location.origin !== expectedOrigin) return { refused: "origin_changed" };
  const elements = [];
  const forms = [];
  (function walk(root) {
    for (const e of root.querySelectorAll("*")) {
      if (e.tagName === "INPUT" || e.tagName === "SELECT") elements.push(e);
      else if (e.tagName === "FORM") forms.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  })(document);
  const skipped = ["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"];
  const usable = (el) => {
    if (el.tagName !== "INPUT" || el.disabled || el.readOnly || skipped.includes(el.type)) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const norm = (t) => String(t || "").normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const otp = /\b(?:one[\s-]?time|verification|security|auth(?:entication|enticator)?|2fa|two[\s-]?factor|mfa|totp|otp|passcode|sms)\b.*\b(?:code|pin|token)\b|\b(?:otp|totp|2fa|mfa|verification\s*code|passcode)\b/;
  const textOf = (el) => {
    const labels = el.labels ? Array.from(el.labels, (l) => l.textContent || "") : [];
    const aria = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean).map((id) => {
      const r = el.getRootNode();
      const n = (r && r.getElementById ? r.getElementById(id) : null) || document.getElementById(id);
      return n ? (n.textContent || "") : "";
    }).join(" ");
    return [el.name, el.id, ...labels, el.getAttribute("aria-label") || "", aria,
            el.getAttribute("placeholder") || "", el.getAttribute("title") || ""].join(" ");
  };
  const controls = [];
  elements.forEach((el, index) => {
    if (!usable(el)) return;
    const tokens = String(el.autocomplete || "").toLowerCase().split(/\s+/);
    let score = 0;
    if (tokens.includes("one-time-code")) score = 100;
    else if (["text", "tel", "number", "password", ""].includes(el.type) && otp.test(norm(textOf(el)))) score = 70;
    if (score) controls.push({ el, index, score, form: el.form ? forms.indexOf(el.form) : -1 });
  });
  let writes = [];
  const boxes = controls.filter((c) => c.el.maxLength === 1);
  if (boxes.length > 0 && boxes.length === value.length && boxes.every((b) => b.form === boxes[0].form)
      && boxes.every((b, i) => i === 0 || b.index - boxes[i - 1].index === 1)) {
    writes = boxes.map((b, i) => [b.el, value[i]]);
  } else if (controls.length > 0) {
    const best = controls.reduce((a, b) => (b.score > a.score ? b : a));
    writes = [[best.el, value]];
  } else {
    let active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;
    if (active && usable(active) && ["text", "tel", "number", "password", ""].includes(active.type)) {
      writes = [[active, value]];
    }
  }
  if (writes.length === 0) return { filled: 0 };
  let filled = 0;
  let wrote = 0;
  try {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    for (const [el, part] of writes) {
      el.focus();
      if (setter && setter.set) { setter.set.call(el, part); } else { el.value = part; }
      wrote = 1;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value.length > 0) filled += 1;
    }
  } catch (e) {
    // A page handler can throw with the code in its message; report only that the write failed.
    return { refused: "write_failed" };
  }
  return { filled: filled > 0 ? 1 : 0, wrote };
}"""

FIELDS = {"password": _FILL_FUNCTION, "totp": _TOTP_FUNCTION}


# Called on the document with (expectedOrigin, local, session), each storage's
# entries a list of [key, value] pairs passed as arguments. Storage access
# throws on a page where it is blocked, which is reported as a failed write.
_STORAGE_FUNCTION = """function (expectedOrigin, local, session) {
  if (window.location.origin !== expectedOrigin) return { refused: "origin_changed" };
  try {
    for (const entry of local) window.localStorage.setItem(entry[0], entry[1]);
    for (const entry of session) window.sessionStorage.setItem(entry[0], entry[1]);
    return { wrote: 1 };
  } catch (e) {
    // The message can quote a key or value; report only that the write failed.
    return { refused: "write_failed" };
  }
}"""


@dataclass(frozen=True)
class FillResult:
    filled: bool
    origin: str
    error: str | None


@dataclass(frozen=True)
class HandoverResult:
    done: bool
    origin: str
    error: str | None
    cookies: int = 0  # how many were written; 0 unless the handover went through


class _Fail(Exception):
    """Internal: carries one of the fixed error texts to ``fill_async`` or ``handover_async``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fill(endpoint: str, target_id: str, allowed_origins: tuple[str, ...], value: str, timeout: float = 10.0,
         field: str = "password") -> FillResult:
    """Fill ``field`` ("password" or "totp") in ``target_id`` at ``endpoint``.

    ``endpoint`` is a provider's CDP WebSocket URL for the credential client, token
    and all. Call from a thread with no running event loop.
    """
    return asyncio.run(fill_async(endpoint, target_id, allowed_origins, value, timeout, field=field))


async def fill_async(endpoint: str, target_id: str, allowed_origins: tuple[str, ...], value: str,
                     timeout: float = 10.0, field: str = "password") -> FillResult:
    if field not in FIELDS:
        raise ValueError('field must be "password" or "totp"')
    if not valid_endpoint(endpoint):
        return FillResult(False, "", CDP_UNAVAILABLE)
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        return FillResult(False, "", TARGET_NOT_FOUND)
    if not isinstance(value, str) or not value:
        raise ValueError("value must be a non-empty string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError("timeout must be a positive number")
    if isinstance(allowed_origins, str):
        raise TypeError("allowed_origins must be a collection of origins, not one string")
    allowed = {o for o in (normalize_origin(a) for a in allowed_origins) if o}

    # One deadline for the whole call. Every step, the two HTTP GETs included, runs on this
    # task under one wait_for, so nothing outlives it. Detach and close each get a slice.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cleanup = min(1.0, timeout / 5)
    budget = deadline - 2 * cleanup - loop.time()
    seen = {"origin": ""}
    code = None
    try:
        await asyncio.wait_for(_fill(endpoint, target_id, allowed, value, budget, cleanup, seen,
                                     FIELDS[field]), budget)
    except _Fail as exc:
        code = exc.code
    except asyncio.TimeoutError:
        code = CDP_UNAVAILABLE
    except Exception:  # noqa: BLE001 - any other failure is the endpoint's, and its text stays out
        code = CDP_UNAVAILABLE
    return FillResult(code is None, seen["origin"], code)


async def _fill(endpoint, target_id, allowed, value, budget, cleanup, seen, function) -> None:
    async with _connected(endpoint, budget, cleanup) as conn:
        origin = await _target_origin(conn, target_id)
        seen["origin"] = origin
        if not origin or origin not in allowed:
            raise _Fail(ORIGIN_MISMATCH)
        await _write(conn, target_id, origin, value, cleanup, function)


def handover(endpoint: str, target_id: str, allowed_origins: tuple[str, ...], cookies, local, session,
             timeout: float = 10.0) -> HandoverResult:
    """Write a sign-in session into ``target_id`` at ``endpoint``. Call from a thread with no running event loop."""
    return asyncio.run(handover_async(endpoint, target_id, allowed_origins, cookies, local, session, timeout))


async def handover_async(endpoint: str, target_id: str, allowed_origins: tuple[str, ...], cookies, local,
                         session, timeout: float = 10.0) -> HandoverResult:
    """Set ``cookies`` and both storages in the tab, then reload it.

    The tab's origin is read first and must be one of ``allowed_origins`` (the item's
    origins, never an IdP's): anything else is ``handover_refused`` and nothing is
    written. ``cookies`` are CDP cookie records from the signer; ``local`` and
    ``session`` are lists of ``[key, value]`` pairs, passed as arguments, never as
    source. The deadline and the connection are ``fill_async``'s.
    """
    if not valid_endpoint(endpoint):
        return HandoverResult(False, "", CDP_UNAVAILABLE)
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        return HandoverResult(False, "", TARGET_NOT_FOUND)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError("timeout must be a positive number")
    if isinstance(allowed_origins, str):
        raise TypeError("allowed_origins must be a collection of origins, not one string")
    allowed = {o for o in (normalize_origin(a) for a in allowed_origins) if o}
    params = _cookie_params(cookies, origin_hosts(allowed))
    entries = (_entries(local), _entries(session))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cleanup = min(1.0, timeout / 5)
    budget = deadline - 2 * cleanup - loop.time()
    seen = {"origin": ""}
    code = None
    try:
        await asyncio.wait_for(_handover(endpoint, target_id, allowed, params, entries, budget, cleanup, seen),
                               budget)
    except _Fail as exc:
        code = exc.code
    except asyncio.TimeoutError:
        code = CDP_UNAVAILABLE
    except Exception:  # noqa: BLE001 - any other failure is the endpoint's, and its text stays out
        code = CDP_UNAVAILABLE
    return HandoverResult(code is None, seen["origin"], code, len(params) if code is None else 0)


async def _handover(endpoint, target_id, allowed, params, entries, budget, cleanup, seen) -> None:
    async with _connected(endpoint, budget, cleanup) as conn:
        origin = await _target_origin(conn, target_id)
        seen["origin"] = origin
        if not origin or origin not in allowed:
            raise _Fail(HANDOVER_REFUSED)
        await _handover_write(conn, target_id, origin, params, entries, cleanup)


async def _handover_write(conn, target_id, origin, params, entries, cleanup) -> None:
    async with _attached(conn, target_id, cleanup) as session:
        if params:
            reply = await conn.call("Network.setCookies", {"cookies": params}, session)
            if "error" in reply:
                raise _Fail(PAGE_ERROR)
        outcome = await _call_on_document(conn, session, _STORAGE_FUNCTION,
                                          [{"value": origin}, {"value": entries[0]}, {"value": entries[1]}])
        if outcome.get("refused") == "origin_changed":
            raise _Fail(HANDOVER_REFUSED)
        if outcome.get("wrote") != 1:
            raise _Fail(PAGE_ERROR)
        reloaded = await conn.call("Page.reload", {"ignoreCache": False}, session)
        if "error" in reloaded:
            raise _Fail(PAGE_ERROR)


def _cookie_params(cookies, hosts: set[str]) -> list[dict]:
    """The signer's cookie records as Network.setCookies parameters, field by field.

    A record comes from a page's jar, so each field is taken only in the type and
    range CDP takes: a bad one would fail the whole call. ``expires`` is left out
    for a session cookie, which CDP reports as -1. A cookie whose domain is not
    one of ``hosts`` or a parent of one is dropped: the signer selects on the same
    rule, and this is the call that writes into the agent's browser.
    """
    out = []
    dropped = 0
    for cookie in cookies or ():
        if not isinstance(cookie, dict) or not isinstance(cookie.get("name"), str) \
                or not isinstance(cookie.get("value"), str):
            continue
        if not cookie_domain_allowed(cookie.get("domain"), hosts):
            dropped += 1
            continue
        param = {"name": cookie["name"], "value": cookie["value"]}
        for key in ("domain", "path"):
            if isinstance(cookie.get(key), str):
                param[key] = cookie[key]
        for key in ("httpOnly", "secure"):
            if isinstance(cookie.get(key), bool):
                param[key] = cookie[key]
        if cookie.get("sameSite") in ("Strict", "Lax", "None"):
            param["sameSite"] = cookie["sameSite"]
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            param["expires"] = float(expires)
        out.append(param)
    if dropped:
        LOG.info("handover dropped %d cookie(s) outside the item's origins", dropped)
    return out


def _entries(items) -> list[list[str]]:
    """``[key, value]`` string pairs from the signer's storage lists."""
    out = []
    for item in items or ():
        if isinstance(item, (list, tuple)) and len(item) == 2 \
                and all(isinstance(part, str) for part in item):
            out.append([item[0], item[1]])
    return out


def valid_endpoint(endpoint) -> bool:
    """True for a provider endpoint this module can open: ``ws://host:port/devtools/browser/<name>``.

    ``wss`` is refused for now: no provider serves it, and a TLS client here would
    need its own trust decision. A query string (the client's token) is allowed and
    never logged.
    """
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    return bool(parts.scheme == "ws" and parts.hostname and port
                and _BROWSER_PATH.fullmatch(parts.path))


@contextlib.asynccontextmanager
async def _connected(endpoint, budget, cleanup):
    """An open browser websocket to ``endpoint``, closed on the way out."""
    try:
        import websockets
    except ImportError:
        raise _Fail(CDP_UNAVAILABLE) from None

    parts = urlsplit(endpoint)
    loop = asyncio.get_running_loop()
    try:
        # Resolved and connected here, so websockets skips its proxy lookup whatever
        # the environment holds, and so a name with several addresses is tried in turn.
        infos = await loop.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_STREAM)
    except OSError:
        raise _Fail(CDP_UNAVAILABLE) from None
    ws = None
    for family, kind, proto, _canon, address in infos:
        sock = socket.socket(family, kind, proto)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, address)
            ws = await websockets.connect(
                endpoint, sock=sock, max_size=_MAX_MESSAGE, compression=None,
                open_timeout=budget, close_timeout=cleanup, ping_interval=None, logger=_WS_LOGGER,
            )
            break
        except (OSError, asyncio.TimeoutError, Exception):  # noqa: BLE001 - try the next address
            sock.close()
            continue
        except BaseException:
            sock.close()
            raise
    if ws is None:
        raise _Fail(CDP_UNAVAILABLE)

    try:
        yield _Connection(ws)
    finally:
        try:
            await asyncio.wait_for(ws.close(), cleanup)
        except Exception:  # noqa: BLE001 - a close that fails or times out does not change the result
            ws.transport.abort()
        except BaseException:
            # Cancelled again during cleanup: drop the connection, then let the cancel through.
            ws.transport.abort()
            raise


async def _target_origin(conn, target_id: str) -> str:
    """The origin of page ``target_id``, through ``Target.getTargetInfo`` on the browser connection.

    A target that is not a page, or that the provider does not show this client, is
    ``target_not_found``: nothing is attached and nothing is written.
    """
    reply = await conn.call("Target.getTargetInfo", {"targetId": target_id})
    body = reply.get("result") if "error" not in reply else None
    info = body.get("targetInfo") if isinstance(body, dict) else None
    if not isinstance(info, dict) or info.get("type") != "page":
        raise _Fail(TARGET_NOT_FOUND)
    return normalize_origin(info.get("url")) or ""


@contextlib.asynccontextmanager
async def _attached(conn, target_id, cleanup):
    """A flat CDP session on ``target_id``, detached on the way out."""
    attached = await conn.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    body = attached.get("result")
    found = body.get("sessionId") if "error" not in attached and isinstance(body, dict) else None
    if not isinstance(found, str) or not found:
        raise _Fail(TARGET_NOT_FOUND)
    try:
        yield found
    finally:
        try:
            await asyncio.wait_for(conn.call("Target.detachFromTarget", {"sessionId": found}), cleanup)
        except Exception:  # noqa: BLE001 - the session ends with the socket anyway
            pass


async def _call_on_document(conn, session, function, arguments) -> dict:
    """Call ``function`` on the page's document and return what it reported."""
    doc = await conn.call("Runtime.evaluate", {"expression": "document", "returnByValue": False,
                                               "silent": True}, session)
    object_id = _result(doc).get("objectId")
    if not isinstance(object_id, str):
        raise _Fail(PAGE_ERROR)
    called = await conn.call("Runtime.callFunctionOn", {
        "objectId": object_id,
        "functionDeclaration": function,
        "arguments": arguments,
        "returnByValue": True,
        "silent": True,
    }, session)
    outcome = _result(called).get("value")
    if not isinstance(outcome, dict):
        raise _Fail(PAGE_ERROR)
    return outcome


async def _write(conn, target_id, origin, value, cleanup, function) -> None:
    async with _attached(conn, target_id, cleanup) as session:
        outcome = await _call_on_document(conn, session, function,
                                          [{"value": origin}, {"value": value}])
        if outcome.get("refused") == "origin_changed":
            raise _Fail(ORIGIN_MISMATCH)
        if outcome.get("refused") == "write_failed":
            raise _Fail(PAGE_ERROR)
        if outcome.get("filled") != 1:
            # wrote: a setter ran, so the value may be in the page even though the field reads empty.
            raise _Fail(WRITE_UNCONFIRMED if outcome.get("wrote") == 1 else NO_FIELD)


def _result(reply: dict) -> dict:
    """The remote object from a Runtime reply, or ``_Fail(PAGE_ERROR)`` for an error or exception."""
    body = reply.get("result")
    if "error" in reply or not isinstance(body, dict) or body.get("exceptionDetails"):
        raise _Fail(PAGE_ERROR)
    obj = body.get("result")
    if not isinstance(obj, dict):
        raise _Fail(PAGE_ERROR)
    return obj


class _Connection:
    """Sends CDP commands and waits for the reply with the same id, skipping events."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0

    async def call(self, method: str, params: dict, session: str | None = None) -> dict:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await self.ws.recv()
            if not isinstance(raw, str):
                continue
            try:
                reply = json.loads(raw)
            except ValueError:
                raise _Fail(CDP_UNAVAILABLE) from None
            if isinstance(reply, dict) and reply.get("id") == self.next_id and reply.get("sessionId") == session:
                return reply
