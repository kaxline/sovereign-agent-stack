#!/usr/bin/env python3
"""Checks for the Bitwarden sign-in signer's pure parts (compose/bitwarden-signin/signer).

Host part: scrub.py, snapshot.py and cookies.py run without a browser. The
scrub test compares the signer's encodings with the broker's
digest_redaction.FORMS so the two copies stay equal. Values are synthetic.

Image part (IMAGE_DRIVER): the signer's socket server, sessions and the Hermes
image's Chromium with its sandbox on, against a test site the driver serves on
127.0.0.1 and localhost (two origins) inside the same container. The container
runs as a non-root user with seccomp=unconfined so the namespace sandbox can
start; Task 4 replaces that with the service's seccomp profile.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import hermes_image  # noqa: E402
from testkit import expect  # noqa: E402

SIGNER_SRC = ROOT / "compose" / "bitwarden-signin"
BROKER_SRC = ROOT / "compose" / "bitwarden-broker"
sys.path.insert(0, str(SIGNER_SRC))
sys.path.insert(0, str(BROKER_SRC))

from broker import digest_redaction  # noqa: E402
from signer import cookies, scrub, snapshot  # noqa: E402

MARKER = "«redacted-vault-secret»"
SECRET = "pa ss&wö\r\nrd/CANARY+1="


def test_scrub_matches_digest_forms() -> None:
    s = scrub.Scrubber()
    s.add(SECRET)
    stripped = SECRET.replace("\r", "").replace("\n", "")
    want = {digest_redaction._encode(v, f) for v in (SECRET, stripped) for f in digest_redaction.FORMS}
    expect(s.patterns() == want, "scrubber encodings equal the broker's digest forms")
    expect(scrub.MARKER == MARKER, "marker")


def test_scrub_replaces_each_form() -> None:
    s = scrub.Scrubber()
    s.add(SECRET)
    stripped = SECRET.replace("\r", "").replace("\n", "")
    for v in (SECRET, stripped):
        for form in digest_redaction.FORMS:
            enc = digest_redaction._encode(v, form).decode("utf-8")
            out = s.apply(f"before {enc} after")
            expect(out == f"before {MARKER} after", f"{form} of {v!r} replaced: {out!r}")
    expect(s.apply("nothing here") == "nothing here", "clean text unchanged")
    s.clear()
    expect(s.apply(SECRET) == SECRET, "clear forgets values")
    s.add("")
    expect(s.apply("abc") == "abc", "empty value ignored")


def _name(value, sources):
    return {"type": "computedString", "value": value, "sources": sources}


def _node(i, role, name="", *, sources=(), value=None, ignored=False, backend=None, children=()):
    """A node in the shape Accessibility.getFullAXTree returns (string nodeIds)."""
    n = {"nodeId": str(i), "ignored": ignored, "role": {"type": "role", "value": role},
         "name": _name(name, list(sources)), "childIds": [str(c) for c in children],
         "backendDOMNodeId": backend if backend is not None else 100 + i}
    if value is not None:
        n["value"] = {"type": "string", "value": value}
    return n


LABEL = [{"type": "attribute", "attribute": "aria-label", "value": {"type": "computedString", "value": "x"}}]
PLACEHOLDER = [{"type": "attribute", "attribute": "aria-label", "superseded": True},
               {"type": "placeholder", "attribute": "placeholder", "value": {"type": "computedString", "value": "x"}}]
CONTENTS = [{"type": "contents", "value": {"type": "computedString", "value": "x"}}]


def _fixture() -> list[dict]:
    return [
        _node(1, "RootWebArea", "Sign in", children=[3, 4, 5, 6, 7, 8]),
        _node(2, "generic", ignored=True),
        _node(3, "textbox", "Password", sources=LABEL, value="CANARY-pw", children=[9]),
        _node(4, "textbox", "CANARY-user", sources=CONTENTS, value="CANARY-user"),
        _node(5, "searchbox", "CANARY-q", sources=[], value="CANARY-q"),
        _node(6, "combobox", "CANARY-c", value="CANARY-c"),
        _node(7, "button", "Continue", sources=CONTENTS),
        _node(8, "StaticText", "Welcome back"),
        _node(9, "generic", children=[10]),
        _node(10, "StaticText", "CANARY-pw", children=[11]),
        _node(11, "InlineTextBox", "CANARY-pw"),
    ]


def test_reduce_drops_values() -> None:
    items, refs = snapshot.reduce(_fixture())
    expect("CANARY" not in json.dumps(items, ensure_ascii=False), f"no value text in {items}")
    by_role = {i["role"]: i for i in items}
    expect(by_role["button"]["name"] == "Continue", "button name kept")
    expect(items[1]["role"] == "textbox" and items[1]["name"] == "Password", "labelled textbox name kept")
    expect(by_role["StaticText"]["text"] == "Welcome back", "static text kept as text")
    expect(set(items[0]) == {"ref", "role", "name", "text"}, "item keys")
    expect(refs[items[1]["ref"]] == 103, "ref maps to backendDOMNodeId")


def test_reduce_skips_field_descendants() -> None:
    items, _ = snapshot.reduce(_fixture())
    expect([i["role"] for i in items] == ["RootWebArea", "textbox", "textbox", "searchbox", "combobox", "button",
                                          "StaticText"], f"field children skipped: {items}")


def test_reduce_placeholder_name_kept() -> None:
    items, _ = snapshot.reduce([_node(1, "textbox", "Search", sources=PLACEHOLDER, value="CANARY")])
    expect(items[0]["name"] == "Search", "placeholder name kept")


def test_reduce_real_chromium_tree() -> None:
    # Captured from the Hermes image's Chromium (nousresearch/hermes-agent:v0.23.1) on a page with a
    # labelled input holding a value, a password input, a placeholder input with a value, a
    # placeholder-only input, a labelled textarea with a value and a button.
    nodes = json.loads((ROOT / "scripts" / "fixtures" / "bitwarden-signer-ax.json").read_text())
    raw = json.dumps(nodes, ensure_ascii=False)
    expect("CANARY-user" in raw and "CANARY-pass" not in raw.replace("\u2022", "") or "CANARY" in raw,
           "fixture holds typed values")
    items, refs = snapshot.reduce(nodes)
    out = json.dumps(items, ensure_ascii=False)
    expect("CANARY" not in out and "\u2022" not in out and "•" not in out, f"no typed text: {out}")
    names = {i["name"] for i in items if i["role"] == "textbox"}
    expect(names == {"Username", "Password", "Search site", "Only placeholder", "Notes"}, f"labels kept: {names}")
    expect(any(i["role"] == "button" and i["name"] == "Continue" for i in items), "button kept")
    expect(len(refs) == len(items), "every item has a backend id")


def test_reduce_refs_stable_order() -> None:
    a, ra = snapshot.reduce(_fixture())
    b, rb = snapshot.reduce(_fixture())
    expect(a == b and ra == rb, "same input, same refs")
    expect([i["ref"] for i in a] == list(range(1, len(a) + 1)), "refs numbered from 1 in order")
    expect(ra[1] == 101 and ra[2] == 103, "ignored node takes no ref")


def _c(domain, name="s", **kw):
    base = {"name": name, "value": "v", "domain": domain, "path": "/", "expires": -1, "httpOnly": True,
            "secure": True, "sameSite": "Lax", "size": 9, "session": True}
    base.update(kw)
    return base


def test_select_parent_domain_and_excludes_sibling_and_idp() -> None:
    jar = [_c(".example.com", "parent"), _c("app.example.com", "exact"), _c("other.example.com", "sibling"),
           _c("sso.idp.test", "idp"), _c("example.com", "hostonly-parent"), _c(".idp.test", "idp-domain"),
           _c(".com", "tld")]
    out = cookies.select(jar, ("https://app.example.com",))
    names = sorted(c["name"] for c in out)
    expect(names == ["exact", "parent"], f"selected {names}")
    expect("tld" not in names, "a bare TLD domain cookie is not kept")
    expect(set(out[0]) == {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"},
           "reduced keys")
    out2 = cookies.select(jar, ("https://app.example.com", "https://sso.idp.test:8443"))
    expect(sorted(c["name"] for c in out2) == ["exact", "idp", "idp-domain", "parent"], "several origins")
    expect(cookies.select(jar, ()) == [], "no origins, no cookies")


# Chromium's namespace sandbox refuses to start as root and needs clone/unshare with
# CLONE_NEWUSER, which Docker's default seccomp profile blocks.
IMAGE_DOCKER_ARGS = ("--network", "none", "--user", "10001:10001", "-e", "HOME=/tmp",
                     "--security-opt", "seccomp=unconfined")

IMAGE_DRIVER = r'''
import glob
import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/signin-src")
from signer import browser, scrub, server

CANARY = "pa ss&wö\r\nrd/CANARY+1="
MARKER = "«redacted-vault-secret»"
PROFILES = "/tmp/bitwarden-signin-*"


def fail(msg):
    sys.stdout.flush()
    print(f"FAIL {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def expect(cond, msg):
    if not cond:
        fail(msg)


records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.DEBUG)

# --- test site: origin A on 127.0.0.1, origin B on localhost -----------------

PAGES = {
    "/login": """<html><head><title>Sign in</title></head><body>
