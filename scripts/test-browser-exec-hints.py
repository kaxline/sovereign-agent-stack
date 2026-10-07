#!/usr/bin/env python3
"""Checks for browser_exec result hints.

Host part is stdlib-only (no Hermes import). With --image (or when Docker and
the pinned Hermes image are present and --host-only is not given), the patch
is also applied inside nousresearch/hermes-agent and browser_exec is called
with the CLI subprocess stubbed, using the calls from session
20261007_030737_5f92c8.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_DIR = ROOT / "compose" / "hermes"
sys.path.insert(0, str(HERMES_DIR))

import browser_exec_hints as bh  # noqa: E402

DEFAULT_TAG = "v2026.9.14"

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
bu._run_cli_killing_process_group = lambda cmd, code, env, timeout: SimpleNamespace(**proc)

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
print("image ok")
'''


def run_image_checks(tag: str) -> None:
    import json

    image = f"nousresearch/hermes-agent:{tag}"
    cmd = [
        "docker", "run", "--rm", "-i", "--entrypoint", "/opt/hermes/.venv/bin/python",
        "-v", f"{HERMES_DIR}:/bootstrap:ro", image, "-c", IMAGE_DRIVER,
    ]
    payload = json.dumps({"import_code": SESSION_IMPORT_CODE, "import_stderr": SESSION_IMPORT_STDERR})
    out = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    if out.returncode != 0 or "image ok" not in out.stdout:
        fail(f"image checks ({image}):\n{out.stdout}{out.stderr}")
    print(f"image ok ({image})")


def image_available(tag: str) -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", f"nousresearch/hermes-agent:{tag}"],
        capture_output=True,
    )
    return probe.returncode == 0


def main() -> None:
    test_session_import()
    test_unrelated_missing_module()
    test_helper_names_from_other_module()
    test_session_bare_expression()
    test_printed_output()
    test_other_failure()
    print("ok")

    args = sys.argv[1:]
    if "--host-only" in args:
        return
    tag = os.environ.get("HERMES_AGENT_IMAGE_TAG", DEFAULT_TAG)
    if "--image" in args or image_available(tag):
        run_image_checks(tag)
    else:
        print(f"skip image checks (nousresearch/hermes-agent:{tag} not present)")


if __name__ == "__main__":
    main()
