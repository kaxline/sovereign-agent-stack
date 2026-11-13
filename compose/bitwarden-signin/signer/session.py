"""One sign-in session: a Chromium, the refs of its last snapshot, and the values it wrote.

Every method takes ``allowed``, the normalized origins the broker permits for
this call, and refuses with ``origin_mismatch`` when the page's origin (from the
main frame's URL) is not among them; ``open`` also checks the URL before
loading it. The broker decides what goes in ``allowed``; the session only
compares.

Element work runs in an isolated world (``Page.createIsolatedWorld``), so page
scripts cannot replace the prototypes or globals the signer's functions use. A
ref resolves through ``DOM.resolveNode`` on its backendDOMNodeId; a ref that is
not in the last snapshot, cannot be resolved, or is no longer connected to the
document gets ``stale_ref``.

``fill`` classifies the element with a function that takes no value, adds the
value to the scrubber, then writes it with ``Runtime.callFunctionOn`` passing
the value only as an argument. Snapshots are scrubbed string by string before
they leave, so a value echoed by the page (in an error line, a title, or a
field whose type a script flipped from password to text) comes back as the
marker.

Errors are ``SessionError`` with a fixed code. ``ends_session`` is set when the
session cannot go on (timeout, Chromium gone); the server then ends it.
"""

from __future__ import annotations

import contextlib
import time

from . import browser, cookies, scrub, snapshot
from .cookies import normalize_origin

SETTLE_SECONDS = 0.3
READY_SECONDS = 10.0
WORLD = "bitwarden-signin"

# Returns what ``fill`` needs to decide on a target; it is never passed the value.
# ``boxes`` counts the run of one-character inputs that starts at this element
# (same form, adjacent in document order); ``first`` is false when the input
# before it in that order is itself a one-character input.
_RUN_HELPERS = r"""
  const skipped = ["hidden", "submit", "button", "reset", "file", "image", "checkbox", "radio"];
  const isBox = (e) => e instanceof HTMLInputElement && e.maxLength === 1 && !skipped.includes(e.type)
    && !e.disabled && !e.readOnly;
  const scope = (e) => Array.from((e.form || e.getRootNode()).querySelectorAll("input"))
    .filter((i) => i.form === e.form);
  const runFrom = (e) => {
    const all = scope(e);
    const at = all.indexOf(e);
    const run = [];
    for (let i = at; i >= 0 && i < all.length && isBox(all[i]); i++) run.push(all[i]);
    return { run, first: at <= 0 || !isBox(all[at - 1]) };
  };
"""

CLASSIFY_FUNCTION = "function () {" + _RUN_HELPERS + r"""
  const el = this;
  const out = { connected: el.isConnected === true, input: false, password: false, otp: false, boxes: 0,
                first: false };
  if (!(el instanceof HTMLInputElement)) return out;
  out.input = true;
  out.password = el.type === "password";
  const tokens = String(el.autocomplete || "").toLowerCase().split(/\s+/);
  out.otp = tokens.includes("one-time-code") && !skipped.includes(el.type) && !el.disabled && !el.readOnly;
  if (isBox(el)) {
    const r = runFrom(el);
    out.boxes = r.run.length;
    out.first = r.first;
  }
  return out;
}"""

# Writes ``value`` (an argument, never part of this source) into this element,
# or one character per box along the run that starts here when ``mode`` is
# "boxes". The native setter is taken from this isolated world's prototype.
WRITE_FUNCTION = "function (value, mode) {" + _RUN_HELPERS + r"""
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const put = (el, part) => {
    el.focus();
    setter.call(el, part);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, inputType: "insertText" }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  };
  const chars = Array.from(value);
  if (mode === "boxes") {
    const run = runFrom(this).run;
    if (chars.length > run.length) return 0;
    chars.forEach((c, i) => put(run[i], c));
    return chars.length;
  }
  put(this, value);
  return 1;
}"""

FOCUS_FUNCTION = r"""function () {
  this.focus();
  if (typeof this.select === "function") this.select();
}"""

