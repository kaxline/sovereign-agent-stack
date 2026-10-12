#!/usr/bin/env python3
"""Client context and per-turn profile check on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies client_context.py into the agent
package and appends one install call to tui_gateway/server.py. The contract
fields (v2026.9+ refuses unknown keys with 4000), handler wraps and
capability flags are installed at runtime, not edited.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"
BOOTSTRAP = Path(os.environ.get("CLIENT_CONTEXT_BOOTSTRAP", "/bootstrap"))

TAG = "# assistant-stack: client context"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{TAG}
try:
    from agent.client_context import install as _client_context_install
    _client_context_install(sys.modules[__name__])
except Exception:
    logger.exception("client context overlay failed to install")
'''


def main() -> None:
    src = BOOTSTRAP / "client_context.py"
    if not src.is_file():
        raise SystemExit(f"[patch-client-context] missing {src}")
    shutil.copyfile(src, AGENT / "client_context.py")
    print(f"[patch-client-context] installed {AGENT / 'client_context.py'}")

    if not (GATEWAY / "contracts" / "registry.py").is_file():
        raise SystemExit("[patch-client-context] pre-v2026.9 gateway (no contracts); not supported")
    server = GATEWAY / "server.py"
    text = server.read_text()
    if TAG in text:
        print("[patch-client-context] already applied (server install)")
    else:
        if ANCHOR not in text:
            raise SystemExit(
                "[patch-client-context] split-module registration loop missing in server.py. "
                "Hermes image may have changed — update this patch."
            )
        server.write_text(text.rstrip("\n") + "\n" + INSTALL)
        print("[patch-client-context] patched server install")


if __name__ == "__main__":
    main()
