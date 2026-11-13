#!/usr/bin/env python3
"""Broker-side sign-in end to end, with canary values the agent side must never hold.

    python3 scripts/test-bitwarden-signin-e2e.py

On throwaway networks it starts the whole chain: a Vaultwarden behind the
stack's TLS sidecar (scripts/lib/vw_testkit.py) holding one account and one
login item whose password and TOTP seed are canaries generated for this run;
the signer image built from the pinned Hermes image, run with the compose
service's hardening; the broker in the agent container's network namespace, as
``network_mode: service:hermes`` puts it; a Hermes-image container running the
agent's own Chromium with the sign-in overlays applied; and a local login site
on two origins.

The site serves five flows on one origin: a one-page form, a two-step form, a
TOTP step, a ``localStorage`` token, and a redirect through an IdP origin that
only the item's ``agent-signin-idp`` field allows. Each is driven through the
Hermes overlay entry points (``browser_vault`` action=sign_in and the six
session actions), with no model. The site checks the password, the username and
the TOTP code it receives, and each flow must end with the agent's own tab on a
signed-in page.

Then every encoding of each canary (broker/digest_redaction.py FORMS, of the
value and of its CR/LF-stripped form) is looked for in:

  - the agent Chromium's DOM, cookies and both storages, through CDP;
  - the agent Chromium's profile directory on disk, after it exits;
  - the memory of the gateway stand-in process, through /proc, read by a second
    process running as the ``hermes`` user; that process never learns the
    canaries from the stand-in, which does not hold them either;
  - ``docker logs`` of every container in the run.

A control value the site puts in every page and prints once at startup must be found on each of
those surfaces, so a run with no canary hit cannot pass because the search reached nothing.

Expected: each flow signs in and there is no hit anywhere. Containers, networks,
volumes and built images are removed by name on success and on failure. Exit 77
without Docker or the pinned Hermes image.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))

import compose_config  # noqa: E402
import hermes_image  # noqa: E402
import vw_testkit  # noqa: E402
from broker.digest_redaction import FORMS, _encode  # noqa: E402
from testkit import expect, fail  # noqa: E402
from vw_testkit import USER, docker  # noqa: E402

BROKER_SRC = ROOT / "compose" / "bitwarden-broker"
HERMES_SRC = ROOT / "compose" / "hermes"
SIGNIN_SERVICE = "bitwarden-signin"
PYTHON = "/opt/hermes/.venv/bin/python"
SITE_HOST = "site.signin.internal"
IDP_HOST = "idp.signin.internal"
SITE_PORT = 8080
SITE_ORIGIN = f"http://{SITE_HOST}:{SITE_PORT}"
IDP_ORIGIN = f"http://{IDP_HOST}:{SITE_PORT}"
AGENT_EMAIL = "agent@vault.localhost"
LOGIN_USER = "canary@example.com"
FLOWS = ("one_page", "two_step", "totp", "local_token", "idp")
TASK = "signin-e2e"
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# The overlays 05-patch-tool-calling.sh runs on tools/browser_vault_tool.py, in its order.
PATCHES = ("patch-browser-exec-hint.py", "patch-vault-create-login.py", "patch-vault-capture-secret.py",
           "patch-digest-redaction.py", "patch-bitwarden-broker-fill.py", "patch-bitwarden-signin.py")
DRIVER_SECONDS = 900


def skip(reason: str) -> None:
    print(f"SKIP {reason}")
    raise SystemExit(77)


def canary_patterns(values: dict[str, str]) -> list[dict]:
    """Every scrub encoding of each canary, as the searcher takes them."""
    out, seen = [], set()
    for label, value in values.items():
        for source in (value, value.replace("\r", "").replace("\n", "")):
            for form in FORMS:
                data = _encode(source, form)
                if data in seen:
                    continue
                seen.add(data)
                out.append({"label": label, "form": form, "b64": base64.b64encode(data).decode("ascii")})
    return out


def scan_text(text: str, patterns: list[dict], where: str) -> list[str]:
    data = text.encode("utf-8", "surrogatepass")
    return [f"{where}: {p['label']} ({p['form']})" for p in patterns
            if base64.b64decode(p["b64"]) in data]


# ── the login site ────────────────────────────────────────────────────────────────────────────

SITE_PY = r'''#!/usr/bin/env python3
"""The five sign-in flows, on the item origin and on the IdP origin.