CLICK_FUNCTION = "function () { this.click(); }"

READY_EXPRESSION = "document.readyState"


class SessionError(Exception):
    def __init__(self, code: str, ends_session: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.ends_session = ends_session


def url_allowed(url: str, allowed: list[str]) -> bool:
    """An http(s) URL whose normalized origin is in ``allowed``."""
    scheme = url.split(":", 1)[0].lower() if isinstance(url, str) else ""
    return scheme in ("http", "https") and normalize_origin(url) in allowed


def _scrubbed(obj, scrubber: scrub.Scrubber):
    if isinstance(obj, str):
        return scrubber.apply(obj)
    if isinstance(obj, list):
        return [_scrubbed(v, scrubber) for v in obj]
    if isinstance(obj, dict):
        return {k: _scrubbed(v, scrubber) for k, v in obj.items()}
    return obj


class Session:
    def __init__(self, chromium: browser.Chromium, action_seconds: float) -> None:
        self.chromium = chromium
        self.action_seconds = action_seconds
        self.scrubber = scrub.Scrubber()
        self._refs: dict[int, int] = {}
        self.last_action = time.monotonic()

    # --- public actions ----------------------------------------------------

    def open(self, url: str, allowed: list[str]) -> dict:
        if not url_allowed(url, allowed):
            raise SessionError("origin_mismatch")
        with self._action():
            result = self.chromium.page("Page.navigate", {"url": url})
            if result.get("errorText"):
                raise SessionError("page_error")
            self._settle()
            return self._snapshot(allowed)

    def snapshot(self, allowed: list[str]) -> dict:
        with self._action():
            return self._snapshot(allowed)

    def click(self, ref: int, allowed: list[str]) -> dict:
        with self._action():
            self._origin(allowed)
            backend, obj = self._resolve(ref)
            self._click(backend, obj)
            self._settle()
            return self._snapshot(allowed)

    def type(self, ref: int, text: str, allowed: list[str]) -> dict:
        with self._action():
            self._origin(allowed)
            _, obj = self._resolve(ref)
            if self._classify(obj)["password"]:
                raise SessionError("not_a_secret_field")
            self._call_on(obj, FOCUS_FUNCTION)
            self.chromium.page("Input.insertText", {"text": text})
            self._settle()
            return self._snapshot(allowed)

    def fill(self, ref: int, field: str, value: str, allowed: list[str]) -> dict:
        with self._action():
            self._origin(allowed)
            _, obj = self._resolve(ref)
            kind = self._classify(obj)
            if field == "password" and kind["password"]:
                mode = "single"
            elif field == "totp" and kind["otp"]:
                mode = "single"
            elif field == "totp" and kind["first"] and kind["boxes"] >= 2 and len(value) <= kind["boxes"]:
                mode = "boxes"
            else:
                raise SessionError("not_a_secret_field")
            self.scrubber.add(value)
            wrote = self._call_on(obj, WRITE_FUNCTION, [{"value": value}, {"value": mode}])
            if not wrote:
                raise SessionError("not_a_secret_field")
            self._settle()
            return self._snapshot(allowed)

    def export(self, allowed: list[str], item_origins: list[str]) -> dict:
        with self._action():
            origin = self._origin(allowed)
            jar = self.chromium.call("Storage.getCookies").get("cookies") or []
            out = {"origin": origin, "cookies": cookies.select(jar, tuple(item_origins)),
                   "localStorage": [], "sessionStorage": []}
            if origin in item_origins:
                self.chromium.page("DOMStorage.enable")
                for key, local in (("localStorage", True), ("sessionStorage", False)):
                    items = self.chromium.page("DOMStorage.getDOMStorageItems", {
                        "storageId": {"securityOrigin": origin, "isLocalStorage": local}}).get("entries") or []
                    out[key] = [[k, v] for k, v in items if isinstance(k, str) and isinstance(v, str)]
            return out

    def end(self) -> dict:
        self.chromium.stop()
        self.scrubber.clear()
        self._refs = {}
        return {"ended": True}

    # --- helpers -----------------------------------------------------------

    @contextlib.contextmanager
    def _action(self):
        """Bound one action by ``action_seconds`` and map browser errors to codes."""
        self.last_action = time.monotonic()
        self.chromium.deadline = self.last_action + self.action_seconds
        try:
            yield
        except browser.Timeout:
            raise SessionError("timeout", ends_session=True) from None
        except browser.BrowserGone:
            raise SessionError("page_error", ends_session=True) from None
        except (browser.CdpError, KeyError, TypeError, ValueError):
            raise SessionError("page_error") from None
        finally:
            self.chromium.deadline = None
            self.last_action = time.monotonic()

    def _frame(self) -> dict:
        return self.chromium.page("Page.getFrameTree")["frameTree"]["frame"]

    def _origin(self, allowed: list[str]) -> str:
        origin = normalize_origin(self._frame().get("url") or "")
        if origin is None or origin not in allowed:
            raise SessionError("origin_mismatch")
        return origin

    def _world(self) -> int:
        frame_id = self._frame()["id"]
        return self.chromium.page("Page.createIsolatedWorld", {
            "frameId": frame_id, "worldName": WORLD, "grantUniveralAccess": False})["executionContextId"]

    def _resolve(self, ref: int) -> tuple[int, str]:
        backend = self._refs.get(ref)
        if backend is None:
            raise SessionError("stale_ref")
        context = self._world()
        try:
            obj = self.chromium.page("DOM.resolveNode", {"backendNodeId": backend,
                                                         "executionContextId": context})["object"]["objectId"]
        except (browser.CdpError, KeyError):
            raise SessionError("stale_ref") from None
        if not self._classify(obj)["connected"]:
            raise SessionError("stale_ref")
        return backend, obj

    def _call_on(self, obj: str, function: str, args: list | None = None):
        result = self.chromium.page("Runtime.callFunctionOn", {
            "objectId": obj, "functionDeclaration": function, "arguments": args or [],
            "returnByValue": True, "awaitPromise": False})
        if result.get("exceptionDetails"):
            # The details can quote the page's message or an argument; only the fact is kept.
            raise SessionError("page_error")
        return (result.get("result") or {}).get("value")

    def _classify(self, obj: str) -> dict:
        kind = self._call_on(obj, CLASSIFY_FUNCTION)
        if not isinstance(kind, dict):
            raise SessionError("page_error")
        return kind

    def _click(self, backend: int, obj: str) -> None:
        try:
            self.chromium.page("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
            quads = self.chromium.page("DOM.getContentQuads", {"backendNodeId": backend}).get("quads") or []
        except browser.CdpError:
            quads = []
        if not quads:
            self._call_on(obj, CLICK_FUNCTION)
            return
        q = quads[0]
        x = sum(q[0::2]) / 4
        y = sum(q[1::2]) / 4
        self.chromium.page("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind in ("mousePressed", "mouseReleased"):
            self.chromium.page("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                            "clickCount": 1})

    def _settle(self) -> None:
        """Give a click or write time to start a navigation, then wait for the document to load."""
        time.sleep(SETTLE_SECONDS)
        end = time.monotonic() + READY_SECONDS
        while time.monotonic() < end:
            try:
                context = self._world()
                ready = self.chromium.page("Runtime.evaluate", {
                    "expression": READY_EXPRESSION, "contextId": context, "returnByValue": True})
                if (ready.get("result") or {}).get("value") == "complete":
                    return
            except browser.CdpError:
                pass  # the context went away mid-navigation; try the new one
            time.sleep(0.1)

    def _snapshot(self, allowed: list[str]) -> dict:
        origin = self._origin(allowed)
        nodes = self.chromium.page("Accessibility.getFullAXTree").get("nodes") or []
        items, self._refs = snapshot.reduce(nodes)
        return {"origin": origin, "nodes": _scrubbed(items, self.scrubber)}
