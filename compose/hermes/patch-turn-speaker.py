#!/usr/bin/env python3
"""Speaker names on assistant rows on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies turn_speaker.py into the agent
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
BOOTSTRAP = Path(os.environ.get("TURN_SPEAKER_BOOTSTRAP", "/bootstrap"))

MARKER = "# assistant-stack: turn speaker"
TAG = "[patch-turn-speaker]"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{MARKER}
try:
    from agent.turn_speaker import install as _turn_speaker_install
    _turn_speaker_install(sys.modules[__name__])
except Exception:
    logger.exception("turn speaker overlay failed to install")
'''


def main() -> None:
    install(BOOTSTRAP / "turn_speaker.py", AGENT / "turn_speaker.py", TAG)
    if not (GATEWAY / "contracts" / "registry.py").is_file():
        fail(TAG, "pre-v2026.9 gateway (no contracts/registry.py): not supported")
    edit(GATEWAY / "server.py", [], MARKER, TAG, label="server install", append=INSTALL,
         requires=[ANCHOR])


if __name__ == "__main__":
    main()
