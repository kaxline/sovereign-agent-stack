#!/usr/bin/env python3
"""Client context and per-turn profile check on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies client_context.py into the agent
package and appends one install call to tui_gateway/server.py. The contract
fields (v2026.9+ refuses unknown keys with 4000), handler wraps and
capability flags are installed at runtime, not edited.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, SERVER_TAIL_ANCHOR, edit, fail, install, install_tail

AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"

MARKER = "# assistant-stack: client context"
TAG = "[patch-client-context]"

INSTALL = install_tail(MARKER, "client_context", "client context")


def main() -> None:
    install(BOOTSTRAP / "client_context.py", AGENT / "client_context.py", TAG)
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/registry.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[SERVER_TAIL_ANCHOR])


if __name__ == "__main__":
    main()
