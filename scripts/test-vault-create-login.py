#!/usr/bin/env python3
"""Checks for browser_vault action=create_login (CR 0024 R1).

Host part is stdlib-only: password policy, generator, answer parsing and field selection. With
--image (or when Docker and the pinned Hermes image are present and --host-only is not given), the
patch is applied inside nousresearch/hermes-agent and the action runs end to end in the image's
Chromium against local fixture pages, through browser_vault's dispatcher and the session prompt scope.
"""

from __future__ import annotations

import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
import vault_create_login as vcl  # noqa: E402

failures = 0


def expect(cond: bool, msg: str) -> None:
    global failures
    if not cond:
        failures += 1
        print(f"FAIL: {msg}")


def has_each(password: str, sets: list[str]) -> bool:
    return all(any(c in s for c in password) for s in sets)


def test_default_policy() -> None:
    policy = vcl.derive_policy([{}])
    expect(policy.length == 24, f"default length {policy.length}")
    for _ in range(50):
        pw = vcl.generate_password(policy)
        expect(len(pw) == 24 and has_each(pw, [vcl.LOWER, vcl.UPPER, vcl.DIGIT, vcl.DEFAULT_SYMBOLS]), pw)
        expect(set(pw) <= set(vcl.LOWER + vcl.UPPER + vcl.DIGIT + vcl.DEFAULT_SYMBOLS), f"charset {pw}")


def test_length_limits() -> None:
    expect(vcl.derive_policy([{"maxLength": 16}, {}]).length == 16, "maxlength 16")
    expect(vcl.derive_policy([{"minLength": 32}]).length == 32, "minlength 32")
    expect(vcl.derive_policy([{"minLength": 20, "maxLength": 12}]).length == 12, "conflict uses maxlength")
    expect(vcl.derive_policy([{"maxLength": 16}, {"maxLength": 20}]).length == 16, "smallest maxlength wins")
    expect(vcl.derive_policy([{}], cap=12).length == 12, "retry cap")
    expect(vcl.derive_policy([{"minLength": 16}], cap=12).length == 16, "cap keeps minlength")


def test_passwordrules() -> None:
    rules = vcl.parse_passwordrules(
        "minlength: 10; maxlength: 14; required: lower; required: upper; required: digit; "
        "required: [-().&@?'#,/+]; max-consecutive: 2; unknown: x")
    expect(rules.minlength == 10 and rules.maxlength == 14 and rules.max_consecutive == 2, f"numbers {rules}")
    expect(len(rules.required) == 4 and rules.required[3] == "-().&@?'#,/+", f"required {rules.required}")
    policy = vcl.derive_policy([{"passwordrules": "minlength: 10; maxlength: 14; required: lower; required: upper; "
                                                  "required: digit; required: [-().&@?'#,/+]; max-consecutive: 2"}])
    expect(policy.length == 14, f"rules length {policy.length}")
    for _ in range(50):
        pw = vcl.generate_password(policy)
        expect(len(pw) == 14 and has_each(pw, [vcl.LOWER, vcl.UPPER, vcl.DIGIT, "-().&@?'#,/+"]), pw)
        expect(not vcl._too_consecutive(pw, 2), f"consecutive {pw}")
        expect(set(pw) <= set(vcl.LOWER + vcl.UPPER + vcl.DIGIT + "-().&@?'#,/+"), f"allowed {pw}")
    digits = vcl.derive_policy([{"passwordrules": "required: digit; minlength: 6; maxlength: 6"}])
    expect(digits.length == 6 and set(vcl.generate_password(digits)) <= set(string.digits), "digits only")
    allowed = vcl.derive_policy([{"passwordrules": "allowed: lower, upper"}])
    expect(set(vcl.generate_password(allowed)) <= set(vcl.LOWER + vcl.UPPER), "allowed only")


def test_alnum_fallback() -> None:
    policy = vcl.derive_policy([{}], symbols=False)
    for _ in range(20):
        pw = vcl.generate_password(policy)
        expect(pw.isalnum() and has_each(pw, [vcl.LOWER, vcl.UPPER, vcl.DIGIT]), pw)
    # Rules that require a symbol keep it even on the fallback.
    policy = vcl.derive_policy([{"passwordrules": "required: lower; required: [!]"}], symbols=False)
    expect("!" in vcl.generate_password(policy), "required symbol kept")


def test_parse_answer() -> None:
    expect(vcl.parse_answer('{"generate": true}') == {"generate": True}, "generate json")
    expect(vcl.parse_answer("generate") == {"generate": True}, "generate word")
    expect(vcl.parse_answer('{"password": "typed-Pass1"}') == {"password": "typed-Pass1"}, "typed")
    for raw in ("", None, "{}", '{"generate": "yes"}', '{"password": ""}', "[1]", "not json"):
        expect(vcl.parse_answer(raw) is None, f"decline {raw!r}")


def control(index, autocomplete="", form=0, name="", label=""):
    return {"index": index, "type": "password", "autocomplete": autocomplete, "formIndex": form,
            "name": name, "label": label}