Reads the expected username, password and TOTP seed; /seen reports only whether
each matched, so the agent side can check a flow without holding a value.
"""
import base64, hashlib, hmac, html, json, secrets, struct, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CONF = json.load(open("/test/config.json"))
SECRET = json.load(open("/secret/secret.json"))
SITE, IDP = CONF["site_origin"], CONF["idp_origin"]
USER, PASSWORD, SEED = SECRET["login_user"], SECRET["password"], SECRET["totp_seed"]
IDP_HOST = urlsplit(IDP).hostname
BLANK = {"username_ok": False, "password_ok": False, "totp_ok": False, "idp_visited": False}
STATE = {"flow": "one_page", "sessions": set(), "tokens": set(), "half": set(), "codes": set(),
         "seen": dict(BLANK)}


def totp_code(step):
    key = base64.b32decode(SEED.upper() + "=" * (-len(SEED) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(int.from_bytes(mac[off:off + 4], 'big') & 0x7FFFFFFF) % 1000000:06d}"


def totp_ok(value):
    now = int(time.time()) // 30
    return any(value == totp_code(now + d) for d in (-1, 0, 1))


def page(body, script=""):
    tail = f"<script>{script}</script>" if script else ""
    # The control value: a string the search must find on every surface it covers. Without it a
    # run with no canary hit could mean the search reached nothing.
    return ("<!doctype html><html><head><meta charset=utf-8><title>e2e</title>"
            f'<meta name="e2e-control" content="{CONF["control"]}"></head>'
            f"<body>{body}{tail}</body></html>")


def field(label, name, kind="text", extra=""):
    return (f'<p><label for="{name}">{label}</label>'
            f'<input id="{name}" name="{name}" type="{kind}" {extra}></p>')


def form(inner, button, action="/login"):
    return f'<form method="post" action="{action}">{inner}<button type="submit">{button}</button></form>'


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def send(self, body, status=200, headers=()):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, headers=()):
        self.send("", 303, (("Location", location), *headers))

    def cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                out[name] = value
        return out

    def body(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urlsplit(self.path).query).items()}

    def on_idp(self):
        return (self.headers.get("Host") or "").split(":")[0] == IDP_HOST

    def log_message(self, *a):
        pass  # URLs carry session tokens; nothing from a request is logged

    # --- routes -----------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.on_idp():
            return self.idp_get(path)
        if path == "/control":
            STATE.update(flow=self.query().get("flow", "one_page"), sessions=set(), tokens=set(),
                         half=set(), seen=dict(BLANK))
            return self.send("ok")
        if path == "/seen":
            return self.send(json.dumps(STATE["seen"]))
        if path == "/login":
            return self.send(self.login_page())
        if path == "/check":
            token = self.query().get("token", "")
            return self.send(f"SIGNED-IN {html.escape(USER)}" if token and token in STATE["tokens"]
                             else "NOT-SIGNED-IN")
        if path == "/callback":
            code = self.query().get("code", "")
            if code and code in STATE["half"]:
                STATE["half"].discard(code)
                sid = secrets.token_hex(16)
                STATE["sessions"].add(sid)
                return self.redirect("/home", (("Set-Cookie", f"sid={sid}; Path=/"),))
            return self.send(page("<p>BAD-CODE</p>"), 400)
        if path == "/home":
            return self.send(self.home_page())
        return self.send(page("<p>other</p>"), 404)

    def do_POST(self):
        if self.on_idp():
            return self.idp_post()
        if urlsplit(self.path).path != "/login":
            return self.send(page("<p>other</p>"), 404)
        body, flow = self.body(), STATE["flow"]
        if "username" in body:
            STATE["seen"]["username_ok"] = body.get("username") == USER
        if "password" in body:
            STATE["seen"]["password_ok"] = body.get("password") == PASSWORD
        if "otp" in body:
            STATE["seen"]["totp_ok"] = totp_ok(body.get("otp", ""))
        if flow == "two_step" and "password" not in body:
            return self.redirect("/login?step=2") if STATE["seen"]["username_ok"] else self.deny()
        if not STATE["seen"]["password_ok"] or not STATE["seen"]["username_ok"]:
            if flow != "two_step" or "password" in body:
                return self.deny()
        if flow == "totp" and "otp" not in body:
            return self.redirect("/login?step=code")
        if flow == "totp" and not STATE["seen"]["totp_ok"]:
            return self.deny()
        if flow == "local_token":
            token = secrets.token_hex(16)
            STATE["tokens"].add(token)
            # The site also keeps the password in a cookie. The signer's export must drop it, so it
            # never reaches the agent's tab.
            return self.send(
                page("<p>TOKEN-ISSUED</p>", f"localStorage.setItem('e2e-token', {json.dumps(token)});"),
                headers=(("Set-Cookie", f"remember={urllib.parse.quote(PASSWORD)}; Path=/"),))
        if flow == "idp":
            code = secrets.token_hex(16)
            STATE["half"].add(code)
            return self.redirect(f"{IDP}/authorize?next={urllib.parse.quote(SITE + '/callback')}&code={code}")
        sid = secrets.token_hex(16)
        STATE["sessions"].add(sid)
        return self.redirect("/home", (("Set-Cookie", f"sid={sid}; Path=/"),))

    def deny(self):
        return self.send(page("<p>SIGN-IN-REFUSED</p>"), 401)

    def login_page(self):
        flow, step = STATE["flow"], self.query().get("step", "")
        user = field("Email", "username")
        password = field("Password", "password", "password")
        if flow == "two_step":
            if step == "2":
                return page(f"<p>Signing in as {html.escape(USER)}</p>" + form(password, "Sign in"))
            return page(form(user, "Next"))
        if flow == "totp" and step == "code":
            code = field("Verification code", "otp", "text", 'autocomplete="one-time-code"')
            return page(form(code, "Verify"))
        return page(form(user + password, "Sign in"))

    def home_page(self):
        if STATE["flow"] == "local_token":
            return page("<p>CHECKING</p>", (
                "fetch('/check?token=' + encodeURIComponent(localStorage.getItem('e2e-token') || ''))"
                ".then(r => r.text()).then(t => { document.body.textContent = t; })"
                ".catch(() => { document.body.textContent = 'NOT-SIGNED-IN'; });"))
        signed = self.cookies().get("sid", "") in STATE["sessions"]
        return page(f"<p>SIGNED-IN {html.escape(USER)}</p>" if signed else "<p>NOT-SIGNED-IN</p>")

    # --- the identity provider, on its own origin -------------------------

    def idp_get(self, path):
        if path != "/authorize":
            return self.send(page("<p>other</p>"), 404)
        STATE["seen"]["idp_visited"] = True
        query = self.query()
        hidden = (f'<input type="hidden" name="next" value="{html.escape(query.get("next", ""))}">'
                  f'<input type="hidden" name="code" value="{html.escape(query.get("code", ""))}">')
        return self.send(page("<p>Identity provider</p>" + form(hidden, "Continue", "/authorize")))

    def idp_post(self):
        body = self.body()
        target = body.get("next") or SITE + "/callback"
        return self.redirect(f"{target}?code={urllib.parse.quote(body.get('code', ''))}")


print(f"control {CONF['control']}", file=sys.stderr, flush=True)
server = ThreadingHTTPServer(("0.0.0.0", CONF["site_port"]), Site)
server.daemon_threads = True
server.serve_forever()
'''


# ── Vaultwarden provisioning, in the broker image ─────────────────────────────────────────────

PROVISION_PY = r'''#!/usr/bin/env python3
"""Register the agent account, write its instance file and secrets, create the canary item."""
import base64, json, os, sys, time

sys.path[:0] = ["/app", "/app/setup"]
import vw_register as vw
from broker.bwcli import classify, run_bw

CONF = json.load(open("/test/config.json"))
SECRET = json.load(open("/secret/secret.json"))
BASE, SLUG, EMAIL = CONF["vault_base"], CONF["slug"], CONF["agent_email"]
MASTER = SECRET["master_password"]

deadline = time.time() + 180
while True:
    try:
        vw.opener().open(f"{BASE}/alive", timeout=5).close()
        break
    except Exception as exc:
        if time.time() > deadline:
            sys.exit(f"Vaultwarden did not answer /alive: {type(exc).__name__}")
        time.sleep(1)

admin_token = open("/test-admin/admin_token", encoding="utf-8").read().strip()
vw.admin_invite(BASE, admin_token, EMAIL)
vw.register(BASE, EMAIL, MASTER)
token = vw.token(BASE, EMAIL, MASTER)
client_id, client_secret = vw.api_key(BASE, token, EMAIL, MASTER)

secrets_dir = f"/secrets/{SLUG}"
os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
for name, value in (("client_id", client_id), ("client_secret", client_secret),
                    ("master_password", MASTER)):
    path = os.path.join(secrets_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.chmod(path, 0o600)
with open(f"/store/instances/{SLUG}.json", "w", encoding="utf-8") as fh:
    json.dump({"mode": "selfhosted", "url": BASE, "scope": "all", "unlock": "stored"}, fh)
os.chmod(f"/store/instances/{SLUG}.json", 0o600)


def bw(args, env=None, stdin=None):
    code, out, err = run_bw("bw", "/tmp/bw-provision", args, dict(env or {}), stdin)
    if code != 0:
        sys.exit(f"bw {args[0]}: {classify(err)[0]}")
    return out


bw(["config", "server", BASE])
bw(["login", "--apikey"], {"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret})
session = bw(["unlock", "--passwordenv", "BW_PW", "--raw"], {"BW_PW": MASTER}).strip()
item = {
    "type": 1, "name": CONF["item_label"], "notes": None, "favorite": False, "folderId": None,
    "organizationId": None, "collectionIds": [], "reprompt": 0, "secureNote": None, "card": None,
    "identity": None,
    # No agent-in-page-fill field, so the broker lists the item as sign-in only.
    "fields": [{"name": "agent-signin-idp", "value": CONF["idp_origin"], "type": 0}],
    "login": {"username": CONF["login_user"], "password": SECRET["password"],
              "totp": SECRET["totp_seed"],
              "uris": [{"uri": CONF["site_origin"] + "/login", "match": None}]},
}
payload = base64.b64encode(json.dumps(item).encode("utf-8")).decode("ascii")
created = json.loads(bw(["create", "item"], {"BW_SESSION": session}, stdin=payload))
item = payload = None
print("provisioned", created["id"])
'''


# ── a small CDP client, shared by the agent driver and the searcher ───────────────────────────

CDP_CLIENT = r'''
import json as _json, urllib.request as _urllib
from websockets.sync.client import connect as _connect


def cdp_http(port, path, method="GET"):
    request = _urllib.Request(f"http://127.0.0.1:{port}{path}", method=method)
    opener = _urllib.build_opener(_urllib.ProxyHandler({}))
    with opener.open(request, timeout=10) as resp:
        return _json.loads(resp.read())


class Cdp:
    """One browser websocket; commands run on an attached page session."""

    def __init__(self, ws_url):
        self.ws = _connect(ws_url, proxy=None, compression=None, max_size=64 * 1024 * 1024,
                           open_timeout=30, ping_interval=None)
        self.next_id = 0
        self.sessions = {}

    def call(self, method, params=None, session=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(_json.dumps(msg))
        while True:
            reply = _json.loads(self.ws.recv(timeout=30))
            if reply.get("id") != self.next_id:
                continue
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result") or {}

    def session(self, target_id):
        if target_id not in self.sessions:
            self.sessions[target_id] = self.call(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        return self.sessions[target_id]

    def evaluate(self, target_id, expression):
        reply = self.call("Runtime.evaluate",
                          {"expression": expression, "returnByValue": True, "awaitPromise": True},
                          session=self.session(target_id))
        if reply.get("exceptionDetails"):
            return None
        return (reply.get("result") or {}).get("value")

    def navigate(self, target_id, url):
        self.call("Page.navigate", {"url": url}, session=self.session(target_id))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
'''


# ── the gateway stand-in: the Hermes overlays driving the sign-in ─────────────────────────────

AGENT_DRIVER = r'''#!/usr/bin/env python3
"""Drive every flow through the browser_vault sign-in actions. Holds no canary.

