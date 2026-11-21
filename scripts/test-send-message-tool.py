#!/usr/bin/env python3
"""Checks for patch-send-message-tool.py in the pinned Hermes image.

The patch skips softly when an anchor moves, so a Hermes bump can silently drop
the agent-callable send_message tool. With the image present (or --image), the
patch is applied twice: the first run must patch all three parts, the second
must find them applied. Then send_message must be registered, sit in the
hermes-cli toolset, and Signal must be synthesized from SIGNAL_* env, with a profile's secret
scope winning over the process env. patch-cron-scheduler-delivery.py must build cron's Signal
config through the same signal_env module.
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
    # patch-ensure-send-message-tools.py exits non-zero when a model_tools.py anchor moves.
    out = subprocess.run([sys.executable, "/bootstrap/patch-ensure-send-message-tools.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL ensure patch: {out.stdout}{out.stderr}")
    ensure_want = "[patch-ensure-send-message-tools] " + ("patched" if want == "patched" else "already applied")
    assert out.stdout.strip().startswith(ensure_want) and len(out.stdout.strip().splitlines()) == 1, out.stdout

os.environ.update(HERMES_HOME="/tmp/hermes-home", SIGNAL_HTTP_URL="http://signal.example:8080",
                  SIGNAL_ACCOUNT="+15550100", SIGNAL_HOME_CHANNEL="+15550101")
os.makedirs("/tmp/hermes-home", exist_ok=True)
sys.path.insert(0, "/opt/hermes")
try:
    from tools import send_message_tool as smt
except ImportError:  # v0.23.2 moved it
    from gateway.outbound import send as smt
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

# Cron delivery builds the same config through the same module (AS-0036): the
# patch applies, the patched module still imports, and the inserted block calls
# signal_env rather than reading os.environ itself.
for want in ("patched", "already patched"):
    out = subprocess.run([sys.executable, "/bootstrap/patch-cron-scheduler-delivery.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL cron patch: {out.stdout}{out.stderr}")
    synth = [line for line in out.stdout.splitlines() if "(synth)" in line]
    assert len(synth) == 1 and synth[0].startswith(want), out.stdout
from pathlib import Path
cron_path = next(p for p in (Path("/opt/hermes/cron/scheduler_delivery.py"), Path("/opt/hermes/cron/scheduler.py"))
                 if p.is_file())
cron_src = cron_path.read_text(encoding="utf-8")
assert "from agent.signal_env import signal_env_pconfig" in cron_src
assert 'os.getenv("SIGNAL_' not in cron_src, "cron must not read SIGNAL_* past the profile scope"
import importlib
importlib.import_module(f"cron.{cron_path.stem}")

from agent.signal_env import signal_env_pconfig
token = set_secret_scope({"SIGNAL_HTTP_URL": "http://signal-b.example:8080", "SIGNAL_ACCOUNT": "+15550102",
                          "SIGNAL_HOME_CHANNEL": "+15550103", "SIGNAL_HOME_CHANNEL_NAME": "Ops"})
try:
    cfg = signal_env_pconfig()
finally:
    reset_secret_scope(token)
assert cfg.extra["account"] == "+15550102", cfg.extra
assert (cfg.home_channel.chat_id, cfg.home_channel.name) == ("+15550103", "Ops"), cfg.home_channel
os.environ.pop("SIGNAL_ACCOUNT")
assert signal_env_pconfig() is None, "no account, no config"
os.write(1, b"image ok\n")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
