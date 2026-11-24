#!/usr/bin/env python3
"""Speaker names on assistant rows on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies turn_speaker.py and the shared
gateway_overlay_kit.py into the agent package and appends one install call to
tui_gateway/server.py. The contract fields (v2026.9+ refuses unknown keys with
4000), handler wraps and capability flags are installed at runtime, not
edited.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, SERVER_TAIL_ANCHOR, edit, fail, install, install_tail

AGENT = ROOT / "agent"
GATEWAY = ROOT / "tui_gateway"

MARKER = "# assistant-stack: turn speaker"
TAG = "[patch-turn-speaker]"

INSTALL = install_tail(MARKER, "turn_speaker", "turn speaker")


def main() -> None:
    install(BOOTSTRAP / "turn_speaker.py", AGENT / "turn_speaker.py", TAG)
    install(BOOTSTRAP / "gateway_overlay_kit.py", AGENT / "gateway_overlay_kit.py", TAG)
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/registry.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[SERVER_TAIL_ANCHOR])


if __name__ == "__main__":
    main()