<label for=u>Username</label><input id=u autocomplete=username>
<label for=p>Password</label><input id=p type=password autocomplete=current-password>
<button id=show onclick="var p=document.getElementById('p');p.type=p.type==='password'?'text':'password'">Show password</button>
<label for=n>Notes</label><input id=n type=text>
<div id=err></div>
<script>
function enc(s) {
  const b = new TextEncoder().encode(s);
  return [s, btoa(String.fromCharCode(...b)), Array.from(b, (x) => x.toString(16).padStart(2, "0")).join(""),
          encodeURIComponent(s), s.split("").reverse().join("")];
}
document.getElementById("p").addEventListener("input", (e) => {
  document.getElementById("err").textContent = "Wrong password: " + enc(e.target.value).join(" | ");
  document.title = e.target.value;
});
</script></body></html>""",
    "/boxes": """<html><body><form>
<input aria-label="Digit 1" maxlength=1><input aria-label="Digit 2" maxlength=1><input aria-label="Digit 3" maxlength=1>
<input aria-label="Digit 4" maxlength=1><input aria-label="Digit 5" maxlength=1><input aria-label="Digit 6" maxlength=1>
</form><div id=st>Boxes filled: 0</div>
<label for=c>Code</label><input id=c autocomplete=one-time-code>
<div id=cl>Code length: 0</div>
<script>
document.addEventListener("input", () => {
  const n = Array.from(document.querySelectorAll("input[maxlength='1']")).filter((i) => i.value.length === 1).length;
  document.getElementById("st").textContent = "Boxes filled: " + n;
  document.getElementById("cl").textContent = "Code length: " + document.getElementById("c").value.length;
});
</script></body></html>""",
    "/gone": """<html><body><button id=g>Vanishing</button><button>Stays</button>
