#!/usr/bin/env python3
"""Turn heartbeat and one-session live status on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies gateway_liveness.py and the
shared gateway_overlay_kit.py into the agent package and appends one install
call to tui_gateway/server.py. Contracts, handlers and the usage-ticker wrap
are installed at runtime, not edited.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, SERVER_TAIL_ANCHOR, edit, fail, install, install_tail

AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"

MARKER = "# assistant-stack: gateway liveness"
TAG = "[patch-gateway-liveness]"

INSTALL = install_tail(MARKER, "gateway_liveness", "gateway liveness")


def main() -> None:
    install(BOOTSTRAP / "gateway_liveness.py", AGENT / "gateway_liveness.py", TAG)
    install(BOOTSTRAP / "gateway_overlay_kit.py", AGENT / "gateway_overlay_kit.py", TAG)
    if not (GATEWAY / "contracts" / "liveness.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/liveness.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[SERVER_TAIL_ANCHOR, "def _start_usage_ticker("])


if __name__ == "__main__":
    main()
