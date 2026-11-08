"""Write a value into a browser page over the Chrome DevTools Protocol.

The broker runs in the Hermes container's network namespace and fills the page
itself, so the gateway never holds the value. ``fill`` reads ``/json/list`` on
``127.0.0.1:<port>``, refuses a target whose origin is not allowed, attaches to
it through the browser websocket, and calls a function on the page's document
with the value as a ``Runtime.callFunctionOn`` argument. The value is never part
of an expression or function source. ``field="password"`` writes a password
field; ``field="totp"`` writes a one-time code the way core's enter_code does.

``FillResult.error`` is one of five fixed texts. Nothing from the page, from a
CDP error, or the value reaches a result, an exception or a log line. The
websocket code keeps to the asyncio ``websockets.connect`` API that Debian
bookworm's websockets 10.4 has.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .origins import normalize_origin

TARGET_NOT_FOUND = "target_not_found"
ORIGIN_MISMATCH = "origin_mismatch"
NO_FIELD = "no_field"
PAGE_ERROR = "page_error"
CDP_UNAVAILABLE = "cdp_unavailable"

_TARGET_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]{1,64}")
_MAX_BODY = 4 << 20
_MAX_HEADERS = 64 << 10
_MAX_MESSAGE = 4 << 20

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
    return { filled: el.value.length > 0 ? 1 : 0 };
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
  try {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    for (const [el, part] of writes) {
      el.focus();
      if (setter && setter.set) { setter.set.call(el, part); } else { el.value = part; }
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      if (el.value.length > 0) filled += 1;
    }
  } catch (e) {
    // A page handler can throw with the code in its message; report only that the write failed.
    return { refused: "write_failed" };
  }
  return { filled: filled > 0 ? 1 : 0 };
}"""

FIELDS = {"password": _FILL_FUNCTION, "totp": _TOTP_FUNCTION}


@dataclass(frozen=True)
class FillResult:
    filled: bool
    origin: str
    error: str | None