<script>setTimeout(() => document.getElementById("g").remove(), 800);</script></body></html>""",
    "/landing": "<html><body><p>Landing page</p></body></html>",
    "/account": """<html><body><p>Account</p><script>
localStorage.setItem("token", "t-1"); sessionStorage.setItem("tab", "x-1");
</script></body></html>""",
    "/hang": """<html><body><button onclick="while (true) {}">Hang</button></body></html>""",
    "/store": """<html><body>
<label for=p>Password</label><input id=p type=password>
<button onclick="store()">Sign in</button><div id=done></div>
<script>
function store() {
  const v = document.getElementById("p").value;
  const b = new TextEncoder().encode(v);
  localStorage.setItem("pw", v);
  localStorage.setItem("pw64", btoa(String.fromCharCode(...b)));
  localStorage.setItem(Array.from(b, (x) => x.toString(16).padStart(2, "0")).join(""), "by-key");
  sessionStorage.setItem("rev", v.split("").reverse().join(""));
  localStorage.setItem("token", "t-2");
  sessionStorage.setItem("tab", "x-2");
  document.cookie = "pwc=" + encodeURIComponent(v) + "; path=/";
  document.cookie = "sess=s-2; path=/";
  document.getElementById("done").textContent = "Stored";
}
</script></body></html>""",
}

PORTS = {}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body="", headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/redirect":
            return self._send(302, headers=[("Location", f"http://localhost:{PORTS['b']}/landing")])
        if path == "/idp":
            return self._send(200, f"<html><body><a href='http://127.0.0.1:{PORTS['a']}/account'>Continue to app</a>"
                                   "</body></html>", [("Set-Cookie", "idp=i-1; Path=/")])
        if path == "/account":
            return self._send(200, PAGES[path], [("Set-Cookie", "sid=s-1; Path=/; HttpOnly")])
        if path in PAGES:
            return self._send(200, PAGES[path])
        self._send(404, "nf")


for key, host in (("a", "127.0.0.1"), ("b", "127.0.0.1")):
    srv = ThreadingHTTPServer((host, 0), Site)
    PORTS[key] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

A = f"http://127.0.0.1:{PORTS['a']}"
B = f"http://localhost:{PORTS['b']}"

# --- socket client ------------------------------------------------------------


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=90)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(90)
        s.connect(self._path)
        self.sock = s


def call(sock, route, body=None, method="POST"):
    conn = UnixConn(sock)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, route, body=data, headers={"Content-Type": "application/json"} if data is not None else {})
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    return resp.status, json.loads(raw)


def boot(name, idle="5", action="30", start_browser=None):
    env = {"BITWARDEN_SIGNIN_SOCKET": f"/tmp/sock-{name}/signin.sock",
           "BITWARDEN_SIGNIN_IDLE_MINUTES": idle, "BITWARDEN_SIGNIN_ACTION_SECONDS": action}
    kw = {"start_browser": start_browser} if start_browser else {}
    signer = server.Signer(server.load_config(env), **kw)
    signer.listen()
    threading.Thread(target=signer.serve, daemon=True).start()
    return signer, env["BITWARDEN_SIGNIN_SOCKET"]


def ref_of(snap, role, name="", text=""):
    for n in snap["nodes"]:
        if n["role"] == role and (not name or n["name"] == name) and (not text or text in n["text"]):
            return n["ref"]
    fail(f"no {role} {name or text!r} in {snap}")


def texts(snap):
    return " ".join(n["text"] for n in snap["nodes"])


def patterns():
    s = scrub.Scrubber()
    s.add(CANARY)
    return [p.decode("utf-8") for p in s.patterns()]


def no_canary(text, where):
    for p in patterns():
        expect(p not in text, f"canary encoding {p!r} in {where}")


MAIN, SOCK = boot("main")


def end():
    status, body = call(SOCK, "/session/end", {})
    expect(status == 200, f"end: {status} {body}")
    return body


def open_page(path, allowed=None, url=None):
    status, snap = call(SOCK, "/session/open", {"url": url or A + path, "allowed": allowed or [A]})
    expect(status == 200, f"open {path}: {status} {snap}")
    expect(snap["origin"] == (allowed or [A])[0] or snap["origin"] in (allowed or [A]), f"origin {snap}")
    return snap


def test_health_and_socket_mode():
    import stat
    mode = stat.S_IMODE(os.stat(SOCK).st_mode)
    expect(mode == 0o600, f"socket mode {oct(mode)}")
    expect(call(SOCK, "/health", method="GET") == (200, {"status": "ready"}), "health ready")


def test_open_snapshot_has_no_values():
    snap = open_page("/login")
    expect(set(snap) == {"origin", "nodes"} and snap["origin"] == A, f"keys {snap.keys()}")
    names = {n["name"] for n in snap["nodes"] if n["role"] == "textbox"}
    expect(names == {"Username", "Password", "Notes"}, f"labels {names}")
    user = ref_of(snap, "textbox", "Username")
    status, snap = call(SOCK, "/session/type", {"ref": user, "text": "CANARY-user-typed", "allowed": [A]})
    expect(status == 200, f"type: {status} {snap}")
    status, snap = call(SOCK, "/session/snapshot", {"allowed": [A]})
    expect(status == 200, f"snapshot: {status}")
    expect("CANARY" not in json.dumps(snap, ensure_ascii=False), f"typed value in snapshot: {snap}")
    end()


def test_fill_password_then_snapshot_scrubbed():
    records.clear()
    snap = open_page("/login")
    pw = ref_of(snap, "textbox", "Password")
    status, snap = call(SOCK, "/session/fill", {"ref": pw, "field": "password", "value": CANARY, "allowed": [A]})
    expect(status == 200, f"fill: {status} {snap}")
    text = json.dumps(snap, ensure_ascii=False)
    no_canary(text, "fill snapshot")
    expect(MARKER in text, f"the page echo was scrubbed, not missing: {text}")
    show = ref_of(snap, "button", "Show password")
    status, snap = call(SOCK, "/session/click", {"ref": show, "allowed": [A]})
    expect(status == 200, f"click: {status} {snap}")
    status, snap = call(SOCK, "/session/snapshot", {"allowed": [A]})
    text = json.dumps(snap, ensure_ascii=False)
    no_canary(text, "snapshot after the toggle")
    expect(MARKER in text, "marker after the toggle")
    expect("Wrong password" in text, "echo line still shown")
    end()
    no_canary("\n".join(records), "logs")


def test_fill_refuses_text_input():
    snap = open_page("/login")
    notes = ref_of(snap, "textbox", "Notes")
    pw = ref_of(snap, "textbox", "Password")
    for ref, field in ((notes, "password"), (notes, "totp"), (pw, "totp")):
        status, body = call(SOCK, "/session/fill", {"ref": ref, "field": field, "value": CANARY, "allowed": [A]})
        expect((status, body) == (422, {"error": "not_a_secret_field"}), f"fill {field} on {ref}: {status} {body}")
    status, body = call(SOCK, "/session/fill", {"ref": pw, "field": "x", "value": "v", "allowed": [A]})
    expect((status, body) == (400, {"error": "bad_request"}), f"bad field: {status} {body}")
    end()


def test_type_refuses_password_input():
    snap = open_page("/login")
    pw = ref_of(snap, "textbox", "Password")
    status, body = call(SOCK, "/session/type", {"ref": pw, "text": "guess", "allowed": [A]})
    expect((status, body) == (422, {"error": "not_a_secret_field"}), f"type into password: {status} {body}")
    end()


def test_fill_totp_targets():
    snap = open_page("/boxes")
    first = ref_of(snap, "textbox", "Digit 1")
    second = ref_of(snap, "textbox", "Digit 2")
    status, body = call(SOCK, "/session/fill", {"ref": second, "field": "totp", "value": "123456", "allowed": [A]})
    expect((status, body) == (422, {"error": "not_a_secret_field"}), f"second box: {status} {body}")
    status, snap = call(SOCK, "/session/fill", {"ref": first, "field": "totp", "value": "123456", "allowed": [A]})
    expect(status == 200 and "Boxes filled: 6" in texts(snap), f"boxes: {status} {snap}")
    code = ref_of(snap, "textbox", "Code")
    status, snap = call(SOCK, "/session/fill", {"ref": code, "field": "totp", "value": "654321", "allowed": [A]})
    expect(status == 200 and "Code length: 6" in texts(snap), f"one-time-code: {status} {snap}")
    expect("123456" not in json.dumps(snap) and "654321" not in json.dumps(snap), "codes absent")
    end()


def test_origin_mismatch_after_redirect():
    status, body = call(SOCK, "/session/open", {"url": B + "/landing", "allowed": [A]})
    expect((status, body) == (403, {"error": "origin_mismatch"}), f"disallowed url: {status} {body}")
    expect(call(SOCK, "/health", method="GET")[1] == {"status": "ready"}, "no session for a disallowed url")
    status, body = call(SOCK, "/session/open", {"url": A + "/redirect", "allowed": [A]})
    expect((status, body) == (403, {"error": "origin_mismatch"}), f"redirect: {status} {body}")
    status, body = call(SOCK, "/session/snapshot", {"allowed": [A]})
    expect((status, body) == (403, {"error": "origin_mismatch"}), f"snapshot after redirect: {status} {body}")
    status, body = call(SOCK, "/session/snapshot", {"allowed": [A, B]})
    expect(status == 200 and body["origin"] == B, f"B allowed: {status} {body}")
    expect(end() == {"ended": True}, "ended")


def test_stale_ref():
    snap = open_page("/gone")
    gone = ref_of(snap, "button", "Vanishing")
    time.sleep(1.5)
    status, body = call(SOCK, "/session/click", {"ref": gone, "allowed": [A]})
    expect((status, body) == (409, {"error": "stale_ref"}), f"removed node: {status} {body}")
    status, body = call(SOCK, "/session/click", {"ref": 999, "allowed": [A]})
    expect((status, body) == (409, {"error": "stale_ref"}), f"unknown ref: {status} {body}")
    end()


def test_export_cookie_and_storage():
    snap = open_page("/idp", allowed=[B, A], url=B + "/idp")
    link = ref_of(snap, "link", "Continue to app")
    status, snap = call(SOCK, "/session/click", {"ref": link, "allowed": [A, B]})
    expect(status == 200 and snap["origin"] == A, f"click to app: {status} {snap}")
    status, out = call(SOCK, "/session/export", {"allowed": [A, B], "item_origins": [A]})
    expect(status == 200, f"export: {status} {out}")
    expect(set(out) == {"origin", "cookies", "localStorage", "sessionStorage"}, f"keys {out.keys()}")
    expect(out["origin"] == A, "origin")
    expect([(c["name"], c["value"]) for c in out["cookies"]] == [("sid", "s-1")], f"cookies {out['cookies']}")
    expect(out["localStorage"] == [["token", "t-1"]], f"localStorage {out['localStorage']}")
    expect(out["sessionStorage"] == [["tab", "x-1"]], f"sessionStorage {out['sessionStorage']}")
    status, out = call(SOCK, "/session/export", {"allowed": [A, B], "item_origins": [B]})
    expect(status == 200 and [c["name"] for c in out["cookies"]] == ["idp"], f"idp cookies {out}")
    expect(out["localStorage"] == [] and out["sessionStorage"] == [], "no storage off the item origin")
    status, out = call(SOCK, "/session/export", {"allowed": [B], "item_origins": [B]})
    expect((status, out) == (403, {"error": "origin_mismatch"}), f"export off allowed: {status} {out}")
    end()


def test_export_drops_written_values():
    snap = open_page("/store")
    pw = ref_of(snap, "textbox", "Password")
    status, snap = call(SOCK, "/session/fill", {"ref": pw, "field": "password", "value": CANARY, "allowed": [A]})
    expect(status == 200, f"fill: {status} {snap}")
    status, snap = call(SOCK, "/session/click", {"ref": ref_of(snap, "button", "Sign in"), "allowed": [A]})
    expect(status == 200 and "Stored" in texts(snap), f"store: {status} {snap}")
    status, out = call(SOCK, "/session/export", {"allowed": [A], "item_origins": [A]})
    expect(status == 200, f"export: {status} {out}")
    no_canary(json.dumps(out, ensure_ascii=False), "export")
    expect([(c["name"], c["value"]) for c in out["cookies"]] == [("sess", "s-2")], f"cookies {out['cookies']}")
    expect(out["localStorage"] == [["token", "t-2"]], f"localStorage {out['localStorage']}")
    expect(out["sessionStorage"] == [["tab", "x-2"]], f"sessionStorage {out['sessionStorage']}")
    end()


def test_snapshot_origin_recheck():
    """A navigation committing between the origin check and the tree read is refused.

    The race is forced: the tree call is wrapped so it navigates to the other
    origin, and waits for the commit, right after the tree comes back.
    """
    from signer import session as signer_session
    chromium = browser.Chromium.start()
    try:
        s = signer_session.Session(chromium, 30)
        s.open(A + "/landing", [A])
        real = chromium.page
        race = {"to": None}

        def racing(method, params=None):
            out = real(method, params)
            if method == "Accessibility.getFullAXTree" and race["to"]:
                real("Page.navigate", {"url": race["to"] + "/landing"})
                end_at = time.monotonic() + 10
                while time.monotonic() < end_at:
                    if real("Page.getFrameTree")["frameTree"]["frame"]["url"].startswith(race["to"]):
                        break
                    time.sleep(0.05)
            return out

        chromium.page = racing
        for start, to, allowed in ((A, B, [A, B]), (B, A, [A, B]), (A, B, [A])):
            race["to"] = to
            try:
                s.snapshot(allowed)
            except signer_session.SessionError as exc:
                expect(exc.code == "origin_mismatch", f"{start}->{to} {allowed}: {exc.code}")
            else:
                fail(f"{start}->{to} with {allowed}: snapshot returned")
            if to == A:
                continue
            race["to"] = None
            time.sleep(0.3)
            if allowed == [A, B]:
                expect(s.snapshot(allowed)["origin"] == B, "no race, same origin, snapshot returned")
        s.end()
    finally:
        chromium.stop()


def crashpad_handlers():
    return [(int(e), open(f"/proc/{e}/cmdline", "rb").read().split(b"\0")) for e in os.listdir("/proc")
            if e.isdigit() and os.path.exists(f"/proc/{e}/cmdline")
            and open(f"/proc/{e}/cmdline", "rb").read().split(b"\0")[0].endswith(b"/chrome_crashpad_handler")]


def test_crashpad_contained():
    open_page("/landing")
    found = profiles()
    expect(len(found) == 1, f"one profile: {found}")
    want = f"--database={found[0]}/crash".encode()
    handlers = crashpad_handlers()
    # This Chromium build starts crashpad whatever the flags; its database must be in the profile.
    expect(all(want in argv for _, argv in handlers), f"crashpad database outside the profile: {handlers}")
    end()
    expect(crashpad_handlers() == [], f"crashpad left after end: {crashpad_handlers()}")
    expect(not os.path.exists("/tmp/.config/chromium") and not glob.glob("/tmp/org.chromium.*"),
           f"Chromium wrote outside the profile: {os.listdir('/tmp')}")


def test_busy_second_open():
    open_page("/landing")
    status, body = call(SOCK, "/session/open", {"url": A + "/login", "allowed": [A]})
    expect((status, body) == (409, {"error": "busy"}), f"second open: {status} {body}")
    expect(call(SOCK, "/health", method="GET")[1] == {"status": "busy"}, "health busy")
    expect(end() == {"ended": True}, "ended")
    expect(call(SOCK, "/health", method="GET")[1] == {"status": "ready"}, "health ready after end")


def test_end_without_session():
    expect(end() == {"ended": False}, "nothing to end")
    for route, body in (("/session/snapshot", {"allowed": [A]}), ("/session/click", {"ref": 1, "allowed": [A]}),
                        ("/session/type", {"ref": 1, "text": "x", "allowed": [A]}),
                        ("/session/fill", {"ref": 1, "field": "password", "value": "x", "allowed": [A]}),
                        ("/session/export", {"allowed": [A], "item_origins": [A]})):
        status, out = call(SOCK, route, body)
        expect((status, out) == (409, {"error": "no_session"}), f"{route}: {status} {out}")


def test_bad_requests():
    status, body = call(SOCK, "/session/open", {"url": A, "allowed": [A], "extra": 1})
    expect((status, body) == (400, {"error": "bad_request"}), f"unknown key: {status} {body}")
    status, body = call(SOCK, "/session/open", {"url": A, "allowed": "x"})
    expect((status, body) == (400, {"error": "bad_request"}), f"allowed not a list: {status} {body}")
    status, body = call(SOCK, "/session/click", {"ref": True, "allowed": [A]})
    expect((status, body) == (400, {"error": "bad_request"}), f"bool ref: {status} {body}")
    status, body = call(SOCK, "/session/open", method="GET")
    expect((status, body) == (405, {"error": "method_not_allowed"}), f"GET open: {status} {body}")
    status, body = call(SOCK, "/nope", {})
    expect((status, body) == (404, {"error": "not_found"}), f"unknown route: {status} {body}")


def profiles():
    return glob.glob(PROFILES)


def chrome_running(_profile=None):
    """Chromium processes left (browser, crashpad, zygote, renderer): one session runs at a time."""
    found = []
    for proc in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(proc + "/cmdline", "rb").read()
            state = open(proc + "/stat").read().rsplit(") ", 1)[1].split()[0]
        except OSError:
            continue
        argv0 = cmd.split(b"\0")[0]
        if b"/chrome-linux/" in argv0:
            found.append((proc[6:], state, argv0[-40:].decode(errors="replace")))
    return found


def test_idle_end_wipes_profile():
    signer, sock = boot("idle", idle="0.05")
    status, snap = call(sock, "/session/open", {"url": A + "/landing", "allowed": [A]})
    expect(status == 200, f"open: {status} {snap}")
    found = profiles()
    expect(len(found) == 1, f"one profile while open: {found}")
    time.sleep(6)
    expect(profiles() == [], f"profile deleted after idle: {profiles()}")
    left = chrome_running()
    expect(not left, f"chromium killed after idle: {left}")
    status, body = call(sock, "/session/snapshot", {"allowed": [A]})
    expect((status, body) == (409, {"error": "no_session"}), f"after idle: {status} {body}")
    signer.shutdown()


def test_action_timeout_ends_session():
    signer, sock = boot("slow", action="3")
    status, snap = call(sock, "/session/open", {"url": A + "/hang", "allowed": [A]})
    expect(status == 200, f"open: {status} {snap}")
    found = profiles()
    hang = ref_of(snap, "button", "Hang")
    started = time.monotonic()
    status, body = call(sock, "/session/click", {"ref": hang, "allowed": [A]})
    expect((status, body) == (504, {"error": "timeout"}), f"hung click: {status} {body}")
    expect(time.monotonic() - started < 10, "bounded by the action timeout")
    expect(profiles() == [] and not chrome_running(found[0]), "session ended on timeout")
    expect(call(sock, "/health", method="GET")[1] == {"status": "ready"}, "ready after timeout")
    signer.shutdown()


def test_sandbox_unavailable():
    def no_sandbox(sandbox=True):
        raise browser.StartError()
    signer, sock = boot("nosandbox", start_browser=no_sandbox)
    expect(call(sock, "/health", method="GET") == (200, {"status": "sandbox_unavailable"}), "health")
    status, body = call(sock, "/session/open", {"url": A + "/landing", "allowed": [A]})
    expect((status, body) == (503, {"error": "sandbox_unavailable"}), f"open refused: {status} {body}")
    signer.shutdown()


def test_start_never_disables_sandbox():
    try:
        browser.Chromium.start(sandbox=False)
    except ValueError:
        pass
    else:
        fail("start(sandbox=False) must refuse")


def test_config_rejects_invalid():
    cfg = server.load_config({})
    expect((str(cfg.socket_path), cfg.idle_minutes, cfg.action_seconds)
           == ("/run/bitwarden-signin/signin.sock", 5.0, 30.0), f"defaults {cfg}")
    for key in ("BITWARDEN_SIGNIN_IDLE_MINUTES", "BITWARDEN_SIGNIN_ACTION_SECONDS"):
        for bad in ("0", "-1", "nan", "inf", "abc", ""):
            try:
                server.load_config({key: bad})
            except server.ConfigError:
                continue
            fail(f"{key}={bad!r} accepted")


for test in (test_config_rejects_invalid, test_start_never_disables_sandbox, test_health_and_socket_mode,
             test_end_without_session, test_bad_requests, test_open_snapshot_has_no_values,
             test_fill_password_then_snapshot_scrubbed, test_fill_refuses_text_input,
             test_type_refuses_password_input, test_fill_totp_targets, test_origin_mismatch_after_redirect,
             test_stale_ref, test_export_cookie_and_storage, test_export_drops_written_values,
             test_snapshot_origin_recheck, test_crashpad_contained, test_busy_second_open, test_idle_end_wipes_profile,
             test_action_timeout_ends_session, test_sandbox_unavailable):
    test()
    print(f"pass {test.__name__}", flush=True)

expect(profiles() == [], f"profiles left: {profiles()}")
expect(not chrome_running(), "no Chromium process left")
expect(not os.path.exists("/tmp/.config/chromium") and not glob.glob("/tmp/org.chromium.*"),
       f"Chromium wrote outside its profiles: {os.listdir('/tmp')}")
MAIN.shutdown()
os.write(1, b"image ok\n")
'''


def main() -> None:
    for fn in (test_scrub_matches_digest_forms, test_scrub_replaces_each_form, test_reduce_drops_values,
               test_reduce_skips_field_descendants, test_reduce_placeholder_name_kept,
               test_reduce_real_chromium_tree, test_reduce_refs_stable_order,
               test_select_parent_domain_and_excludes_sibling_and_idp):
        fn()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER, mounts=[(str(SIGNER_SRC), "/signin-src")],
                           docker_args=IMAGE_DOCKER_ARGS)


if __name__ == "__main__":
    main()