Writes /tmp/e2e-status.json when the flows are done and then waits for
/tmp/e2e-go, so the searcher can read this process's memory while it is alive.
"""
import glob, json, os, subprocess, sys, tempfile, time, urllib.request

CONF = json.load(open("/test/config.json"))
SITE, IDP, TASK = CONF["site_origin"], CONF["idp_origin"], CONF["task_id"]
STATUS, GO, CAPTURE = "/tmp/e2e-status.json", "/tmp/e2e-go", "/tmp/e2e-capture.jsonl"
notes = []


def say(line):
    notes.append(line)
    print(line, flush=True)


def http_get(url, timeout=15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# --- the agent's own Chromium ------------------------------------------------
# --no-sandbox because this container has neither the signer's seccomp profile nor
# user namespaces; this stands for the agent's browser, not the signer's.
PROFILE = tempfile.mkdtemp(prefix="agent-profile-", dir="/tmp")
binaries = sorted(glob.glob("/opt/hermes/tools/chromium-*/chrome-linux*/chrome"))
if not binaries:
    sys.exit("no bundled Chromium under /opt/hermes/tools/chromium-*/")
chrome = subprocess.Popen(
    [binaries[0], "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
     "--remote-debugging-port=0", f"--user-data-dir={PROFILE}", f"{SITE}/home"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
active = os.path.join(PROFILE, "DevToolsActivePort")
for _ in range(300):
    if os.path.exists(active) and len(open(active).read().split()) >= 2:
        break
    time.sleep(0.1)
else:
    sys.exit("the agent Chromium did not open a DevTools port")
port_text, ws_path = open(active).read().split()[:2]
PORT = int(port_text)
WS_URL = f"ws://127.0.0.1:{PORT}{ws_path}"

__CDP__

cdp = Cdp(WS_URL)
TAB = None
for _ in range(300):
    pages = [t for t in cdp_http(PORT, "/json/list") if t.get("type") == "page"]
    if pages:
        TAB = pages[0]["id"]
        break
    time.sleep(0.1)
if TAB is None:
    sys.exit("the agent Chromium opened no page target")

# --- Hermes wiring -----------------------------------------------------------
sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, "/bootstrap")
import bitwarden_broker_backend as bwb
import agent.vault_backends as vb
import agent.vault_backends.base as vbase
import tools.browser_session as bsession
import tools.browser_vault_tool as bvt
from agent.vault_backends.plugin import PluginLoginBackend
from tools.browser_supervisor import SUPERVISOR_REGISTRY

BW = PluginLoginBackend("bwbroker", bwb.BrokerLoginBackend(in_cron=lambda: False))
BACKENDS = [BW]
vb.enabled_backends = vbase.enabled_backends = lambda: list(BACKENDS)
vb.backend_for_handle = vbase.backend_for_handle = lambda h: next((b for b in BACKENDS if b.owns(h)), None)
bsession.session_for_task = lambda task_id: {"cdp_url": WS_URL}
assert bwb.in_cron_turn() is False, "this turn must not read as a cron turn"


def tool(args):
    return json.loads(bvt._handle_vault(args, task_id=TASK))


def wait_for_text(want, seconds=45):
    end = time.time() + seconds
    last = ""
    while time.time() < end:
        last = cdp.evaluate(TAB, "document.body ? document.body.innerText : ''") or ""
        if want in last:
            return last
        time.sleep(0.25)
    raise AssertionError(f"the agent tab never showed {want!r} (it showed {last[:120]!r})")


# --- the item ----------------------------------------------------------------
listing = json.loads(bvt.browser_vault_list())
items = [i for i in listing.get("items") or [] if str(i.get("handle", "")).startswith("bwbroker:")]
assert len(items) == 1, f"the broker lists {len(items)} items: {listing}"
HANDLE = items[0]["handle"]
assert items[0].get("sign_in_only") is True, items[0]
say("ok: the broker lists one sign-in-only login")

# fill_login must refuse it, so sign_in is the only way in.
refused = json.loads(bvt.browser_vault_fill(HANDLE, task_id=TASK))
assert refused.get("error_type") == "sign_in_only", refused
say("ok: browser_vault action=fill refuses the item with sign_in_only")


def run_flow(name):
    http_get(f"{SITE}/control?flow={name}")
    cdp.navigate(TAB, f"{SITE}/home")
    wait_for_text("NOT-SIGNED-IN")
    result = tool({"action": "sign_in", "handle": HANDLE})
    if name == "idp":
        assert result.get("success") is True, result
        assert result.get("origin") == IDP, f"the sequence stopped on {result.get('origin')!r}: {result}"
        nodes = result.get("nodes") or []
        button = next((n["ref"] for n in nodes
                       if n.get("role", "").lower() in ("button", "pushbutton")
                       and "continue" in (n.get("name", "") + n.get("text", "")).lower()), None)
        assert button is not None, f"no Continue button in the IdP snapshot: {nodes}"
        denied = tool({"action": "signin_fill", "ref": button, "field": "password"})
        assert denied.get("error_type") == "origin_mismatch", f"a fill on the IdP origin: {denied}"
        result = tool({"action": "signin_click", "ref": button})
        assert result.get("success") is True and result.get("origin") == SITE, result
        result = tool({"action": "signin_finish"})
    assert result.get("success") is True and result.get("done") is True, result
    assert result.get("origin") == SITE, result
    assert isinstance(result.get("cookies"), int), result
    body = wait_for_text("SIGNED-IN")
    assert "NOT-SIGNED-IN" not in body, body
    seen = json.loads(http_get(f"{SITE}/seen"))
    assert seen["username_ok"] and seen["password_ok"], f"{name}: the site did not get the login: {seen}"
    assert seen["totp_ok"] == (name == "totp"), f"{name}: totp_ok {seen['totp_ok']}"
    assert seen["idp_visited"] == (name == "idp"), f"{name}: idp_visited {seen['idp_visited']}"
    jar = cdp.evaluate(TAB, "document.cookie") or ""
    stored = cdp.evaluate(TAB, "JSON.stringify(localStorage)") or "{}"
    if name == "local_token":
        assert "remember" not in jar, "the password-bearing cookie reached the agent's tab"
        assert "e2e-token" in stored, f"the handover carried no localStorage: {stored}"
    # What the tab held at the end of this flow, for the searcher: the tab is reused, so the
    # live search would otherwise only see the last flow.
    with open(CAPTURE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "flow": name, "url": cdp.evaluate(TAB, "location.href"), "text": body, "cookies": jar,
            "localStorage": stored,
            "sessionStorage": cdp.evaluate(TAB, "JSON.stringify(sessionStorage)"),
            "dom": cdp.evaluate(TAB, "document.documentElement.outerHTML"),
        }) + "\n")
    say(f"ok: {name} signed in ({result.get('cookies')} cookies handed over)")


failure = None
try:
    for flow in CONF["flows"]:
        run_flow(flow)
except BaseException as exc:  # reported through the status file; the orchestrator prints it
    failure = f"{type(exc).__name__}: {exc}"
    say(f"FAIL: {failure}")

with open(STATUS, "w", encoding="utf-8") as fh:
    json.dump({"ok": failure is None, "failure": failure, "pid": os.getpid(), "port": PORT,
               "profile": PROFILE, "tab": TAB, "capture": CAPTURE, "notes": notes}, fh)

# Stay alive, with Chromium up, until the searcher has read this process and the pages.
end = time.time() + 600
while not os.path.exists(GO) and time.time() < end:
    time.sleep(0.5)
SUPERVISOR_REGISTRY.stop_all()
cdp.close()
chrome.terminate()
try:
    chrome.wait(timeout=30)
except Exception:
    chrome.kill()
print("driver done", flush=True)
sys.exit(0 if failure is None else 1)
'''


# ── the searcher: the same container, the hermes user, the canaries on stdin ───────────────────

SEARCHER_PY = r'''#!/usr/bin/env python3
"""Look for every canary encoding in the agent browser and in the stand-in's memory.

