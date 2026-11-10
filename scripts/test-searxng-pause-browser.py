#!/usr/bin/env python3
"""Checks that a paused searxng MCP server points the model at browser search
(patch-searxng-pause-browser.py).

Host part is stdlib-only. With --image (or when Docker and the pinned Hermes
image are present and --host-only is not given), the overlay is applied inside
nousresearch/hermes-agent and the MCP circuit breaker is opened by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import json
import subprocess
import sys
import time

out = subprocess.run([sys.executable, "/bootstrap/patch-searxng-pause-browser.py"], capture_output=True, text=True)
assert "patched" in out.stdout and "not patched" not in out.stdout, out.stdout + out.stderr
again = subprocess.run([sys.executable, "/bootstrap/patch-searxng-pause-browser.py"], capture_output=True, text=True)
assert "already applied" in again.stdout, again.stdout + again.stderr

sys.path.insert(0, "/opt/hermes")
from tools import mcp_tool_handlers as h
from tools.mcp_tool_common import _core
from tools.mcp_tool_scope import _resolve_server_key


def open_breaker(name, application):
    key = _resolve_server_key(name)
    _core._server_error_counts[key] = _core._CIRCUIT_BREAKER_THRESHOLD
    _core._server_breaker_opened_at[key] = time.monotonic()
    _core._server_errors_all_application[key] = application


for application in (True, False):
    open_breaker("searxng", application)
    error = json.loads(h._check_circuit_breaker("searxng"))["error"]
    assert "Paused for" in error or "unreachable" in error, error
    assert "browser_exec" in error and "new_tab(" in error and "bing.com/search?q=" in error, error
    assert "Don't call searxng tools again" in error, error

    # Other MCP servers keep upstream's text.
    open_breaker("todos", application)
    other = json.loads(h._check_circuit_breaker("todos"))["error"]
    assert "browser_exec" not in other, other

# A closed breaker still lets calls through.
key = _resolve_server_key("searxng")
_core._server_error_counts[key] = 0
assert h._check_circuit_breaker("searxng") is None

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