class _Fail(Exception):
    """Internal: carries one of the fixed error texts to ``fill_async``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fill(port: int, target_id: str, allowed_origins: tuple[str, ...], value: str, timeout: float = 10.0,
         field: str = "password") -> FillResult:
    """Fill ``field`` ("password" or "totp") in ``target_id``. Call from a thread with no running event loop."""
    return asyncio.run(fill_async(port, target_id, allowed_origins, value, timeout, field=field))


async def fill_async(port: int, target_id: str, allowed_origins: tuple[str, ...], value: str,
                     timeout: float = 10.0, field: str = "password") -> FillResult:
    if field not in FIELDS:
        raise ValueError('field must be "password" or "totp"')
    if not _valid_port(port):
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
        await asyncio.wait_for(_fill(port, target_id, allowed, value, budget, cleanup, seen, FIELDS[field]), budget)
    except _Fail as exc:
        code = exc.code
    except asyncio.TimeoutError:
        code = CDP_UNAVAILABLE
    except Exception:  # noqa: BLE001 - any other failure is the endpoint's, and its text stays out
        code = CDP_UNAVAILABLE
    return FillResult(code is None, seen["origin"], code)


async def _fill(port, target_id, allowed, value, budget, cleanup, seen, function) -> None:
    listing = await _get_json(port, "/json/list")
    target = None
    if isinstance(listing, list):
        target = next((t for t in listing if isinstance(t, dict) and t.get("id") == target_id
                       and t.get("type") == "page"), None)
    if target is None:
        raise _Fail(TARGET_NOT_FOUND)
    origin = normalize_origin(target.get("url")) or ""
    seen["origin"] = origin
    if not origin or origin not in allowed:
        raise _Fail(ORIGIN_MISMATCH)

    version = await _get_json(port, "/json/version")
    ws_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    path = urlsplit(ws_url).path if isinstance(ws_url, str) else ""
    if not _BROWSER_PATH.fullmatch(path):
        raise _Fail(CDP_UNAVAILABLE)
    await _write(port, path, target_id, origin, value, budget, cleanup, function)


async def _write(port, path, target_id, origin, value, budget, cleanup, function) -> None:
    try:
        import websockets
    except ImportError:
        raise _Fail(CDP_UNAVAILABLE) from None

    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, ("127.0.0.1", port))
        # A connected socket keeps websockets on 127.0.0.1 and skips its proxy lookup,
        # whatever host the browser reported in webSocketDebuggerUrl.
        ws = await websockets.connect(
            f"ws://127.0.0.1:{port}{path}", sock=sock, max_size=_MAX_MESSAGE, compression=None,
            open_timeout=budget, close_timeout=cleanup, ping_interval=None, logger=_WS_LOGGER,
        )
    except BaseException:
        sock.close()
        raise

    conn = _Connection(ws)
    session = None
    try:
        attached = await conn.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        body = attached.get("result")
        found = body.get("sessionId") if "error" not in attached and isinstance(body, dict) else None
        if not isinstance(found, str) or not found:
            raise _Fail(TARGET_NOT_FOUND)
        session = found

        doc = await conn.call("Runtime.evaluate", {"expression": "document", "returnByValue": False,
                                                   "silent": True}, session)
        object_id = _result(doc).get("objectId")
        if not isinstance(object_id, str):
            raise _Fail(PAGE_ERROR)

        called = await conn.call("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": function,
            "arguments": [{"value": origin}, {"value": value}],
            "returnByValue": True,
            "silent": True,
        }, session)
        outcome = _result(called).get("value")
        if not isinstance(outcome, dict):
            raise _Fail(PAGE_ERROR)
        if outcome.get("refused") == "origin_changed":
            raise _Fail(ORIGIN_MISMATCH)
        if outcome.get("refused") == "write_failed":
            raise _Fail(PAGE_ERROR)
        if outcome.get("filled") != 1:
            raise _Fail(NO_FIELD)
    finally:
        try:
            if session is not None:
                try:
                    await asyncio.wait_for(conn.call("Target.detachFromTarget", {"sessionId": session}), cleanup)
                except Exception:  # noqa: BLE001 - the session ends with the socket anyway
                    pass
        finally:
            try:
                await asyncio.wait_for(ws.close(), cleanup)
            except Exception:  # noqa: BLE001 - a close that fails or times out does not change the result
                ws.transport.abort()
            except BaseException:
                # Cancelled again during cleanup: drop the connection, then let the cancel through.
                ws.transport.abort()
                raise


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


async def _get_json(port: int, path: str):
    """GET ``path`` from 127.0.0.1:<port> and parse the body as JSON.

    asyncio streams rather than http.client in a thread: a thread cannot be cancelled, so a
    slow endpoint could hold it past the deadline. No proxy from the environment applies.
    """
    reader = writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=_MAX_HEADERS)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
                     .encode("ascii"))
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        body = await _read_body(reader, head)
    except (OSError, EOFError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
        body = None
    finally:
        if writer is not None:
            writer.transport.abort()
    if body is None:
        raise _Fail(CDP_UNAVAILABLE)
    try:
        return json.loads(body)
    except ValueError:
        raise _Fail(CDP_UNAVAILABLE) from None


async def _read_body(reader: asyncio.StreamReader, head: bytes) -> bytes | None:
    """The body for a 200 response with Content-Length or read-to-close framing, capped at _MAX_BODY."""
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ")
    if len(status) < 2 or not status[0].startswith("HTTP/1.") or status[1] != "200":
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, val = line.partition(":")
        if sep:
            headers[name.strip().lower()] = val.strip()
    if "transfer-encoding" in headers:
        return None  # DevTools sends Content-Length; chunked framing is not needed
    if "content-length" in headers:
        length = int(headers["content-length"])
        if not 0 <= length <= _MAX_BODY:
            return None
        return await reader.readexactly(length)
    body = b""
    while len(body) <= _MAX_BODY:
        more = await reader.read(_MAX_BODY + 1 - len(body))
        if not more:
            return body
        body += more
    return None


def _valid_port(port) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