The patterns arrive on stdin, so no canary is ever written into this container.
"""
import base64, json, os, sys

MODE = sys.argv[1]
SPEC = json.load(sys.stdin)
PATTERNS = [(p["label"], p["form"], base64.b64decode(p["b64"])) for p in SPEC["patterns"]]
# The control value, which the site puts in every page: a surface the search covers must show it,
# or a run with no canary hit says nothing about that surface.
CONTROLS = [base64.b64decode(p["b64"]) for p in SPEC.get("control") or []]
LONGEST = max(len(p[2]) for p in PATTERNS + [("", "", c) for c in CONTROLS])
CHUNK = 4 << 20
hits, stats, controls = [], {}, set()

__CDP__


def scan(surface, where, data):
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    if not isinstance(data, (bytes, bytearray)):
        return
    for label, form, pattern in PATTERNS:
        if pattern in data:
            hits.append(f"{where}: {label} ({form})")
    if any(control in data for control in CONTROLS):
        controls.add(surface)


def scan_browser(port):
    targets = [t for t in cdp_http(port, "/json/list") if t.get("type") == "page"]
    scan("browser", "chromium /json/list", json.dumps(targets))
    version = cdp_http(port, "/json/version")
    cdp = Cdp(version["webSocketDebuggerUrl"])
    reads = 0
    for target in targets:
        tid = target["id"]
        for name, expression in (
                ("dom", "document.documentElement.outerHTML"),
                ("text", "document.body ? document.body.innerText : ''"),
                ("cookies", "document.cookie"),
                ("localStorage", "JSON.stringify(localStorage)"),
                ("sessionStorage", "JSON.stringify(sessionStorage)")):
            value = cdp.evaluate(tid, expression)
            if isinstance(value, str):
                reads += 1
                scan("browser", f"chromium {name} ({target.get('url', '')[:60]})", value)
    cdp.close()
    stats["browser_targets"] = len(targets)
    stats["browser_reads"] = reads
    if not targets or reads < 5:
        hits.append("chromium: nothing could be read from the agent browser")


def scan_memory(pid):
    regions = anon = unreadable = 0
    total = 0
    try:
        maps = open(f"/proc/{pid}/maps", encoding="utf-8").read().splitlines()
    except OSError as exc:
        hits.append(f"/proc/{pid}/maps: cannot be read ({type(exc).__name__})")
        return
    for name in ("cmdline", "environ"):
        try:
            scan("memory", f"/proc/{pid}/{name}", open(f"/proc/{pid}/{name}", "rb").read())
        except OSError:
            pass
    try:
        mem = open(f"/proc/{pid}/mem", "rb", 0)
    except OSError as exc:
        hits.append(f"/proc/{pid}/mem: cannot be read as this user ({type(exc).__name__})")
        return
    with mem:
        for line in maps:
            parts = line.split()
            if len(parts) < 5 or "r" not in parts[1]:
                continue
            path = parts[5] if len(parts) > 5 else ""
            if path.startswith("/dev/") or path in ("[vvar]", "[vsyscall]", "[vvar_vclock]"):
                continue
            low, high = (int(x, 16) for x in parts[0].split("-"))
            regions += 1
            writable_anon = "w" in parts[1] and not path.startswith("/")
            try:
                mem.seek(low)
                left, tail = high - low, b""
                while left > 0:
                    data = mem.read(min(CHUNK, left))
                    if not data:
                        break
                    left -= len(data)
                    total += len(data)
                    scan("memory", f"/proc/{pid}/mem {path or 'anon'}", tail + data)
                    tail = data[-(LONGEST - 1):] if LONGEST > 1 else b""
                if writable_anon:
                    anon += 1
            except OSError:
                unreadable += 1
    stats.update(regions=regions, anon_regions_read=anon, unreadable_regions=unreadable,
                 bytes_scanned=total)
    if anon == 0:
        hits.append(f"/proc/{pid}/mem: no writable anonymous region could be read; the check is empty")


def scan_profile(path):
    files = size = 0
    for folder, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(folder, name)
            scan("profile", f"profile name {full}", name)
            try:
                with open(full, "rb") as fh:
                    files += 1
                    tail = b""
                    while True:
                        data = fh.read(CHUNK)
                        if not data:
                            break
                        size += len(data)
                        scan("profile", f"profile {full}", tail + data)
                        tail = data[-(LONGEST - 1):] if LONGEST > 1 else b""
            except OSError:
                pass
    stats.update(profile_files=files, profile_bytes=size)
    if files == 0:
        hits.append(f"profile {path}: no file could be read; the check is empty")


def scan_capture(path):
    """What each flow's tab held at its end, written by the stand-in as it went."""
    lines = 0
    try:
        with open(path, "rb") as fh:
            for line in fh:
                lines += 1
                scan("capture", f"tab after flow {lines}", line)
    except OSError as exc:
        hits.append(f"{path}: cannot be read ({type(exc).__name__})")
    stats["flows_captured"] = lines
    if lines != SPEC["flows"]:
        hits.append(f"{path}: {lines} flows captured, want {SPEC['flows']}")


