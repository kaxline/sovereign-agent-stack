#!/usr/bin/env python3
"""Checks for patch-send-message-tool.py in the pinned Hermes image.

The patch skips softly when an anchor moves, so a Hermes bump can silently drop
the agent-callable send_message tool. With the image present (or --image), the
patch is applied twice: the first run must patch all three parts, the second
must find them applied. Then send_message must be registered, sit in the
hermes-cli toolset, and Signal must be synthesized from SIGNAL_* env, with a profile's secret
scope winning over the process env.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import os
import subprocess
import sys

for want in ("patched", "already patched"):
    out = subprocess.run([sys.executable, "/bootstrap/patch-send-message-tool.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    lines = out.stdout.strip().splitlines()
    assert len(lines) == 3 and all(line.startswith(want) for line in lines), out.stdout

os.environ.update(HERMES_HOME="/tmp/hermes-home", SIGNAL_HTTP_URL="http://signal.example:8080",
                  SIGNAL_ACCOUNT="+15550100", SIGNAL_HOME_CHANNEL="+15550101")
os.makedirs("/tmp/hermes-home", exist_ok=True)
sys.path.insert(0, "/opt/hermes")
from tools import send_message_tool as smt
from tools.registry import registry
import toolsets

entry = registry.get_entry("send_message")
assert entry is not None and entry.toolset == "send_message", entry
assert "target='signal'" in entry.schema["description"], "the schema tells the model Signal works"
assert "send_message" in toolsets.resolve_toolset("hermes-cli"), "core toolsets include send_message"

# api_server runs keep only configurable toolsets when they expand hermes-cli,
# so send_message reaches a WebUI turn only when the bootstrap names it.
from hermes_cli.tools_config import _get_platform_tools
plain = _get_platform_tools({"platform_toolsets": {"api_server": ["hermes-cli"]}}, "api_server",
                            include_default_mcp_servers=False)
named = _get_platform_tools({"platform_toolsets": {"api_server": ["hermes-cli", "send_message"]}}, "api_server",
                            include_default_mcp_servers=False)
assert "send_message" in named, sorted(named)
if "send_message" in plain:
    print("note: hermes-cli keeps send_message on api_server again; the bootstrap entry is redundant")

pconfig = smt._signal_env_pconfig()
assert pconfig.extra == {"http_url": "http://signal.example:8080", "account": "+15550100"}, pconfig.extra
assert pconfig.home_channel.chat_id == "+15550101", pconfig.home_channel

# A profile's own .env wins over the process env, so a multiplexed gateway
# never hands one profile's turn the launch profile's Signal account.
from agent.secret_scope import reset_secret_scope, set_secret_scope
token = set_secret_scope({"SIGNAL_HTTP_URL": "http://signal-b.example:8080", "SIGNAL_ACCOUNT": "+15550102"})
try:
    scoped = smt._signal_env_pconfig()
finally:
    reset_secret_scope(token)
assert scoped.extra == {"http_url": "http://signal-b.example:8080", "account": "+15550102"}, scoped.extra
os.write(1, b"image ok\n")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
