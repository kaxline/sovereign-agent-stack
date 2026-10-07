#!/usr/bin/env python3
"""Checks for browser_exec result hints, expression echo and the vault save next step.

Host part is stdlib-only (no Hermes import). With --image (or when Docker and
the pinned Hermes image are present and --host-only is not given), the patch
is also applied inside nousresearch/hermes-agent and browser_exec is called
with the CLI subprocess stubbed, using the calls from session
20261007_030737_5f92c8.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(HERMES_DIR))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import browser_exec_hints as bh  # noqa: E402
import hermes_image  # noqa: E402

# Calls and stderr from the LinkedIn login session (20261007_030737_5f92c8).
SESSION_IMPORT_CODE = """# Navigate to LinkedIn and check vault for saved logins
from browser_helpers import new_tab, goto_url, wait_for_load, page_info, browser_vault_list

new_tab('https://www.linkedin.com')
wait_for_load()
print(page_info())
"""
SESSION_IMPORT_STDERR = """Traceback (most recent call last):
  File "/opt/data/home/.cache/uv/archive-v0/lc5LMWjskVPinukw964pY/lib/python3.13/site-packages/browser_harness/run.py", line 406, in _run
    exec(code, globals())
  File "<string>", line 2, in <module>
ModuleNotFoundError: No module named 'browser_helpers'"""
SESSION_BARE_CODE = "page_info()"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def test_session_import() -> None:
    hint = bh.result_hint(SESSION_IMPORT_CODE, 1, "", SESSION_IMPORT_STDERR)
    expect(hint is not None and "'browser_helpers'" in hint, f"import hint: {hint!r}")
    expect("already defined" in hint, "hint should say the helpers are defined")


def test_unrelated_missing_module() -> None:
    stderr = "ModuleNotFoundError: No module named 'pandas'"
    expect(
        bh.result_hint("import pandas as pd\n", 1, "", stderr) is None,
        "a real third-party import should get no helper hint",
    )


def test_helper_names_from_other_module() -> None:
    stderr = "ModuleNotFoundError: No module named 'bh'"
    expect(
        bh.result_hint("from bh import (new_tab,\n", 1, "", stderr) is not None,
        "helper names imported from any missing module should get the hint",
    )


def test_session_bare_expression() -> None:
    hint = bh.result_hint(SESSION_BARE_CODE, 0, "", "")
    expect(hint == bh.NO_OUTPUT_HINT, f"no-output hint: {hint!r}")


def test_printed_output() -> None:
    expect(
        bh.result_hint("print(page_info())", 0, "url: https://www.linkedin.com/login\n", "") is None,
        "printed output needs no hint",
    )


def test_other_failure() -> None:
    expect(
        bh.result_hint("js('x')", 1, "", "RuntimeError: CDP disconnected") is None,
        "other failures get no hint",
    )


def run_echo(code: str, **helpers) -> str:
    """Execute echoed code with stub helpers; return what it printed."""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(bh.echo_last_expression(code), dict(helpers))
    return out.getvalue()


def test_echo_session_js() -> None:
    # Session 20261007_041748_5394c0 ended several calls on an unprinted js(...).
    code = "# Let's try a simpler JS approach to find the form\njs(\"document.title\")"
    expect(
        run_echo(code, js=lambda e: "LinkedIn Login") == "LinkedIn Login\n",
        "a trailing js(...) should print its value",
    )
    expect(
        bh.echo_last_expression(code).startswith("# Let's try"),
        "the step-label comment stays first",
    )


def test_echo_multiline() -> None:
    code = "inputs = 2\njs(\n    'x'  # comment\n)\n# trailing comment\n"
    expect(run_echo(code, js=lambda e: [e] * 2) == "['x', 'x']\n", "multi-line call echoes")


def test_echo_none_is_silent() -> None:
    expect(run_echo("fill_input('#a', 'b')", fill_input=lambda s, v: None) == "", "None prints nothing")


def test_echo_leaves_code_alone() -> None:
    for code in (
        "print(page_info())",
        "x = js('1')",
        "for i in range(2):\n    js('x')",
        "a = 1; js('x')",
        "def f(:\n",
        "'just a string'",
        "",
    ):
        expect(bh.echo_last_expression(code) == code, f"unchanged: {code!r}")


def test_save_next() -> None:
    expect(bh.save_login_next("vault_x", True) == bh.SAVE_FILLED_NEXT, "filled keeps upstream text")
    text = bh.save_login_next("vault_b47b899a7fe7", False)
    expect("browser_vault_fill" in text and "'vault_b47b899a7fe7'" in text, f"unfilled next: {text}")
    expect("Never ask the user for the password" in text, "unfilled next forbids asking")


# Runs inside the Hermes image with compose/hermes mounted at /bootstrap.
IMAGE_DRIVER = r'''
import json
import subprocess
import sys
from types import SimpleNamespace

def run(script):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
    return out.stdout

first = run("/bootstrap/patch-browser-exec-hint.py")
assert "patched: result hints" in first, first
assert "patched: EACCES hint" in first, first
assert "patched: expression echo" in first, first
assert "patched: vault save next step" in first, first
again = run("/bootstrap/patch-browser-exec-hint.py")
assert "ok: result hints already patched" in again, again
assert "patched" not in again.replace("already patched", ""), again

sys.path.insert(0, "/opt/hermes")
import tools.browser_use_cli as bu

proc = {}
bu._find_cli = lambda: ["browser-use"]
bu._base_subprocess_env = lambda: {}
bu._route_backend = lambda *a, **k: None
bu._attach_vault_supervisor = lambda *a, **k: None
bu._workspace_dir = lambda task_id: None
bu._find_screenshot = lambda *a, **k: None
sent = {}
def fake_cli(cmd, code, env, timeout):
    sent["code"] = code
    return SimpleNamespace(**proc)
bu._run_cli_killing_process_group = fake_cli

def call(code, returncode, stdout, stderr):
    proc.update(returncode=returncode, stdout=stdout, stderr=stderr)
    return json.loads(bu.browser_exec(code, task_id="t"))

cases = json.loads(sys.stdin.read())
r = call(cases["import_code"], 1, "", cases["import_stderr"])
assert "'browser_helpers'" in r.get("hint", ""), r
assert r["stderr"].endswith("No module named 'browser_helpers'"), r
r = call("page_info()", 0, "", "")
assert "print(" in r.get("hint", ""), r
r = call("print(page_info())", 0, "url: https://www.linkedin.com/login\n", "")
assert "hint" not in r, r
assert sent["code"] == "print(page_info())", sent
call('js("document.title")', 0, "LinkedIn Login\n", "")
assert "print(" in sent["code"] and 'js("document.title")' in sent["code"], sent

# browser_vault_save_login on a page with no form: the session's case.
import agent.vault_backends.unlock as unlock
import agent.vault_store as store
import tools.browser_vault_tool as vt
vt._focus_bound_origin = lambda *a, **k: None
vt._current_page_origin = lambda task_id: "https://www.linkedin.com"
unlock.can_prompt_here = lambda: True
unlock.get_save_login_prompt_callback = lambda: (lambda origin, host: {"identifier": "keith@axline.io", "password": "x"})
store.get_vault_store = lambda: SimpleNamespace(add_item=lambda *a, **k: SimpleNamespace(id="vault_b47b899a7fe7"))
for fill_ok in (False, True):
    vt.browser_vault_fill = lambda handle, task_id=None, ok=fill_ok: json.dumps(
        {"success": ok} if ok else {"success": False, "error": "No login form fields were found on the current page."})
    r = json.loads(vt.browser_vault_save_login("LinkedIn", task_id="t"))
    assert r["success"] and r["handle"] == "vault_b47b899a7fe7", r
    if fill_ok:
        assert r["next"].startswith("Type the identifier"), r
    else:
        assert "browser_vault_fill" in r["next"] and "vault_b47b899a7fe7" in r["next"], r
print("image ok")
'''


def main() -> None:
    test_session_import()
    test_unrelated_missing_module()
    test_helper_names_from_other_module()
    test_session_bare_expression()
    test_printed_output()
    test_other_failure()
    test_echo_session_js()
    test_echo_multiline()
    test_echo_none_is_silent()
    test_echo_leaves_code_alone()
    test_save_next()
    print("ok")

    payload = json.dumps({"import_code": SESSION_IMPORT_CODE, "import_stderr": SESSION_IMPORT_STDERR})
    hermes_image.maybe_run(IMAGE_DRIVER, stdin=payload)

if __name__ == "__main__":
    main()