if MODE == "live":
    scan_capture(SPEC["capture"])
    scan_browser(int(SPEC["port"]))
    scan_memory(int(SPEC["pid"]))
elif MODE == "profile":
    scan_profile(SPEC["profile"])
else:
    sys.exit(f"unknown mode {MODE}")
print(json.dumps({"hits": hits, "stats": stats, "control_surfaces": sorted(controls)}))
'''


# ── host side ─────────────────────────────────────────────────────────────────────────────────

def write_scripts(folder: Path, secret_folder: Path, config: dict, canaries: dict) -> None:
    folder.mkdir(mode=0o755)
    secret_folder.mkdir(mode=0o700)
    os.chmod(secret_folder, 0o700)
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (folder / "site.py").write_text(SITE_PY, encoding="utf-8")
    (folder / "provision.py").write_text(PROVISION_PY, encoding="utf-8")
    (folder / "agent_driver.py").write_text(AGENT_DRIVER.replace("__CDP__", CDP_CLIENT), encoding="utf-8")
    (folder / "searcher.py").write_text(SEARCHER_PY.replace("__CDP__", CDP_CLIENT), encoding="utf-8")
    for name in ("config.json", "site.py", "provision.py", "agent_driver.py", "searcher.py"):
        os.chmod(folder / name, 0o644)
    secret_file = secret_folder / "secret.json"
    secret_file.write_text(json.dumps({"login_user": LOGIN_USER, **canaries}), encoding="utf-8")
    os.chmod(secret_file, 0o600)


def signer_flags(service: dict) -> list[str]:
    """docker run flags for the bitwarden-signin service's hardening, from the rendered config."""
    flags = ["--user", USER]
    if service.get("read_only"):
        flags.append("--read-only")
    if service.get("init"):
        flags.append("--init")
    for cap in service.get("cap_drop") or []:
        flags += ["--cap-drop", cap]
    for opt in service.get("security_opt") or []:
        if opt.startswith("seccomp="):
            opt = "seccomp=" + str((ROOT / opt.split("=", 1)[1]).resolve())
        flags += ["--security-opt", opt]
    for mount in service.get("tmpfs") or []:
        flags += ["--tmpfs", mount]
    if service.get("mem_limit"):
        flags += ["--memory", str(service["mem_limit"])]
    for key, value in (service.get("environment") or {}).items():
        flags += ["-e", f"{key}={value}"]
    return flags


def wait_for(what: str, probe, seconds: float, detail=lambda: "") -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if probe():
            return
        time.sleep(1)
    fail(f"{what} did not happen within {seconds:g} s{detail()}")


def main() -> None:
    if not vw_testkit.available():
        skip("bitwarden-signin-e2e: Docker is not available")
    if hermes_image.image_gate() is None:
        skip(f"bitwarden-signin-e2e: nousresearch/hermes-agent:{hermes_image.image_tag()} is not present")

    cfg = compose_config.render("core,bitwarden")
    service = (cfg.get("services") or {}).get(SIGNIN_SERVICE)
    expect(service is not None, f"{SIGNIN_SERVICE} missing from core,bitwarden")
    volume_opts = ((cfg.get("volumes") or {}).get("bitwarden_signin") or {}).get("driver_opts") or {}
    expect(bool(volume_opts), "the bitwarden_signin volume has no driver options")

    canaries = {
        "password": "CanaryPw-" + secrets.token_hex(16),
        "totp_seed": "CANARY" + "".join(secrets.choice(B32) for _ in range(26)),
        "master_password": "CanaryMaster-" + secrets.token_hex(12),
    }
    patterns = canary_patterns(canaries)
    control = "E2eControl-" + secrets.token_hex(16)
    control_patterns = canary_patterns({"control": control})
    config = {
        "control": control,
        "vault_base": vw_testkit.BASE, "slug": "e2e", "agent_email": AGENT_EMAIL,
        "login_user": LOGIN_USER, "item_label": "Canary site", "site_origin": SITE_ORIGIN,
        "idp_origin": IDP_ORIGIN, "site_port": SITE_PORT, "flows": list(FLOWS), "task_id": TASK,
    }

    kit = vw_testkit.Vaultwarden(prefix="bwe2e")
    site, agent, signer, broker, prov = (f"{kit.prefix}-{n}" for n in
                                         ("site", "agent", "signer", "broker", "prov"))
    containers = [prov, broker, signer, agent, site]
    kit.also_remove(*containers)
    volume = f"{kit.prefix}-signin-vol"
    signin_image = f"{kit.prefix}-signin:local"
    scripts, secret_folder = kit.tmp / "t", kit.tmp / "secret"
    store, data = kit.tmp / "store/instances", kit.tmp / "data"
    for folder in (store, data / "bw", data / "broker"):
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)
    write_scripts(scripts, secret_folder, config, canaries)

    uid, gid = USER.split(":")
    status: dict = {}
    try:
        kit.prepare()
        print("ok: the broker image, the TLS material and the admin token")
        build_arg = f"HERMES_AGENT_IMAGE_TAG={service['build']['args']['HERMES_AGENT_IMAGE_TAG']}"
        proc = docker("build", "-q", "-t", signin_image, "--build-arg", build_arg,
                      service["build"]["context"], timeout=900)
        expect(proc.returncode == 0, f"docker build {SIGNIN_SERVICE}: {proc.stderr.strip()[-400:]}")
        kit.start()
        print("ok: Vaultwarden behind the TLS sidecar")

        # One account, one login item: the password and the TOTP seed are the canaries.
        proc = docker("run", "--rm", "--name", prov, "--network", kit.net, "--user", USER,
                      "--read-only", "--tmpfs", "/tmp", "--entrypoint", "python3",
                      "-e", "HOME=/tmp", "-e", "NODE_EXTRA_CA_CERTS=/secrets/local/tls/ca.crt",
                      "-v", f"{BROKER_SRC}:/app:ro", "-v", f"{scripts}:/test:ro",
                      "-v", f"{secret_folder}:/secret:ro", "-v", f"{kit.local}:/test-admin:ro",
                      "-v", f"{kit.secrets_dir}:/secrets", "-v", f"{store}:/store/instances",
                      kit.image, "/test/provision.py", timeout=600)
        provision_output = proc.stdout + proc.stderr
        expect(proc.returncode == 0, f"provisioning failed: {provision_output.strip()[-600:]}")
        print("ok: the account, its instance file and the canary login item")

        docker("run", "-d", "--name", site, "--network", kit.net,
               "--network-alias", SITE_HOST, "--network-alias", IDP_HOST,
               "--user", USER, "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--entrypoint", PYTHON,
               "-v", f"{scripts}:/test:ro", "-v", f"{secret_folder}:/secret:ro",
               f"nousresearch/hermes-agent:{hermes_image.image_tag()}", "/test/site.py")

        # The agent container: root so the overlays can be applied, the driver runs as hermes.
        docker("run", "-d", "--name", agent, "--network", kit.net, "--user", "0:0",
               "--entrypoint", PYTHON,
               "-v", f"{HERMES_SRC}:/bootstrap:ro", "-v", f"{BROKER_SRC}:/broker-src:ro",
               "-v", f"{scripts}:/test:ro",
               f"nousresearch/hermes-agent:{hermes_image.image_tag()}",
               "-c", "import time; time.sleep(10800)")
        expect(docker("inspect", "-f", "{{.State.Running}}", agent).stdout.strip() == "true",
               f"the agent container did not start: {docker('logs', agent).stderr.strip()[-400:]}")

        opts = dict(volume_opts)
        opts["o"] = f"uid={uid},gid={gid},mode=0700,size=1m"
        docker("volume", "create", *[a for k, v in opts.items() for a in ("--opt", f"{k}={v}")], volume)
        docker("run", "-d", "--name", signer, "--network", kit.net, *signer_flags(service),
               "-v", f"{volume}:/run/bitwarden-signin", signin_image)

        docker("run", "-d", "--name", broker, "--network", f"container:{agent}", "--user", USER,
               "--read-only", "--tmpfs", "/tmp",
               "--tmpfs", f"/run/bitwarden-sync:mode=0700,uid={uid},gid={gid}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "-e", "BITWARDEN_INSTANCES_DIR=/store/instances", "-e", "BITWARDEN_DATA_DIR=/data",
               "-e", "BITWARDEN_SECRETS_DIR=/secrets", "-e", "BITWARDEN_LISTEN=127.0.0.1:8099",
               "-e", "BITWARDEN_SYNC_SOCKET=/run/bitwarden-sync/sync.sock",
               "-e", "BITWARDEN_SIGNIN_SOCKET=/run/bitwarden-signin/signin.sock",
               "-e", "BITWARDEN_SYNC_MINUTES=10", "-e", "BITWARDEN_IDLE_LOCK_MINUTES=0",
               "-e", "NODE_EXTRA_CA_CERTS=/secrets/local/tls/ca.crt", "-e", "HOME=/tmp",
               "-v", f"{store}:/store/instances:ro", "-v", f"{kit.secrets_dir}:/secrets:ro",
               "-v", f"{data / 'bw'}:/data/bw", "-v", f"{data / 'broker'}:/data/broker",
               "-v", f"{volume}:/run/bitwarden-signin:ro", kit.image)

        def broker_health() -> dict:
            out = docker("exec", agent, PYTHON, "-c",
                         "import json,urllib.request;"
                         "print(urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=5).read()"
                         ".decode())")
            try:
                return json.loads(out.stdout)
            except ValueError:
                return {}

        wait_for("the broker and the signer became ready",
                 lambda: broker_health().get("signer") == "ready", 180,
                 lambda: f": last health {broker_health()}")
        print("ok: the broker answers and the signer reports ready")

        def items() -> list:
            out = docker("exec", agent, PYTHON, "-c",
                         "import urllib.request;"
                         "print(urllib.request.urlopen('http://127.0.0.1:8099/items', timeout=5).read()"
                         ".decode())")
            try:
                return json.loads(out.stdout)
            except ValueError:
                return []

        wait_for("the broker listed the canary item", lambda: len(items()) == 1, 180)
        listed = items()[0]
        expect(listed["origins"] == [SITE_ORIGIN] and listed["has_totp"] and listed["sign_in_only"],
               f"the broker lists the item as {listed}")
        print("ok: the broker lists the item, sign-in only, with a TOTP key")

        for name in PATCHES:
            out = docker("exec", "-u", "0", "-e", "DIGEST_REDACTION_BOOTSTRAP=/broker-src/broker",
                         agent, PYTHON, f"/bootstrap/{name}", timeout=300)
            expect(out.returncode == 0 and "warn:" not in out.stdout,
                   f"{name}: exit {out.returncode}: {(out.stdout + out.stderr).strip()[-400:]}")
        print(f"ok: the {len(PATCHES)} overlays applied in 05-patch-tool-calling.sh order")

        # The gateway stand-in, as the hermes user: it never holds a canary.
        docker("exec", "-d", "-u", "hermes", agent, "/bin/sh", "-c",
               f"{PYTHON} /test/agent_driver.py >/tmp/e2e-driver.log 2>&1; "
               "echo $? >/tmp/e2e-driver.rc")

        def driver_log() -> str:
            return docker("exec", agent, "/bin/sh", "-c", "cat /tmp/e2e-driver.log 2>/dev/null").stdout

        def driver_status() -> dict:
            out = docker("exec", agent, "/bin/sh", "-c", "cat /tmp/e2e-status.json 2>/dev/null")
            try:
                return json.loads(out.stdout)
            except ValueError:
                return {}

        def driver_done() -> bool:
            status.update(driver_status())
            if status:
                return True
            rc = docker("exec", agent, "/bin/sh", "-c", "cat /tmp/e2e-driver.rc 2>/dev/null").stdout
            expect(not rc.strip(), f"the driver exited early:\n{driver_log()[-2000:]}")
            return False

        wait_for("the sign-in flows finished", driver_done, DRIVER_SECONDS,
                 lambda: f":\n{driver_log()[-2000:]}")
        for line in status.get("notes") or []:
            print(f"  {line}")
        expect(status.get("ok") is True, f"the sign-in flows failed: {status.get('failure')}")
        print(f"ok: all {len(FLOWS)} flows signed in")

        spec = json.dumps({"patterns": patterns, "control": control_patterns, "pid": status["pid"],
                           "port": status["port"], "profile": status["profile"],
                           "capture": status["capture"], "flows": len(FLOWS)})
        live = subprocess.run(["docker", "exec", "-i", "-u", "hermes", agent, PYTHON,
                               "/test/searcher.py", "live"], input=spec, capture_output=True,
                              text=True, timeout=900)
        expect(live.returncode == 0, f"the live search failed: {(live.stdout + live.stderr)[-600:]}")
        live_report = json.loads(live.stdout)
        missing = {"capture", "browser", "memory"} - set(live_report["control_surfaces"])
        expect(not missing, f"the control value was not found on {sorted(missing)}; the canary "
                            "search does not reach those surfaces, so a clean run means nothing")
        print(f"ok: searched the agent browser and the stand-in's memory ({live_report['stats']})")

        docker("exec", agent, "/bin/sh", "-c", "touch /tmp/e2e-go")
        wait_for("the driver exited", lambda: docker(
            "exec", agent, "/bin/sh", "-c", "cat /tmp/e2e-driver.rc 2>/dev/null").stdout.strip() != "",
            120, lambda: f":\n{driver_log()[-1000:]}")
        profile = subprocess.run(["docker", "exec", "-i", "-u", "hermes", agent, PYTHON,
                                  "/test/searcher.py", "profile"], input=spec, capture_output=True,
                                 text=True, timeout=900)
        expect(profile.returncode == 0,
               f"the profile search failed: {(profile.stdout + profile.stderr)[-600:]}")
        profile_report = json.loads(profile.stdout)
        print(f"ok: searched the agent Chromium's profile ({profile_report['stats']})")

        log_hits = scan_text(provision_output, patterns, "the provisioning container's output")
        log_hits += scan_text(driver_log(), patterns, "the stand-in's own output")
        logs = kit.container_logs(site, agent, signer, broker)
        for name, text in logs.items():
            log_hits += scan_text(text, patterns, f"docker logs {name.rsplit('-', 1)[1]}")
        expect(scan_text(logs[site], control_patterns, "site"),
               "docker logs of the site container does not even hold the control value")
        print(f"ok: searched docker logs of {len(logs)} containers, the provisioning output "
              "and the stand-in's output")

        found = live_report["hits"] + profile_report["hits"] + log_hits
        if found:
            for line in sorted(set(found)):
                print(f"FAIL: {line}")
            fail(f"{len(set(found))} canary hit(s); a value reached the agent side")
    finally:
        kit.cleanup()
        docker("volume", "rm", "-f", volume, timeout=120)
        docker("image", "rm", "-f", signin_image, timeout=300)
    print("ok")


if __name__ == "__main__":
    main()
