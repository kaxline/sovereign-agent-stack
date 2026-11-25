#!/usr/bin/env python3
"""Checks for the Bitwarden fill and sign-in overlays.

(patch-bitwarden-broker-fill.py and patch-bitwarden-signin.py.)

Host part checks the wiring. With the pinned Hermes image present (or
--image), the image driver applies the overlays twice, launches the image's
headless Chromium on a local login page and runs a stub broker that writes
through the real broker/cdp.py and refuses a call without the bearer token. It
checks that a fill reaches the stub with the supervisor's target id and the
browser provider name the task's cdp_url maps to,
that the seven sign-in actions are in the schema and route to the /signin/
routes with the right bodies, that a sign_in_only item's fill_login and
enter_code carry the fixed refusal, that list marks those items,
that PluginLoginBackend.resolve_password never runs, that the redaction entry
is registered, that a vault_ handle still takes core's path, the broker-down
results, the TOTP routing and fallback, an unpatched save_login, and that the
broker's normalize_origin agrees with core's. Passwords are generated per run
and must not appear in any tool result or log line.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

import hermes_image  # noqa: E402

BROKER_SRC = ROOT / "compose" / "bitwarden-broker"


# The other overlays that edit tools/browser_vault_tool.py, in overlays.list order.
EARLIER_VAULT_OVERLAYS = ("/bootstrap/patch-browser-exec-hint.py", "/bootstrap/patch-vault-create-login.py",
                          "/bootstrap/patch-vault-capture-secret.py")


def test_overlay_order() -> None:
    order = hermes_image.overlay_order(hermes_image.OVERLAY_LIST)
    expect(order.index("patch-digest-redaction.py") < order.index("patch-bitwarden-broker-fill.py"),
           "the fill overlay runs after the digest overlay")
    # These also patch tools/browser_vault_tool.py and run first; the image driver applies them in this order.
    earlier = [path.rsplit("/", 1)[1] for path in EARLIER_VAULT_OVERLAYS]
    expect(all(name in order for name in earlier)
           and sorted(earlier, key=order.index) == earlier
           and order.index(earlier[-1]) < order.index("patch-bitwarden-broker-fill.py"),
           f"{earlier} run in this order, before the fill overlay")
    patch = (ROOT / "compose" / "hermes" / "patch-bitwarden-broker-fill.py").read_text()
    for name in ("_ensure_supervisor", "focus_page", "_page_session_id",
                 "return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id)",
                 "return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id, with_identifier)"):
        expect(name in patch, f"NEEDED names {name}")
    expect(order.index("patch-bitwarden-broker-fill.py") < order.index("patch-bitwarden-signin.py"),
           "the sign-in overlay runs after the fill overlay")
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in ("patch-bitwarden-signin.py", "vault_bitwarden_signin.py"):
        expect(f"./compose/hermes/{name}:/bootstrap/{name}:ro" in compose, f"{name} is mounted under /bootstrap")


def test_signin_patch_touches_one_upstream_file() -> None:
    """The fork cap: the sign-in patch edits tools/browser_vault_tool.py and installs its own module."""
    import re

    patch = (ROOT / "compose" / "hermes" / "patch-bitwarden-signin.py").read_text()
    edited = set(re.findall(r'"([a-z_]+\.py)"', patch)) | set(re.findall(r'/ "([a-z_/]+\.py)"', patch))
    expect(edited <= {"browser_vault_tool.py", "vault_bitwarden_signin.py"},
           f"only browser_vault_tool.py is edited, not {sorted(edited)}")

def test_the_broker_is_addressed_by_service_name_with_its_token() -> None:
    """The broker is a container of its own now, reached over the credentials network."""
    compose = (ROOT / "docker-compose.yml").read_text()
    backend = (ROOT / "compose" / "hermes" / "bitwarden_broker_backend.py").read_text()
    expect("http://bitwarden-broker:8099" in backend, "the backend calls the service by name")
    expect("127.0.0.1:8099" not in backend, "the backend does not call loopback")
    expect("/run/bitwarden/api-token" in backend, "the backend reads the bearer token")
    expect("/run/bitwarden:ro" in compose, "hermes mounts the token folder read-only")


def test_one_broker_client() -> None:
    """The backend module is the only broker client; the fill and sign-in code import it."""
    hermes = ROOT / "compose" / "hermes"
    fill = (hermes / "patch-bitwarden-broker-fill.py").read_text()
    signin = (hermes / "vault_bitwarden_signin.py").read_text()
    for name, src in (("patch-bitwarden-broker-fill.py", fill), ("vault_bitwarden_signin.py", signin)):
        expect("from plugins.bwbroker import bitwarden_broker_backend" in src, f"{name} imports the backend")
        for copy in ("bitwarden-broker:8099", "/run/bitwarden/api-token", "build_opener", "MAX_REPLY",
                     '"bwbroker:"', "sys.modules"):
            expect(copy not in src, f"{name} keeps no copy of the client ({copy})")


def test_cdp_url_hosts_map_to_provider_names() -> None:
    """The broker takes a provider name; the overlay has one per browser it can reach."""
    patch = (ROOT / "compose" / "hermes" / "patch-bitwarden-broker-fill.py").read_text()
    table = patch[patch.index("_BWBROKER_BROWSERS = {"):].split("}", 1)[0]
    for host, name in (("web-browser", "stack"), ("host.docker.internal", "boundary")):
        expect(f'"{host}": "{name}"' in table, f"{host} maps to {name}")
    expect("127.0.0.1" not in table and "localhost" not in table,
           "a loopback browser has no provider: the broker is not in its namespace")
    expect('"port"' not in patch and "'port'" not in patch, "no request body still sends a port")


def test_the_shared_tab_refusal_fits_both_callers() -> None:
    """_bwbroker_tab serves fill and sign_in, so its supervisor_required text must suit both.

    vault_bitwarden_signin._tab calls it for a sign-in; text naming a fill sent the agent back to
    fill_login instead of sign_in."""
    patch = (ROOT / "compose" / "hermes" / "patch-bitwarden-broker-fill.py").read_text()
    block = patch[patch.index("def _bwbroker_tab("):patch.index("def _bwbroker_fill(")]
    expect("Vault fill requires" not in block,
           "the supervisor_required text does not describe the call as a fill")


IMAGE_DRIVER = r'''
import json, logging, os, secrets, socket, string, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

env = dict(os.environ, DIGEST_REDACTION_BOOTSTRAP="/broker-src/broker")
# The overlays the container runs before ours on the same file, so the anchors are
# checked against the file as the container has it.
for script in EARLIER_VAULT_OVERLAYS:
    out = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "warn:" not in out.stdout, script + ": " + out.stdout + out.stderr
for script in ("/bootstrap/patch-digest-redaction.py", "/bootstrap/patch-bitwarden-broker-fill.py"):
    for _ in range(2):
        out = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stdout + out.stderr
    assert "already applied" in out.stdout, out.stdout
src_before_signin = open("/opt/hermes/tools/browser_vault_tool.py").read()
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-bitwarden-signin.py"], capture_output=True,
                         text=True, env=env)
    assert out.returncode == 0 and "warn:" not in out.stdout, out.stdout + out.stderr
assert "already applied (sign-in dispatch)" in out.stdout, out.stdout
src = open("/opt/hermes/tools/browser_vault_tool.py").read()
assert src.count("# assistant-stack: bitwarden broker fill\n") == 1, "one helper block"
assert src.count("# assistant-stack: bitwarden broker fill (") == 4, "four call sites"

# --- test_patch_line_count ---------------------------------------------------
ADDED = len(src.splitlines()) - len(src_before_signin.splitlines())
assert ADDED <= 50, f"the sign-in patch inserts {ADDED} upstream lines"

# --- test_fill_behavior_unchanged_after_extract ------------------------------
# _bwbroker_fill gets its tab through the extracted helper; the fill checks below are unchanged.
assert "def _bwbroker_tab(" in src, "the tab helper is extracted"
fill_body = src[src.index("def _bwbroker_fill("):src.index("def _bwbroker_list_hint(")]
assert "_bwbroker_tab(" in fill_body, "_bwbroker_fill calls the extracted helper"

# The fill and sign-in code import the broker client from the installed bwbroker plugin.
out = subprocess.run(["sh", "/bootstrap/14-install-bwbroker-plugin.sh"], capture_output=True, text=True)
assert out.returncode == 0 and "warn:" not in out.stdout, out.stdout + out.stderr
sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, "/broker-src")
sys.path.insert(0, "/bootstrap")
from broker import cdp
from broker.origins import normalize_origin as broker_normalize
from plugins.bwbroker import bitwarden_broker_backend as bwb
import agent.vault_backends as vb
import agent.vault_backends.base as vbase
from agent.vault_backends.plugin import PluginLoginBackend
from agent.vault_store import VaultItemMeta, VaultError, normalize_origin as core_normalize
from agent import redact
from agent.digest_redaction import build_entry
import tools.browser_session as bsession
import tools.browser_vault_tool as bvt
from tools.browser_supervisor import SUPERVISOR_REGISTRY

records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logging.getLogger().addHandler(Capture())
logging.getLogger().setLevel(logging.DEBUG)
results = []  # every tool result, searched for the secrets at the end

def tool(fn, *a, **k):
    raw = fn(*a, **k)
    results.append(raw)
    return json.loads(raw)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port

# --- a local login site ------------------------------------------------------
SITE_PORT = free_port()
ORIGIN = f"http://127.0.0.1:{SITE_PORT}"
PAGES = {
    "/login": '<html><body><form><input name="username"><input type="password" name="password">'
              '<button>Sign in</button></form></body></html>',
    "/otp": '<html><body><form><label for="c">Verification code</label>'
            '<input id="c" name="otp" autocomplete="one-time-code"><button>Verify</button></form></body></html>',
}
class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "<html><body>other</body></html>").encode()
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass
site = ThreadingHTTPServer(("127.0.0.1", SITE_PORT), Site)
threading.Thread(target=site.serve_forever, daemon=True).start()

# --- headless Chromium, as in spike 1 ---------------------------------------
udd = tempfile.mkdtemp()
# The bundled Chromium's directory differs by architecture (chrome-linux on
# arm64, chrome-linux64 on x64), so find the binary rather than name it.
import glob
chrome_bins = sorted(glob.glob("/opt/hermes/tools/chromium-*/chrome-linux*/chrome"))
if not chrome_bins:
    sys.exit("no bundled Chromium under /opt/hermes/tools/chromium-*/chrome-linux*/")
chrome = subprocess.Popen([chrome_bins[0], "--headless=new", "--no-sandbox",
                           "--disable-gpu", "--no-first-run", "--remote-debugging-port=0", f"--user-data-dir={udd}",
                           f"{ORIGIN}/login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
active = os.path.join(udd, "DevToolsActivePort")
for _ in range(200):
    if os.path.exists(active) and len(open(active).read().split()) >= 2:
        break
    time.sleep(0.1)
CDP_PORT_S, WS_PATH = open(active).read().split()[:2]
CDP_PORT = int(CDP_PORT_S)
WS_URL = f"ws://127.0.0.1:{CDP_PORT}{WS_PATH}"
def cdp_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}{path}", method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
        return json.loads(r.read())
for _ in range(100):
    pages = [t for t in cdp_json("/json/list") if t.get("type") == "page" and t.get("url", "").startswith(ORIGIN)]
    if pages:
        break
    time.sleep(0.1)
LOGIN_TARGET = pages[0]["id"]
time.sleep(0.5)

# --- stub broker on 8099: real cdp.fill, synthetic values -------------------
# The broker's bearer token, in a file the overlay and the backend are pointed at, and the one
# browser provider the stub knows: "stack", which here is the test's own Chromium.
API_TOKEN = secrets.token_hex(32)
TOKEN_PATH = os.path.join(tempfile.mkdtemp(), "api-token")
with open(TOKEN_PATH, "w") as handle:
    handle.write(API_TOKEN + "\n")
BROKER_BASE = "http://127.0.0.1:8099"
PASSWORD = "Pw+" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(21))
TOTP = "".join(secrets.choice(string.digits) for _ in range(6))
H_OTP, H_PLAIN = "bwbroker:acme/item-otp", "bwbroker:acme/item-plain"
H_ELSEWHERE = "bwbroker:acme/item-elsewhere"
H_SIO = "bwbroker:acme/item-signin-only"
ELSEWHERE = "https://elsewhere.invalid"
ITEMS = [
    # in-page fillable: its /fill succeeds below, and the broker derives the listing flag from the
    # same in_page_fill that gates /fill, so it cannot report this one as sign-in only.
    {"handle": H_OTP, "label": "Acme", "username": "agent@example.com", "origins": [ORIGIN], "has_totp": True,
     "sign_in_only": False},
    # Sign-in only on the open page's origin, so a fill reaches the item's own policy.
    {"handle": H_SIO, "label": "Acme SSO", "username": "agent4@example.com", "origins": [ORIGIN],
     "has_totp": False, "sign_in_only": True},
    {"handle": H_PLAIN, "label": "Acme plain", "username": "agent2@example.com", "origins": [ORIGIN],
     "has_totp": False, "sign_in_only": False},
    # No tab is ever opened on this origin, so sign_in must refuse before the broker is called.
    {"handle": H_ELSEWHERE, "label": "Elsewhere", "username": "agent3@example.com",
     "origins": [ELSEWHERE], "has_totp": False, "sign_in_only": True},
]
SNAPSHOT = {"origin": ORIGIN, "nodes": [{"ref": 1, "role": "textbox", "name": "Password", "text": ""},
                                        {"ref": 2, "role": "button", "name": "Sign in", "text": "Sign in"}]}
SIGNIN_STATUS = {"busy": 409, "no_session": 409, "stale_ref": 409, "origin_mismatch": 403,
                 "not_a_secret_field": 422, "no_totp": 400, "handover_refused": 409, "page_error": 502,
                 "timeout": 504, "signer_unavailable": 503, "sandbox_unavailable": 503,
                 "account_locked": 503, "unknown_handle": 404, "unavailable": 503}
STATE = {"fills": [], "posts": [], "accounts": [{"slug": "acme", "mode": "cloud", "scope": "folder",
                                                 "state": "unlocked", "error_class": None},
                                                {"slug": "home", "mode": "cloud", "scope": "folder",
                                                 "state": "locked", "error_class": None}],
         "fill_mode": "real", "signin_mode": "ok", "unauthorized": 0}
class Broker(BaseHTTPRequestHandler):
    def _send(self, status, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _authorized(self):
        if self.headers.get("Authorization") == "Bearer " + API_TOKEN:
            return True
        STATE["unauthorized"] += 1
        self._send(401, {"error": "unauthorized"})
        return False
    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/items":
            return self._send(200, ITEMS)
        if self.path == "/accounts":
            return self._send(200, STATE["accounts"])
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not_found"})
    def do_POST(self):
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["posts"].append((self.path, body))
        if self.path.startswith("/signin/"):
            return self.signin(self.path[len("/signin/"):], body)
        STATE["fills"].append(body)
        if STATE["fill_mode"] == "sign_in_only":
            return self._send(409, {"error": "sign_in_only"})
        if STATE["fill_mode"] == "html":
            return self._send(500, None, raw=b"<html>Internal Server Error " + PASSWORD.encode() + b"</html>")
        if STATE["fill_mode"] == "code":
            return self._send(200, {"filled": False, "origin": "", "error": STATE["code"], "redaction": STATE.get("red")})
        assert set(body) == {"handle", "target_id", "browser", "field", "base"}, sorted(body)
        item = next((i for i in ITEMS if i["handle"] == body["handle"]), None)
        if item is None:
            return self._send(404, {"error": "unknown_handle"})
        if body["field"] == "totp" and not item["has_totp"]:
            return self._send(400, {"error": "no_totp"})
        value = TOTP if body["field"] == "totp" else PASSWORD
        endpoint = BROWSERS.get(body["browser"])
        assert endpoint, body  # the real broker answers unknown_browser; nothing here sends one
        result = cdp.fill(endpoint, body["target_id"], tuple(item["origins"]), value, field=body["field"])
        redaction = None
        if result.filled or result.error not in (cdp.TARGET_NOT_FOUND, cdp.ORIGIN_MISMATCH, cdp.NO_FIELD):
            redaction = build_entry(value, body["base"])
        self._send(200, {"filled": result.filled, "origin": result.origin, "error": result.error,
                         "redaction": redaction})
    def signin(self, name, body):
        mode = STATE["signin_mode"]
        if mode != "ok":
            return self._send(SIGNIN_STATUS[mode], {"error": mode})
        if name == "start":
            assert set(body) == {"handle", "target_id", "browser"}, sorted(body)
        elif name == "finish":
            assert set(body) == {"target_id", "browser"}, sorted(body)
        elif name == "click":
            assert set(body) == {"ref"}, sorted(body)
        elif name == "type":
            assert set(body) == {"ref", "text"}, sorted(body)
        elif name == "fill":
            assert set(body) == {"ref", "field"}, sorted(body)
        else:
            assert body == {}, body
        if name == "cancel":
            return self._send(200, {"cancelled": True})
        if name == "finish":
            return self._send(200, {"done": True, "origin": ORIGIN, "cookies": 3})
        self._send(200, dict(SNAPSHOT))
    def log_message(self, *a):
        pass
broker = ThreadingHTTPServer(("127.0.0.1", 8099), Broker)
threading.Thread(target=broker.serve_forever, daemon=True).start()

# --- Hermes wiring: the bwbroker plugin backend, a local vault_ item --------
resolve_calls = []
def refuse_resolve(self, handle):
    resolve_calls.append(handle)
    raise AssertionError("resolve_password must not run for a bwbroker handle")
PluginLoginBackend.resolve_password = refuse_resolve
bwb.BROKER_URL = BROKER_BASE
bwb.TOKEN_FILE = TOKEN_PATH
BW = PluginLoginBackend("bwbroker", bwb.BrokerLoginBackend(in_cron=lambda: False, base_url=BROKER_BASE))

class Local(vbase.LoginBackend):
    name, display_name, prefix = "local", "Local vault", "vault_"
    def list_items(self):
        return [VaultItemMeta(id="vault_abc", kind="login", label="Acme local", origin=ORIGIN, created_at="",
                              identifier_type="email", identifier="me@example.com")]
    def get_meta(self, handle):
        return next((m for m in self.list_items() if m.id == handle), None)
    def resolve_password(self, handle):
        return "local-" + PASSWORD[::-1]
class OnePass(Local):
    name, display_name, prefix = "onepassword", "1Password", "op:"
    def list_items(self):
        return []

BACKENDS = [Local(), BW]
def set_backends(*backends):
    BACKENDS[:] = backends
vb.enabled_backends = vbase.enabled_backends = lambda: list(BACKENDS)
vb.backend_for_handle = vbase.backend_for_handle = lambda h: next((b for b in BACKENDS if b.owns(h)), None)
bsession.session_for_task = lambda task_id: {"cdp_url": WS_URL}
TASK = "fill-test"

# --- test_cdp_url_maps_to_a_provider_name -----------------------------------
# The broker holds each provider's endpoint and token and is asked for one by name, so the overlay
# turns the task's cdp_url into a name. A host with no provider -- this test's loopback Chromium
# among them, now that the broker has its own network namespace -- gives None.
for url, want in (("ws://web-browser:9222/devtools/browser/stack?token=t", "stack"),
                  ("ws://host.docker.internal:19222/devtools/browser/boundary", "boundary"),
                  (WS_URL, None), ("", None), (None, None)):
    bsession.session_for_task = lambda task_id, u=url: {"cdp_url": u}
    got = bvt._bwbroker_browser(None, TASK)
    assert got == want, (url, got, want)
bsession.session_for_task = lambda task_id: {"cdp_url": WS_URL}
# The rest of this run writes into that loopback Chromium, which the stub serves as "stack".
bvt._BWBROKER_BROWSERS["127.0.0.1"] = "stack"
BROWSERS = {"stack": WS_URL}

def page_value(selector):
    sup = SUPERVISOR_REGISTRY.get(TASK)
    res = sup.evaluate_runtime(f"document.querySelector({json.dumps(selector)}).value.length")
    assert res.get("ok"), res
    return res["result"]

def secrets_absent(where):
    for text in [*results, *records]:
        assert PASSWORD not in text and TOTP not in text, f"a value surfaced in {where}"

# --- test_fill_routes_to_broker ---------------------------------------------
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res == {"success": True, "filled": ["password"], "origin": ORIGIN, "next": res.get("next")}, res
assert res["next"] and "enter_code" in res["next"], res
assert not resolve_calls, "PluginLoginBackend.resolve_password was called"
assert len(STATE["fills"]) == 1, STATE["fills"]
fill = STATE["fills"][-1]
assert fill["target_id"] == LOGIN_TARGET, (fill["target_id"], LOGIN_TARGET)
assert fill["browser"] == "stack" and fill["field"] == "password" and fill["handle"] == H_PLAIN, fill
assert fill["base"] == redact.vault_redaction_base(), "the profile's base is sent"
assert page_value("input[type=password]") == len(PASSWORD), "the broker wrote the page"
scrubbed = redact.redact_registered_vault_values(f"page says {PASSWORD} here")
assert PASSWORD not in scrubbed and "«redacted-vault-secret»" in scrubbed, "the digest entry is registered"
redact.clear_vault_redaction_values()

# --- test_preview_target_refused (v0.23+) -------------------------------------
# With target='preview' the operation runs on the Desktop in-app page; the broker can only write
# into the managed browser's tab, so it must refuse rather than fill there.
try:
    from tools import browser_vault_preview as bvp
except ImportError:
    bvp = None
if bvp is not None:
    before = len(STATE["fills"])
    token = bvp._page.set(object())
    try:
        res = json.loads(bvt._bwbroker_fill(H_PLAIN, object(), ORIGIN, TASK))
    finally:
        bvp._page.reset(token)
    assert res.get("error_type") == "supervisor_required", res
    assert len(STATE["fills"]) == before, "the broker was not called"

# A vault_ handle takes core's path, which resolves its own password.
core_calls = []
real_fill_bound = bvt._fill_bound_page
def record_core(backend, handle, meta, page_origin, task_id, *rest):  # v0.23 adds with_identifier
    core_calls.append(handle)
    return real_fill_bound(backend, handle, meta, page_origin, task_id, *rest)
bvt._fill_bound_page = record_core
before = len(STATE["fills"])
res = tool(bvt.browser_vault_fill, "vault_abc", task_id=TASK)
assert core_calls == ["vault_abc"] and len(STATE["fills"]) == before, (core_calls, res)
assert res.get("success") and res.get("filled_fields") == 1 and res.get("backend") == "local", res
bvt._fill_bound_page = real_fill_bound
redact.clear_vault_redaction_values()

# Broker error codes come back core-shaped, with the code as error_type and fixed text only.
STATE["fill_mode"] = "code"
for code in ("target_not_found", "no_field", "write_unconfirmed", "page_error", "cdp_unavailable"):
    STATE["code"] = code
    STATE["red"] = build_entry(PASSWORD, redact.vault_redaction_base()) if code in ("write_unconfirmed", "page_error", "cdp_unavailable") else None
    res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
    assert res["success"] is False and res["error_type"] == code and res["error"], res
    registered = PASSWORD not in redact.redact_registered_vault_values(PASSWORD)
    assert registered == (STATE["red"] is not None), (code, "redaction registered whenever returned")
    redact.clear_vault_redaction_values()
STATE["code"], STATE["red"] = "origin_mismatch", None
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res["error_type"] == "origin_mismatch" and res["error"].startswith("Refused: current page origin"), res
assert "Vault fills only run on the exact origin(s) the credential was saved for." in res["error"], res
# A refused entry (a clear moved the base) logs one fixed line and the fill result stands.
STATE["code"] = "write_unconfirmed"
STATE["red"] = build_entry(PASSWORD, redact.vault_redaction_base() + 1)
records.clear()
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res["error_type"] == "write_unconfirmed", res
assert any(r == "bitwarden fill: redaction entry refused" for r in records), records
STATE["fill_mode"], STATE["red"] = "real", None
redact.clear_vault_redaction_values()

# --- test_broker_down_result ------------------------------------------------
UNAVAILABLE = {"success": False,
               "error": "Bitwarden is unavailable right now. Try again shortly, or ask the user to check make doctor."}
STATE["fill_mode"] = "html"  # answers, but not with JSON
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res == UNAVAILABLE, res
STATE["fill_mode"] = "real"
broker.shutdown(); broker.server_close()
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res == UNAVAILABLE, res
listing = tool(bvt.browser_vault_list)
assert listing["success"] and listing["bitwarden"] == {"unavailable": True}, listing
assert [i["handle"] for i in listing["items"]] == ["vault_abc"], "the local vault is still listed"
broker = ThreadingHTTPServer(("127.0.0.1", 8099), Broker)
threading.Thread(target=broker.serve_forever, daemon=True).start()
listing = tool(bvt.browser_vault_list)
assert listing["bitwarden"] == {"locked": ["home"], "unlock": "make vault-unlock SLUG=<slug>"}, listing
assert {i["handle"] for i in listing["items"]} == {"vault_abc", H_OTP, H_SIO, H_PLAIN, H_ELSEWHERE}, listing
set_backends(Local())
assert "bitwarden" not in tool(bvt.browser_vault_list), "no hint without the bwbroker backend"
set_backends(Local(), BW)

# --- test_totp_fallback -----------------------------------------------------
import agent.vault_backends.unlock as unlock
asked = []
unlock.can_prompt_here = lambda prompt=None: True  # v0.23.2 passes the prompt
unlock.get_code_prompt_callback = lambda: (lambda site, hint: asked.append(site) or "")
cdp_json(f"/json/new?{ORIGIN}/otp", method="PUT")
time.sleep(1.0)
otp_target = next(t["id"] for t in cdp_json("/json/list") if t.get("url") == f"{ORIGIN}/otp")
before = len(STATE["fills"])
res = tool(bvt.browser_vault_enter_code, H_PLAIN, task_id=TASK)
assert asked and res.get("error_type") == "code_declined", (asked, res)
assert len(STATE["fills"]) == before, "has_otp false never calls /fill"
asked.clear()
res = tool(bvt.browser_vault_enter_code, H_OTP, task_id=TASK)
assert not asked, "has_otp true never asks the user"
assert res.get("success") is True and res.get("filled") == ["totp"] and res.get("origin") == ORIGIN, res
fill = STATE["fills"][-1]
assert fill["field"] == "totp" and fill["target_id"] == otp_target and fill["browser"] == "stack", fill
assert page_value("input[name=otp]") == len(TOTP), "the code was written"
assert TOTP not in redact.redact_registered_vault_values(f"code {TOTP}"), "the code's digest is registered"
assert not resolve_calls
redact.clear_vault_redaction_values()

# --- test_save_untouched ---------------------------------------------------
# save_login writes to the local vault only, so the overlay leaves it as core wrote it.
save_body = src[src.index("def browser_vault_save_login("):src.index("_TAB_PROBES[\"otp\"] = ")]
assert "assistant-stack: bitwarden broker fill" not in save_body, "save_login is not patched"
assert set(BACKENDS) and any(b.name == "bwbroker" for b in BACKENDS)
unlock.get_save_login_prompt_callback = lambda: (lambda origin, host: None)
res = tool(bvt.browser_vault_save_login, task_id=TASK)
assert res.get("error_type") == "save_declined", res

def snapshot_result(res):
    """A sign-in result without its next-step hint (every result but the cancel carries one)."""
    hint = res.pop("next", None)
    assert hint or not res.get("success") or "cancelled" in res, res
    return res


# --- test_schema_has_signin_actions -----------------------------------------
import tools.vault_bitwarden_signin as bvs
props = bvt.BROWSER_VAULT_SCHEMA["parameters"]["properties"]
assert bvs.ACTIONS == ("sign_in", "signin_snapshot", "signin_click", "signin_type", "signin_fill",
                       "signin_finish", "signin_cancel"), bvs.ACTIONS
for action in bvs.ACTIONS:
    assert action in props["action"]["enum"], (action, props["action"]["enum"])
assert props["ref"]["type"] == "integer" and props["text"]["type"] == "string", props
assert set(props["field"]["enum"]) == {"password", "totp"}, props["field"]
for action in ("list", "fill", "save_login", "enter_code", "unlock"):
    assert action in props["action"]["enum"], "the upstream actions are kept"

# --- test_dispatch_routes_each_action ---------------------------------------
STATE["posts"].clear()
res = tool(bvt._handle_vault, {"action": "sign_in", "handle": H_OTP}, task_id=TASK)
assert snapshot_result(res) == {"success": True, "origin": ORIGIN, "nodes": SNAPSHOT["nodes"]}, res
route, body = STATE["posts"][-1]
assert route == "/signin/start", route
# No form probe: the handover tab is the first open tab on the item's origin.
SITE_TARGETS = {t["id"] for t in cdp_json("/json/list") if t.get("url", "").startswith(ORIGIN)}
assert body == {"handle": H_OTP, "target_id": body["target_id"], "browser": "stack"}, body
assert body["target_id"] in SITE_TARGETS, (body, SITE_TARGETS)
SIGNIN_TAB = body["target_id"]

# --- test_sign_in_refuses_without_a_tab_on_the_item_origin ------------------
# The signer types the password into the live site before anything checks where the session
# can go, so a sign_in started with no tab on the item's origin signs in for nothing and ends
# in handover_refused at the end of the sequence. It must refuse first, with no call to the
# broker. Found on the live stack: three starts, two complete real sign-ins, all refused.
_posts_before = len(STATE["posts"])
res = tool(bvt._handle_vault, {"action": "sign_in", "handle": H_ELSEWHERE}, task_id=TASK)
assert res == {"success": False, "error_type": "no_page",
               "error": (f"Open the login page first: this task has no browser tab on {ELSEWHERE}. "
                         "Open it with browser_exec, then call sign_in again. Nothing was signed in.")}, res
assert len(STATE["posts"]) == _posts_before, STATE["posts"][_posts_before:]
for action, route, body, want in (
        ("signin_snapshot", "/signin/snapshot", {}, {"success": True, "origin": ORIGIN, "nodes": SNAPSHOT["nodes"]}),
        ("signin_click", "/signin/click", {"ref": 2}, {"success": True, "origin": ORIGIN, "nodes": SNAPSHOT["nodes"]}),
        ("signin_type", "/signin/type", {"ref": 1, "text": "agent@example.com"},
         {"success": True, "origin": ORIGIN, "nodes": SNAPSHOT["nodes"]}),
        ("signin_fill", "/signin/fill", {"ref": 1, "field": "password"},
         {"success": True, "origin": ORIGIN, "nodes": SNAPSHOT["nodes"]}),
        ("signin_finish", "/signin/finish", {"target_id": SIGNIN_TAB, "browser": "stack"},
         {"success": True, "done": True, "origin": ORIGIN, "cookies": 3}),
        ("signin_cancel", "/signin/cancel", {}, {"success": True, "cancelled": True})):
    args = {"action": action}
    if action == "signin_click":
        args["ref"] = 2
    if action == "signin_type":
        args["ref"], args["text"] = 1, "agent@example.com"
    if action == "signin_fill":
        args["ref"], args["field"] = 1, "password"
    res = tool(bvt._handle_vault, args, task_id=TASK)
    assert snapshot_result(res) == want, (action, res)
    assert STATE["posts"][-1] == (route, body), (action, STATE["posts"][-1])
# Every sign-in action reached the broker on its own route.
assert [p[0] for p in STATE["posts"]] == ["/signin/start", "/signin/snapshot", "/signin/click", "/signin/type",
                                          "/signin/fill", "/signin/finish", "/signin/cancel"], STATE["posts"]
# A broker code comes back as fixed text with the code as error_type, and never as page text.
STATE["signin_mode"] = "not_a_secret_field"
res = tool(bvt._handle_vault, {"action": "signin_fill", "ref": 1, "field": "password"}, task_id=TASK)
assert res["success"] is False and res["error_type"] == "not_a_secret_field" and res["error"], res
STATE["signin_mode"] = "ok"

# --- test_fill_login_sign_in_only_text / test_enter_code_sign_in_only_text ---
SIGN_IN_ONLY = ("This Bitwarden login is set up for sign_in. Call browser_vault action=sign_in with this handle.")
STATE["fill_mode"] = "sign_in_only"
res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
assert res == {"success": False, "error_type": "sign_in_only", "error": SIGN_IN_ONLY}, res
res = tool(bvt.browser_vault_enter_code, H_OTP, task_id=TASK)
assert res == {"success": False, "error_type": "sign_in_only", "error": SIGN_IN_ONLY}, res
STATE["fill_mode"] = "real"

# --- test_sign_in_only_survives_a_tab_refusal -------------------------------
# Boundary's browser is on host.docker.internal, where _bwbroker_cdp_port finds no loopback port
# and the tab helper refuses. The item's own sign_in_only is still what the agent must be told:
# it is the one refusal that names sign_in, and a plumbing error sends the agent back to a fill.
# fill_mode stays "real", so only the gate can produce this refusal, not the stub's 409.
_real_tab = bvt._bwbroker_tab
bvt._bwbroker_tab = lambda *a, **k: {"success": False, "error_type": "cdp_unavailable",
                                     "error": bvt._BWBROKER_ERRORS["cdp_unavailable"]}
try:
    res = tool(bvt.browser_vault_fill, H_SIO, task_id=TASK)
    assert res == {"success": False, "error_type": "sign_in_only", "error": SIGN_IN_ONLY}, res
    # A fillable item keeps the plumbing refusal: for it the tab error is the real answer.
    res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
    assert res["error_type"] == "cdp_unavailable", res
finally:
    bvt._bwbroker_tab = _real_tab

# --- test_list_sign_in_only_flag --------------------------------------------
listing = tool(bvt.browser_vault_list)
flags = {i["handle"]: i.get("sign_in_only") for i in listing["items"]}
assert flags == {"vault_abc": None, H_OTP: False, H_SIO: True, H_PLAIN: False,
                 H_ELSEWHERE: True}, flags

# --- test_signin_in_cron_turn_unknown_handle --------------------------------
# The broker holds one session for the whole stack, bound to no task, so a cron turn must not
# reach any action: not sign_in (no items are listed) and not the six that drive an open session.
CRON_BW = PluginLoginBackend("bwbroker", bwb.BrokerLoginBackend(in_cron=lambda: True))
set_backends(Local(), CRON_BW)
real_in_cron = bwb.in_cron_turn
bwb.in_cron_turn = lambda: True
STATE["posts"].clear()
try:
    for action in bvs.ACTIONS:
        args = {"action": action, "handle": H_OTP, "ref": 1, "text": "x", "field": "password"}
        res = tool(bvt._handle_vault, args, task_id=TASK)
        assert res == {"success": False, "error_type": "unknown_handle",
                       "error": "No Bitwarden login with that handle. Use browser_vault action=list."}, (action, res)
    assert STATE["posts"] == [], "a cron turn never reaches the broker"
finally:
    bwb.in_cron_turn = real_in_cron
    set_backends(Local(), BW)
# The signer's own task keeps its finish origin: a second task does not inherit it.
assert bvs._last_origin.get(TASK) is None, bvs._last_origin
res = tool(bvt._handle_vault, {"action": "signin_snapshot"}, task_id=TASK)
assert bvs._last_origin == {TASK: ORIGIN}, bvs._last_origin
STATE["posts"].clear()
res = tool(bvt._handle_vault, {"action": "signin_finish"}, task_id="other-task")
assert [p[0] for p in STATE["posts"]] == ["/signin/snapshot", "/signin/finish"], STATE["posts"]
assert bvs._last_origin[TASK] == ORIGIN, "the other task's finish left this task's origin alone"
res = tool(bvt._handle_vault, {"action": "signin_cancel"}, task_id=TASK)
assert bvs._last_origin == {}, bvs._last_origin

# --- test_upstream_actions_survive_a_missing_schema_anchor ------------------
# A Hermes bump that moves the schema anchor must not break page_ops: the dispatch entries name
# the actions themselves and import the module inside the lambda.
fake = "/tmp/fake-hermes"
os.makedirs(fake + "/tools", exist_ok=True)
# Same import, written differently: the schema needle no longer matches, the file still runs.
open(fake + "/tools/browser_vault_tool.py", "w").write(
    src_before_signin.replace("from tools.registry import no_cache_check_fn, registry",
                              "from tools.registry import registry, no_cache_check_fn", 1))
out = subprocess.run([sys.executable, "/bootstrap/patch-bitwarden-signin.py"], capture_output=True,
                     text=True, env=dict(env, HERMES_ROOT=fake))
assert out.returncode == 0, out.stdout + out.stderr
assert "skipped: anchor missing" in out.stdout and "'from tools.registry import no_cache_check_fn, registry'" in out.stdout, out.stdout
assert "patched sign-in dispatch" in out.stdout, out.stdout
partial = open(fake + "/tools/browser_vault_tool.py").read()
assert "# assistant-stack: bitwarden sign-in (schema)" not in partial, "the schema hook did not apply"
# Run that file's _handle_vault on its own, with stubs for what it calls.
body = partial[partial.index("def _handle_vault("):partial.index("\nfrom tools.registry import")]
calls = []
# The slice also carries the other overlays' schema hooks, which run against a copy.
scope = {"json": json, "_fenced_page_op": lambda tid, fn: fn(),
         "BROWSER_VAULT_SCHEMA": json.loads(json.dumps(bvt.BROWSER_VAULT_SCHEMA)),
         "logger": logging.getLogger("page_ops")}
for name in ("browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
             "browser_vault_save_login", "browser_vault_enter_code"):
    scope[name] = (lambda name=name: lambda *a, **k: calls.append(name) or "{}")()
exec(compile(body, "<page_ops>", "exec"), scope)
for action in ("list", "unlock", "fill", "save_login", "enter_code"):
    scope["_handle_vault"]({"action": action}, task_id=TASK)
assert calls == ["browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
                 "browser_vault_save_login", "browser_vault_enter_code"], calls
# The sign-in actions still route, through the import inside the lambda.
res = json.loads(scope["_handle_vault"]({"action": "sign_in", "handle": "bwbroker:gone/item"}, task_id=TASK))
assert res["error_type"] == "unknown_handle", res
assert calls == ["browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
                 "browser_vault_save_login", "browser_vault_enter_code"], "no upstream handler ran for it"

# --- test_a_call_without_the_token_is_refused -------------------------------
# The broker answers on a network, not on loopback inside this container, so the bearer token is
# what tells it the call is hermes'. Without the file the call is refused and nothing is written.
assert STATE["unauthorized"] == 0, "every call so far carried the token"
before = page_value("input[type=password]")
bwb.TOKEN_FILE = "/nonexistent/api-token"
try:
    res = tool(bvt.browser_vault_fill, H_PLAIN, task_id=TASK)
    # More than one: the sign-in-only gate reads /items before the fill, and that is refused too.
    assert res.get("success") is not True and STATE["unauthorized"], (res, STATE["unauthorized"])
    assert page_value("input[type=password]") == before, "a refused call wrote nothing"
finally:
    bwb.TOKEN_FILE = TOKEN_PATH
STATE["unauthorized"] = 0
redact.clear_vault_redaction_values()

# --- test_origin_normalization_matches_core ---------------------------------
URLS = [
    "https://example.com", "https://example.com/", "https://example.com:443/login?x=1#f", "http://example.com:80",
    "http://example.com:8080/a", "https://example.com:8443", "HTTPS://Example.COM/Path", "https://example.com.",
    "https://example.com.:443/", "https://user:pass@example.com/", "https://user@example.com:444",
    "http://xn--bcher-kva.example/", "https://bücher.example/", "https://[::1]:8080/", "http://127.0.0.1:3000",
    "https://example.com:0", "https://example.com:99999", "https://example.com:abc", "example.com", "/login",
    "", "   ", "https://", "ftp://files.example.com/x", "androidapp://com.example.app", "https://exa mple.com",
    " https://example.com ", "http://localhost", "https://sub.domain.example.co.uk:443/",
]
for url in URLS:
    try:
        core = core_normalize(url)
    except (VaultError, ValueError):
        core = None
    assert broker_normalize(url) == core, (url, broker_normalize(url), core)

secrets_absent("results or logs")
SUPERVISOR_REGISTRY.stop_all()
chrome.terminate()
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_overlay_order()
    test_signin_patch_touches_one_upstream_file()
    test_the_shared_tab_refusal_fits_both_callers()
    test_the_broker_is_addressed_by_service_name_with_its_token()
    test_one_broker_client()
    test_cdp_url_hosts_map_to_provider_names()
    print("ok")
    driver = f"EARLIER_VAULT_OVERLAYS = {EARLIER_VAULT_OVERLAYS!r}\n" + IMAGE_DRIVER
    hermes_image.maybe_run(driver, mounts=[(str(BROKER_SRC), "/broker-src")])


if __name__ == "__main__":
    main()
