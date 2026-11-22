#!/usr/bin/env python3
"""Gated cron fires leave no empty chat; cron sessions refuse typed turns.

Overlay for Boundary CR 0033 (not upstream yet; see docs/contracts/upstreams.md).
Copies cron_gated_sessions.py into the agent package and appends an install
call to gateway/session_cron.py and to tui_gateway/server.py. The handlers are
wrapped at runtime, not edited.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, FileEdit, edit_files, install

BOOTSTRAP = Path(os.environ.get("CRON_GATED_SESSIONS_BOOTSTRAP", "/bootstrap"))

MARKER = "# assistant-stack: cron gated sessions"
TAG = "[patch-cron-gated-sessions]"

CRON_ANCHOR = "async def execute(authority, ref, row, policy):"
SERVER_ANCHOR = "_m.register(sys.modules[__name__])\ndel _m\n"

CRON_INSTALL = f'''

{MARKER}
try:
    import sys as _cgs_sys
    from agent.cron_gated_sessions import install_cron as _cgs_install
    _cgs_install(_cgs_sys.modules[__name__])
except Exception:
    import logging as _cgs_logging
    _cgs_logging.getLogger(__name__).exception("cron gated sessions overlay failed to install")
'''

SERVER_INSTALL = f'''

{MARKER}
try:
    from agent.cron_gated_sessions import install_gateway as _cgs_install
    _cgs_install(sys.modules[__name__])
except Exception:
    logger.exception("cron read-only overlay failed to install")
'''


def main() -> None:
    install(BOOTSTRAP / "cron_gated_sessions.py", ROOT / "agent" / "cron_gated_sessions.py", TAG)
    edit_files([
        FileEdit(ROOT / "gateway" / "session_cron.py", label="session_cron install",
                 append=CRON_INSTALL, requires=[CRON_ANCHOR]),
        FileEdit(ROOT / "tui_gateway" / "server.py", label="server install",
                 append=SERVER_INSTALL, requires=[SERVER_ANCHOR]),
    ], MARKER, TAG)


if __name__ == "__main__":
    main()
