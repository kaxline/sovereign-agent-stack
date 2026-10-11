#!/usr/bin/env python3
"""Client context and per-turn profile check on the pinned Hermes gateway.

Compatibility overlay (not upstream). Copies client_context.py into the agent
package, declares ``context`` on the gateway wire contract (v2026.9+ refuses
unknown keys with 4000), and appends one install call to tui_gateway/server.py.
The handlers themselves are wrapped at runtime, not edited.
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
FIELD = f"    context: str | None = None  {TAG}\n"


def _insert(path: Path, before: str, addition: str, *, label: str, after: str = "") -> None:
    """Insert ``addition`` between ``before`` and ``after``, which must appear once, adjacent."""
    text = path.read_text()
    if before + addition + after in text:
        print(f"[patch-client-context] already applied ({label})")
        return
    anchor = before + after
    if text.count(anchor) != 1:
        raise SystemExit(
            f"[patch-client-context] expected one anchor for {label} in {path}, "
            f"found {text.count(anchor)}. Hermes image may have changed — update this patch."
        )
    path.write_text(text.replace(anchor, before + addition + after, 1))
    print(f"[patch-client-context] patched {label}")


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

    contracts = GATEWAY / "contracts"
    if not (contracts / "sessions.py").is_file():
        raise SystemExit("[patch-client-context] pre-v2026.9 gateway (no contracts); not supported")
    _insert(
        contracts / "sessions.py",
        "    follow_profile_config: bool = False\n",
        FIELD, label="session.create contract")
    _insert(
        contracts / "sessions.py",
        "    close_on_disconnect: bool = False\n",
        FIELD, after="\n\nclass SessionResumeResult", label="session.resume contract")
    _insert(
        contracts / "prompt_voice.py",
        "    voice_context: str | None = None  # recent spoken transcript, model input only (voice-live)\n",
        FIELD, label="prompt.submit contract")

    server = GATEWAY / "server.py"
    text = server.read_text()
    if TAG in text:
        print("[patch-client-context] already applied (server install)")
    else:
        if "_m.register(sys.modules[__name__])\ndel _m\n" not in text:
            raise SystemExit(
                "[patch-client-context] split-module registration loop missing in server.py. "
                "Hermes image may have changed — update this patch."
            )
        server.write_text(text.rstrip("\n") + "\n" + INSTALL)
        print("[patch-client-context] patched server install")


if __name__ == "__main__":
    main()
