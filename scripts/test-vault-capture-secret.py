#!/usr/bin/env python3
"""Checks for browser_vault action=capture_secret and the vault.take RPC (CR 0024 R2).

Host part is stdlib-only: which values count as key-like, name rules, answer parsing and the schema.
With --image (or when Docker and the pinned Hermes image are present and --host-only is not given),
both vault patches are applied inside nousresearch/hermes-agent and the action runs in the image's
Chromium against local fixture pages, through browser_vault's dispatcher, the session prompt scope
and the vault.take JSON-RPC handler.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
import vault_capture_secret as vcs  # noqa: E402

failures = 0


def expect(cond: bool, msg: str) -> None:
    global failures
    if not cond:
        failures += 1
        print(f"FAIL: {msg}")


def test_looks_like_secret() -> None:
    yes = ["sk-test-4f9aQ2LmZx81BvT0", "sk-proj-AbCdEf0123456789xyzXYZ", "ghp_a1B2c3D4e5F6g7H8i9J0kLmNoPqRs",
           "3f9b2c71a0d84e6f9c1b7a2d5e8f4c30", "pk_live_51HxYzAbCdEfGhIjKlMnOp"]
    no = ["https://dash.example.com/settings/keys", "someone@example.com", "/usr/local/lib/python3",
          "Abcdefghijklmnopqrstuvwxyz", "aaaaaaaaaaaaaaaaaaaaaaaa", "short-Key1", "two words here and more here",
          "Settings-General-Billing-Keys", "12345678901234567890123"]
    for v in yes:
        expect(vcs.looks_like_secret(v), f"key-like: {v}")
    for v in no:
        expect(not vcs.looks_like_secret(v), f"not key-like: {v}")


def test_pick_candidates() -> None:
    raw = [{"where": "input #key", "value": "sk-test-4f9aQ2LmZx81BvT0"},
           {"where": "code", "value": "sk-test-4f9aQ2LmZx81BvT0"},
           {"where": "span", "value": "someone@example.com"},
           {"where": "pre", "value": "ghp_a1B2c3D4e5F6g7H8i9J0kLmNoPqRs"}]
    got = vcs.pick_candidates(raw)
    expect([c["where"] for c in got] == ["input #key", "pre"], f"dedupe and filter: {got}")
    expect(vcs.masked("sk-test-4f9aQ2LmZx81BvT0") == "…BvT0", "masked")


def test_names() -> None:
    for ok in ("PREM_API_KEY", "OPENAI_API_KEY", "MY_SERVICE_TOKEN"):
        expect(vcs.name_problem(ok) == "", f"allowed {ok}")
    for bad in ("prem_api_key", "AB", "HERMES_API_KEY", "OPENAI_BASE_URL", "PATH", "LD_PRELOAD", "API_SERVER_KEY",
                "HTTPS_PROXY", "MY-KEY", "PYTHONPATH"):
        expect(vcs.name_problem(bad) != "", f"refused {bad}")


def test_parse_answer() -> None:
    expect(vcs.parse_answer("approve") and vcs.parse_answer('{"approve": true}'), "approve")
    for raw in ("", None, "{}", '{"approve": "yes"}', "no", "[true]"):
        expect(not vcs.parse_answer(raw), f"decline {raw!r}")


def test_schema() -> None:
    schema = {"description": "Vault.", "parameters": {"properties": {
        "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock", "create_login"]}}}}
    vcs.extend_schema(schema)
    vcs.extend_schema(schema)
    props = schema["parameters"]["properties"]
    expect(props["action"]["enum"].count("capture_secret") == 1, "enum once")
    expect(props["destination"]["enum"] == ["env", "viewer"], "destination enum")
    expect(all(k in props for k in ("name", "selector", "candidate")), "props")
    expect(schema["description"].count("capture_secret (") == 1, "description once")
    expect("//" not in vcs.TAB_PROBE.split("JSON.parse(")[1].split("candidates.some")[0].replace("://", ""),
           "probe has no line comments (it is joined onto one line)")


# Runs inside the Hermes image with compose/hermes mounted at /bootstrap.
IMAGE_DRIVER = r'''
import http.server, json, os, subprocess, sys, tempfile, threading
from types import SimpleNamespace

def run(script):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
    return out.stdout

run("/bootstrap/patch-vault-create-login.py")
first = run("/bootstrap/patch-vault-capture-secret.py")
for part in ("capture_secret schema", "capture_secret dispatch", "capture_secret prompt", "vault.take method",
             "capture_secret request contract", "vault.take contract"):
    assert f"patched: {part}" in first, first
again = run("/bootstrap/patch-vault-capture-secret.py")
assert "patched" not in again.replace("already patched", ""), again

os.chdir("/opt/hermes")
sys.path.insert(0, "/opt/hermes")
HOME = tempfile.mkdtemp(prefix="vault-capture-")
os.environ["HERMES_HOME"] = HOME

from tui_gateway.contracts import METHODS, SERVER_REQUESTS
assert "vault.capture_secret" in SERVER_REQUESTS and "vault.take" in METHODS

KEY = "sk-test-" + "Qm7Lx2Vb9Rt4Wn8Kp3Zs6Hd1"
OTHER = "ghp_a1B2c3D4e5F6g7H8i9J0kLmNoPqRs"
FILLED = "Filled-Pw-7Hq2Lz9Xc4Vb"  # stands in for a password the broker filled
PAGES = {
    "/key": f"""<!doctype html><title>keys</title><h1>API keys</h1>