def test_select_fields() -> None:
    pick = lambda cs: [c["index"] for c in vcl.select_new_password_fields(cs)]  # noqa: E731
    expect(pick([control(1, "new-password"), control(2, "new-password")]) == [1, 2], "two marked")
    expect(pick([control(1, "current-password"), control(2, "new-password"), control(3, "new-password")]) == [2, 3],
           "change-password form skips current")
    expect(pick([control(1, label="Create password"), control(2, label="Confirm password")]) == [1, 2], "unmarked")
    expect(pick([control(1, name="old_password"), control(2, label="New password")]) == [2], "labelled current")
    expect(pick([control(1, "current-password")]) == [], "sign-in only")
    expect(pick([control(1, "current-password", form=0), control(2, form=1), control(3, form=1)]) == [2, 3],
           "sign-up form beside a sign-in form")
    expect(pick([control(1, form=0), control(2, form=1)]) == [1], "first form only")
    expect(pick([control(1, "one-time-code")]) == [], "otp")


def test_schema() -> None:
    schema = {"description": "Vault.", "parameters": {"properties": {
        "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock"]},
        "handle": {"type": "string", "description": "fill / enter_code: a handle from list."},
        "label": {"type": "string", "description": "save_login only: short site name (default: the host)."}}}}
    vcl.extend_schema(schema)
    vcl.extend_schema(schema)
    props = schema["parameters"]["properties"]
    expect(props["action"]["enum"].count("create_login") == 1, "enum once")
    expect("identifier" in props and "create_login" in props["handle"]["description"], "props")
    expect(props["label"]["description"].startswith("save_login / create_login"), props["label"])
    expect(schema["description"].count("create_login (") == 1, "description once")


