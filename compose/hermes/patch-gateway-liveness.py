#!/usr/bin/env python3
"""Turn heartbeat and one-session live status on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies gateway_liveness.py into the agent
package and appends one install call to tui_gateway/server.py. Contracts,
handlers and the usage-ticker wrap are installed at runtime, not edited.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, edit, fail, install

AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"
BOOTSTRAP = Path(os.environ.get("GATEWAY_LIVENESS_BOOTSTRAP", "/bootstrap"))

MARKER = "# assistant-stack: gateway liveness"
TAG = "[patch-gateway-liveness]"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{MARKER}
try:
    from agent.gateway_liveness import install as _gateway_liveness_install
    _gateway_liveness_install(sys.modules[__name__])
except Exception:
    logger.exception("gateway liveness overlay failed to install")
'''


def main() -> None:
    install(BOOTSTRAP / "gateway_liveness.py", AGENT / "gateway_liveness.py", TAG)
    if not (GATEWAY / "contracts" / "liveness.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/liveness.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[ANCHOR, "def _start_usage_ticker("])


if __name__ == "__main__":
    main()
