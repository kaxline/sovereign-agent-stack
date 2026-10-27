#!/usr/bin/env python3
"""Speaker names on assistant rows on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies turn_speaker.py into the agent
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
BOOTSTRAP = Path(os.environ.get("TURN_SPEAKER_BOOTSTRAP", "/bootstrap"))

TAG = "# assistant-stack: turn speaker"
ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

INSTALL = f'''

{TAG}
try:
    from agent.turn_speaker import install as _turn_speaker_install
    _turn_speaker_install(sys.modules[__name__])
except Exception:
    logger.exception("turn speaker overlay failed to install")
'''


def main() -> None:
    src = BOOTSTRAP / "turn_speaker.py"
    if not src.is_file():
        raise SystemExit(f"[patch-turn-speaker] missing {src}")
    shutil.copyfile(src, AGENT / "turn_speaker.py")
    print(f"[patch-turn-speaker] installed {AGENT / 'turn_speaker.py'}")

    if not (GATEWAY / "contracts" / "registry.py").is_file():
        raise SystemExit("[patch-turn-speaker] pre-v2026.9 gateway (no contracts); not supported")
    server = GATEWAY / "server.py"
    text = server.read_text()
    if TAG in text:
        print("[patch-turn-speaker] already applied (server install)")
    else:
        if ANCHOR not in text:
            raise SystemExit(
                "[patch-turn-speaker] split-module registration loop missing in server.py. "
                "Hermes image may have changed — update this patch."
            )
        server.write_text(text.rstrip("\n") + "\n" + INSTALL)
        print("[patch-turn-speaker] patched server install")


if __name__ == "__main__":
    main()