# Runs inside the Hermes image with compose/hermes mounted at /bootstrap.
IMAGE_DRIVER = r'''
import contextlib, http.server, json, os, subprocess, sys, tempfile, threading
from types import SimpleNamespace

def run(script):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
    return out.stdout

first = run("/bootstrap/patch-vault-create-login.py")
for part in ("create_login schema", "create_login dispatch", "create_login prompt", "create_login contract"):
    assert f"patched: {part}" in first, first
again = run("/bootstrap/patch-vault-create-login.py")
assert "patched" not in again.replace("already patched", ""), again

os.chdir("/opt/hermes")
sys.path.insert(0, "/opt/hermes")
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="vault-create-login-")

from tui_gateway.contracts import SERVER_REQUESTS
assert "vault.create_login" in SERVER_REQUESTS, sorted(SERVER_REQUESTS)

import tools.browser_vault_tool as bvt
props = bvt.BROWSER_VAULT_SCHEMA["parameters"]["properties"]
assert "create_login" in props["action"]["enum"] and "identifier" in props, props
from tools.registry import registry
assert registry.get_schema("browser_vault")["parameters"]["properties"]["action"]["enum"][-1] == "create_login"

PAGES = {
    "/signup": b"""<!doctype html><title>signup</title>
<form><input name=email type=email autocomplete=email><input name=name autocomplete=name>
<input name=pw type=password autocomplete=new-password maxlength=16>
<input name=pw2 type=password autocomplete=new-password><input type=submit></form>""",
    "/pattern": b"""<!doctype html><title>pattern</title>
<form><input name=email type=email><input name=pw type=password placeholder="Create password" pattern="[A-Za-z0-9]{12,20}">
<input name=pw2 type=password placeholder="Repeat password"></form>""",
    "/login": b"""<!doctype html><title>login</title>
<form><input name=email type=email autocomplete=username><input name=pw type=password autocomplete=current-password></form>""",
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
TASK = "create-login-e2e"

from tools import browser_use_cli as bu
from tools.browser_supervisor import SUPERVISOR_REGISTRY
from agent.vault_store import get_vault_store
from agent.vault_backends import unlock
import gateway.run_turn_progress as rtp
import gateway.session_server_requests as ssr
from gateway.session_vault_prompt import vault_prompt_scope

def exec_(code):
    out = json.loads(bu.browser_exec(code, task_id=TASK, timeout_s=90))
    assert out.get("success"), out
    return out

def dom(expr):
    return SUPERVISOR_REGISTRY.get(TASK).evaluate_runtime(expr)["result"]

def vault(args):
    return bvt._handle_vault(args, task_id=TASK)

# The session authority's viewer: answers the frame and records it.
frames = []
answer = {"value": '{"generate": true}'}
def fake_ask_viewer(events, session_id, method, params, **kw):
    frames.append((method, dict(params)))
    return answer["value"]
ssr.ask_viewer = fake_ask_viewer
rtp.local_viewer_turn = lambda turn: True
authority = SimpleNamespace(sessions={"s1": SimpleNamespace(event_stream=object())})
turn = SimpleNamespace(_approval_owner=(authority, "s1", 1))
agent = SimpleNamespace(_interrupt_requested=False)

def in_turn(args):
    with vault_prompt_scope(turn, agent):
        return vault(args)

store = get_vault_store()
try:
    exec_(f"new_tab({origin + '/signup'!r}); wait_for_load()")

    # No viewer that can answer: prompt_unavailable, nothing stored.
    out = json.loads(vault({"action": "create_login", "identifier": "new@example.com"}))
    assert out["error_type"] == "prompt_unavailable", out
    assert not store.list_items()

    # Declined card: vault unchanged, fields empty.
    answer["value"] = ""
    out = json.loads(in_turn({"action": "create_login", "identifier": "new@example.com"}))
    assert out["error_type"] == "save_declined", out
    assert not store.list_items() and dom("document.querySelector('[name=pw]').value") == ""
    assert frames[-1] == ("vault.create_login", {"origin": origin, "site": origin.split("://")[1],
                                                 "identifier": "new@example.com", "regenerate": False}), frames

    # Approved: both new-password fields get the same 16-character password, one item, no password in the result.
    answer["value"] = '{"generate": true}'
    raw = in_turn({"action": "create_login", "identifier": "new@example.com", "label": "Example"})
    out = json.loads(raw)
    assert out["success"] and out["fill"]["filled_fields"] == 2 and out["origin"] == origin, out
    pw, pw2 = dom("document.querySelector('[name=pw]').value"), dom("document.querySelector('[name=pw2]').value")
    assert pw == pw2 and len(pw) == 16, (len(pw), pw == pw2)
    assert pw not in raw
    items = store.list_items()
    assert len(items) == 1 and items[0].origin == origin and items[0].identifier == "new@example.com", items
    assert store.resolve_secret(out["handle"])["password"] == pw
    assert dom("document.querySelector('[name=email]').value") == "", "identifier is typed by the agent, not the tool"
    assert dom("document.querySelectorAll('[data-hermes-signup-slot]').length") == 0, "stamps removed"

    # The agent reads the field afterwards: browser_exec output is redacted.
    read = json.dumps(exec_("print(js(\"document.querySelector('[name=pw]').value\"))"))
    assert pw not in read and "redacted" in read, read

    # Regenerate with the handle: same item, new password in both fields.
    raw = in_turn({"action": "create_login", "handle": out["handle"]})
    again = json.loads(raw)
    assert again["success"] and again["regenerated"] and again["handle"] == out["handle"], again
    new_pw = dom("document.querySelector('[name=pw]').value")
    assert new_pw != pw and new_pw == dom("document.querySelector('[name=pw2]').value") and len(new_pw) == 16
    assert len(store.list_items()) == 1 and store.resolve_secret(out["handle"])["password"] == new_pw
    assert frames[-1][1]["regenerate"] is True
    assert new_pw not in raw

    # A password the user typed in the card.
    store.remove_item(out["handle"])
    answer["value"] = json.dumps({"password": "Typed-Pass-0423"})
    raw = in_turn({"action": "create_login", "identifier": "typed@example.com"})
    out = json.loads(raw)
    assert out["success"] and dom("document.querySelector('[name=pw2]').value") == "Typed-Pass-0423", out
    assert "Typed-Pass-0423" not in raw
    store.remove_item(out["handle"])

    # Unmarked fields with a pattern that refuses symbols: the generator falls back to letters and digits.
    answer["value"] = '{"generate": true}'
    exec_(f"goto_url({origin + '/pattern'!r}); wait_for_load()")
    out = json.loads(in_turn({"action": "create_login", "identifier": "p@example.com"}))
    assert out["success"] and out["fill"]["filled_fields"] == 2, out
    v = dom("document.querySelector('[name=pw]').value")
    assert v.isalnum() and 12 <= len(v) <= 20 and v == dom("document.querySelector('[name=pw2]').value"), len(v)
    store.remove_item(out["handle"])

    # A typed password the pattern refuses: nothing written or stored.
    exec_(f"goto_url({origin + '/pattern'!r}); wait_for_load()")
    answer["value"] = json.dumps({"password": "no spaces!"})
    out = json.loads(in_turn({"action": "create_login", "identifier": "p@example.com"}))
    assert out["error_type"] == "password_rules_unmet", out
    assert not store.list_items() and dom("document.querySelector('[name=pw]').value") == ""

    # A sign-in form: refused before the user is asked.
    exec_(f"goto_url({origin + '/login'!r}); wait_for_load()")
    asked = len(frames)
    out = json.loads(in_turn({"action": "create_login", "identifier": "x@example.com"}))
    assert out["error_type"] == "no_new_password_field" and len(frames) == asked, out
    out = json.loads(in_turn({"action": "create_login"}))
    assert out["error_type"] == "identifier_required", out

    # Outside the turn the callback is gone again.
    from tools.vault_create_login import get_prompt_callback
    assert get_prompt_callback() is None
finally:
    SUPERVISOR_REGISTRY.stop_all()
    from tools.browser_session_lifecycle import cleanup_all_browsers
    cleanup_all_browsers()
    srv.shutdown()
print("image ok")
'''


def main() -> None:
    test_default_policy()
    test_length_limits()
    test_passwordrules()
    test_alnum_fallback()
    test_parse_answer()
    test_select_fields()
    test_schema()
    if failures:
        sys.exit(1)
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
