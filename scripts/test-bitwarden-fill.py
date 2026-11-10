#!/usr/bin/env python3
"""Checks for the Bitwarden fill overlay (patch-bitwarden-broker-fill.py).

Host part checks the wiring. With the pinned Hermes image present (or
--image), the image driver applies the overlays twice, launches the image's
headless Chromium on a local login page and runs a stub broker on
127.0.0.1:8099 that writes through the real broker/cdp.py. It checks that a
bwbroker: fill reaches the stub with the supervisor's target id and CDP port,
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


# The other overlays that edit tools/browser_vault_tool.py, in 05-patch-tool-calling.sh order.
EARLIER_VAULT_OVERLAYS = ("/bootstrap/patch-browser-exec-hint.py", "/bootstrap/patch-vault-create-login.py",
                          "/bootstrap/patch-vault-capture-secret.py")


def test_overlay_order() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    expect(runner.index("patch-digest-redaction.py") < runner.index("patch-bitwarden-broker-fill.py"),
           "the fill overlay runs after the digest overlay")
    # These also patch tools/browser_vault_tool.py and run first; the image driver applies them in this order.
    for earlier in EARLIER_VAULT_OVERLAYS:
        name = earlier.rsplit("/", 1)[1]
        expect(f"run_patch {earlier}" in runner
               and runner.index(name) < runner.index("patch-bitwarden-broker-fill.py"),
               f"{name} runs before the fill overlay")
    patch = (ROOT / "compose" / "hermes" / "patch-bitwarden-broker-fill.py").read_text()
    for name in ("_ensure_supervisor", "focus_page", "_page_session_id",
                 "return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id)"):
        expect(name in patch, f"NEEDED names {name}")

IMAGE_DRIVER = r'''
import json, logging, os, secrets, socket, string, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

env = dict(os.environ, DIGEST_REDACTION_BOOTSTRAP="/broker-src/broker")
# The overlays 05-patch-tool-calling.sh runs before ours on the same file, so the anchors are
# checked against the file as the container has it.
for script in EARLIER_VAULT_OVERLAYS:
    out = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "warn:" not in out.stdout, script + ": " + out.stdout + out.stderr
for script in ("/bootstrap/patch-digest-redaction.py", "/bootstrap/patch-bitwarden-broker-fill.py"):
    for _ in range(2):
        out = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stdout + out.stderr
    assert "already applied" in out.stdout, out.stdout
src = open("/opt/hermes/tools/browser_vault_tool.py").read()
assert src.count("# assistant-stack: bitwarden broker fill\n") == 1, "one helper block"
assert src.count("# assistant-stack: bitwarden broker fill (") == 4, "four call sites"

sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, "/broker-src")
sys.path.insert(0, "/bootstrap")
from broker import cdp
from broker.origins import normalize_origin as broker_normalize
import bitwarden_broker_backend as bwb
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
chrome = subprocess.Popen(["/opt/hermes/tools/chromium-1208/chrome-linux/chrome", "--headless=new", "--no-sandbox",
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
PASSWORD = "Pw+" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(21))
TOTP = "".join(secrets.choice(string.digits) for _ in range(6))
H_OTP, H_PLAIN = "bwbroker:acme/item-otp", "bwbroker:acme/item-plain"
ITEMS = [
    {"handle": H_OTP, "label": "Acme", "username": "agent@example.com", "origins": [ORIGIN], "has_totp": True},
    {"handle": H_PLAIN, "label": "Acme plain", "username": "agent2@example.com", "origins": [ORIGIN], "has_totp": False},
]
STATE = {"fills": [], "accounts": [{"slug": "acme", "mode": "cloud", "scope": "folder", "state": "unlocked",
                                    "error_class": None},
                                   {"slug": "home", "mode": "cloud", "scope": "folder", "state": "locked",
                                    "error_class": None}],
         "fill_mode": "real"}
class Broker(BaseHTTPRequestHandler):
    def _send(self, status, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/items":
            return self._send(200, ITEMS)
        if self.path == "/accounts":
            return self._send(200, STATE["accounts"])
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not_found"})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE["fills"].append(body)
        if STATE["fill_mode"] == "html":
            return self._send(500, None, raw=b"<html>Internal Server Error " + PASSWORD.encode() + b"</html>")
        if STATE["fill_mode"] == "code":
            return self._send(200, {"filled": False, "origin": "", "error": STATE["code"], "redaction": STATE.get("red")})
        assert set(body) == {"handle", "target_id", "port", "field", "base"}, sorted(body)
        item = next((i for i in ITEMS if i["handle"] == body["handle"]), None)
        if item is None:
            return self._send(404, {"error": "unknown_handle"})
        if body["field"] == "totp" and not item["has_totp"]:
            return self._send(400, {"error": "no_totp"})
        value = TOTP if body["field"] == "totp" else PASSWORD
        result = cdp.fill(body["port"], body["target_id"], tuple(item["origins"]), value, field=body["field"])
        redaction = None
        if result.filled or result.error not in (cdp.TARGET_NOT_FOUND, cdp.ORIGIN_MISMATCH, cdp.NO_FIELD):
            redaction = build_entry(value, body["base"])
        self._send(200, {"filled": result.filled, "origin": result.origin, "error": result.error,
                         "redaction": redaction})
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
BW = PluginLoginBackend("bwbroker", bwb.BrokerLoginBackend(in_cron=lambda: False))

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
assert fill["port"] == CDP_PORT and fill["field"] == "password" and fill["handle"] == H_PLAIN, fill
assert fill["base"] == redact.vault_redaction_base(), "the profile's base is sent"
assert page_value("input[type=password]") == len(PASSWORD), "the broker wrote the page"
scrubbed = redact.redact_registered_vault_values(f"page says {PASSWORD} here")
assert PASSWORD not in scrubbed and "«redacted-vault-secret»" in scrubbed, "the digest entry is registered"
redact.clear_vault_redaction_values()

# A vault_ handle takes core's path, which resolves its own password.
core_calls = []
real_fill_bound = bvt._fill_bound_page
def record_core(backend, handle, meta, page_origin, task_id):
    core_calls.append(handle)
    return real_fill_bound(backend, handle, meta, page_origin, task_id)
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
assert {i["handle"] for i in listing["items"]} == {"vault_abc", H_OTP, H_PLAIN}, listing
set_backends(Local())
assert "bitwarden" not in tool(bvt.browser_vault_list), "no hint without the bwbroker backend"
set_backends(Local(), BW)

# --- test_totp_fallback -----------------------------------------------------
import agent.vault_backends.unlock as unlock
asked = []
unlock.can_prompt_here = lambda: True
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
assert fill["field"] == "totp" and fill["target_id"] == otp_target and fill["port"] == CDP_PORT, fill
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
    print("ok")
    driver = f"EARLIER_VAULT_OVERLAYS = {EARLIER_VAULT_OVERLAYS!r}\n" + IMAGE_DRIVER
    hermes_image.maybe_run(driver, mounts=[(str(BROKER_SRC), "/broker-src")])


if __name__ == "__main__":
    main()
