#!/usr/bin/env python3
"""Client context and per-turn profile check on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies client_context.py into the agent
package and appends one install call to tui_gateway/server.py. The contract
fields (v2026.9+ refuses unknown keys with 4000), handler wraps and
capability flags are installed at runtime, not edited.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, edit, fail, install

AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"
BOOTSTRAP = Path(os.environ.get("CLIENT_CONTEXT_BOOTSTRAP", "/bootstrap"))

MARKER = "# assistant-stack: client context"
TAG = "[patch-client-context]"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{MARKER}
try:
    from agent.client_context import install as _client_context_install
    _client_context_install(sys.modules[__name__])
except Exception:
    logger.exception("client context overlay failed to install")
'''


def main() -> None:
    install(BOOTSTRAP / "client_context.py", AGENT / "client_context.py", TAG)
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/registry.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[ANCHOR])


if __name__ == "__main__":
    main()
