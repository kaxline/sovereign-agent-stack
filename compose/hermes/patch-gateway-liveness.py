#!/usr/bin/env python3
"""Turn heartbeat and one-session live status on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies gateway_liveness.py into the agent
package and appends one install call to tui_gateway/server.py. Contracts,
handlers and the usage-ticker wrap are installed at runtime, not edited.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"
BOOTSTRAP = Path(os.environ.get("GATEWAY_LIVENESS_BOOTSTRAP", "/bootstrap"))

TAG = "# assistant-stack: gateway liveness"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{TAG}
try:
    from agent.gateway_liveness import install as _gateway_liveness_install
    _gateway_liveness_install(sys.modules[__name__])
except Exception:
    logger.exception("gateway liveness overlay failed to install")
'''


def main() -> None:
    src = BOOTSTRAP / "gateway_liveness.py"
    if not src.is_file():
        raise SystemExit(f"[patch-gateway-liveness] missing {src}")
    shutil.copyfile(src, AGENT / "gateway_liveness.py")
    print(f"[patch-gateway-liveness] installed {AGENT / 'gateway_liveness.py'}")

    if not (GATEWAY / "contracts" / "liveness.py").is_file():
        raise SystemExit("[patch-gateway-liveness] pre-v2026.9 gateway (no contracts); not supported")
    server = GATEWAY / "server.py"
    text = server.read_text()
    if TAG in text:
        print("[patch-gateway-liveness] already applied (server install)")
        return
    if ANCHOR not in text or "def _start_usage_ticker(" not in text:
        raise SystemExit(
            "[patch-gateway-liveness] split-module registration loop or _start_usage_ticker missing in "
            "server.py. Hermes image may have changed — update this patch."
        )
    server.write_text(text.rstrip("\n") + "\n" + INSTALL)
    print("[patch-gateway-liveness] patched server install")


if __name__ == "__main__":
    main()