<p>Signed in as someone@example.com. Docs: https://docs.example.com/keys</p>
<label for=k>Your new key</label><input id=k readonly value="{KEY}"><button>Copy</button>""".encode(),
    "/two": f"""<!doctype html><title>keys</title>
<div>Project <code>proj-a</code></div><code id=a>{OTHER}</code><input id=b aria-label="Secret key" readonly value="{KEY}">
<input id=pw type=password value="{FILLED}">""".encode(),
    "/none": b"<!doctype html><title>empty</title><p>No keys yet.</p>",
}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path, b"nope")
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{srv.server_address[1]}"
TASK = "capture-e2e"

from tools import browser_use_cli as bu
from tools.browser_supervisor import SUPERVISOR_REGISTRY
import tools.browser_vault_tool as bvt
import gateway.run_turn_progress as rtp
import gateway.session_server_requests as ssr
from gateway.session_vault_prompt import vault_prompt_scope
from agent.vault_store import get_vault_store

def exec_(code):
    out = json.loads(bu.browser_exec(code, task_id=TASK, timeout_s=90))
    assert out.get("success"), out
    return out

frames = []
answer = {"value": "approve"}
def fake_ask_viewer(events, session_id, method, params, **kw):
    frames.append((method, dict(params)))
    return answer["value"]
ssr.ask_viewer = fake_ask_viewer
rtp.local_viewer_turn = lambda turn: True
turn = SimpleNamespace(_approval_owner=(SimpleNamespace(sessions={"s1": SimpleNamespace(event_stream=object())}), "s1", 1))
agent = SimpleNamespace(_interrupt_requested=False)

def vault(args):
    return bvt._handle_vault(args, task_id=TASK)

def in_turn(args):
    with vault_prompt_scope(turn, agent):
        return vault(args)

def env_file():
    p = os.path.join(HOME, ".env")
    return open(p).read() if os.path.exists(p) else ""

def rpc(method, params):
    from tui_gateway import server
    return server.handle_request({"jsonrpc": "2.0", "id": "t1", "method": method, "params": params})

try:
    exec_("new_tab('about:blank')")  # the supervisor's default page is not the key page
    exec_(f"new_tab({origin + '/key'!r}); wait_for_load()")

    out = json.loads(vault({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "env"}))
    assert out["error_type"] == "prompt_unavailable", out
    out = json.loads(in_turn({"action": "capture_secret", "name": "OPENAI_BASE_URL", "destination": "env"}))
    assert out["error_type"] == "bad_name" and not frames, out
    out = json.loads(in_turn({"action": "capture_secret", "name": "PREM_API_KEY"}))
    assert out["error_type"] == "destination_required", out

    # Declined: nothing written.
    answer["value"] = ""
    out = json.loads(in_turn({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "env"}))
    assert out["error_type"] == "save_declined" and "PREM_API_KEY" not in env_file(), out

    # env: the .env line, last four only in the result and the request.
    answer["value"] = "approve"
    raw = in_turn({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "env"})
    out = json.loads(raw)
    assert out["success"] and out["last4"] == "…" + KEY[-4:] and out["origin"] == origin and not out["replaced"], out
    assert KEY not in raw and f"PREM_API_KEY={KEY}" in env_file().replace("'", "").replace('"', ""), env_file()
    method, params = frames[-1]
    assert method == "vault.capture_secret" and params["last4"] == "…" + KEY[-4:] and KEY not in json.dumps(params), frames[-1]
    assert params["destination"] == "env" and params["replaces"] is False and params["capture_id"] == "", params

    # The agent prints the page afterwards: the key is redacted.
    printed = json.dumps(exec_("print(js('document.body.innerText + \" \" + document.getElementById(\"k\").value'))"))
    assert KEY not in printed and "redacted" in printed, printed

    # A second capture into the same name says it replaces the value.
    in_turn({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "env"})
    assert frames[-1][1]["replaces"] is True, frames[-1]

    # viewer: stored beside the vault, taken once over vault.take, never in vault.list.
    raw = in_turn({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "viewer"})
    out = json.loads(raw)
    assert out["success"] and out["capture_id"].startswith("capture_") and KEY not in raw, out
    assert frames[-1][1]["capture_id"] == out["capture_id"] and KEY not in json.dumps(frames[-1]), frames[-1]
    assert not get_vault_store().list_items()
    listed = rpc("vault.list", {})
    assert KEY not in json.dumps(listed), listed
    taken = rpc("vault.take", {"id": out["capture_id"]})
    assert taken.get("result") == {"name": "PREM_API_KEY", "origin": origin, "value": KEY}, taken
    again = rpc("vault.take", {"id": out["capture_id"]})
    assert "error" in again and KEY not in json.dumps(again), again
    assert "error" in rpc("vault.take", {}), "id required"

    # Several candidates: listed masked, then picked by number; a selector works too.
    exec_(f"goto_url({origin + '/two'!r}); wait_for_load()")
    raw = in_turn({"action": "capture_secret", "name": "GITHUB_TOKEN", "destination": "viewer"})
    out = json.loads(raw)
    assert out["error_type"] == "several_candidates" and KEY not in raw and OTHER not in raw, out
    lasts = [c["last4"] for c in out["candidates"]]
    assert lasts == ["…" + OTHER[-4:], "…" + KEY[-4:]], out["candidates"]
    out = json.loads(in_turn({"action": "capture_secret", "name": "GITHUB_TOKEN", "destination": "viewer", "candidate": 1}))
    assert out["success"] and out["last4"] == "…" + OTHER[-4:], out
    assert rpc("vault.take", {"id": out["capture_id"]})["result"]["value"] == OTHER
    out = json.loads(in_turn({"action": "capture_secret", "name": "PREM_API_KEY", "destination": "viewer", "selector": "#b"}))
    assert out["success"] and out["last4"] == "…" + KEY[-4:], out
    out = json.loads(in_turn({"action": "capture_secret", "name": "X_API_KEY", "destination": "viewer", "candidate": 5}))
    assert out["error_type"] == "bad_candidate", out
    out = json.loads(in_turn({"action": "capture_secret", "name": "X_API_KEY", "destination": "viewer", "selector": "##"}))
    assert out["error_type"] == "bad_selector", out
    # An editable password field is refused by selector too, so a filled password never reaches a file.
    before = len(frames)
    raw = in_turn({"action": "capture_secret", "name": "X_API_KEY", "destination": "env", "selector": "#pw"})
    out = json.loads(raw)
    assert out["error_type"] == "password_field" and FILLED not in raw, out
    assert FILLED not in env_file() and len(frames) == before, "nothing stored or asked"

    # Nothing key-like.
    exec_(f"goto_url({origin + '/none'!r}); wait_for_load()")
    out = json.loads(in_turn({"action": "capture_secret", "name": "X_API_KEY", "destination": "env"}))
    assert out["error_type"] == "no_secret_found", out

    from tools.vault_capture_secret import get_prompt_callback
    assert get_prompt_callback() is None
finally:
    SUPERVISOR_REGISTRY.stop_all()
    from tools.browser_session_lifecycle import cleanup_all_browsers
    cleanup_all_browsers()
    srv.shutdown()
# Importing tui_gateway.server points sys.stdout at stderr (it guards the stdio JSON-RPC channel).
sys.__stdout__.write("image ok\n")
sys.__stdout__.flush()
'''


def main() -> None:
    test_looks_like_secret()
    test_pick_candidates()
    test_names()
    test_parse_answer()
    test_schema()
    if failures:
        sys.exit(1)
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
