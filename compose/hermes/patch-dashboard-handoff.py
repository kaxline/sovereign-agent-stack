#!/usr/bin/env python3
"""One-time sign-in links for the dashboard: GET /auth/handoff?code=...

Compatibility overlay (not upstream), for Boundary change request 0007 R1.
Copies dashboard_handoff.py into hermes_cli/ and:

- registers the route at the end of hermes_cli/dashboard_auth/routes.py, before
  web_server includes that router;
- adds ``/auth/handoff`` to the auth gate's public prefixes
  (hermes_cli/dashboard_auth/middleware.py), like ``/auth/password-login``.

The route is off (404) until HERMES_DASHBOARD_HANDOFF_SECRET is set. See
dashboard_handoff.py for the design and docs/extending.md for the contract.
"""
from __future__ import annotations

import os
from pathlib import Path

from overlay_patch import ROOT, edit, install

BOOTSTRAP = Path(os.environ.get("DASHBOARD_HANDOFF_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: dashboard handoff"
TAG = "[patch-dashboard-handoff]"
AUTH = ROOT / "hermes_cli" / "dashboard_auth"


def _patch_routes() -> None:
    install_call = (
        f"\n\n{MARKER}: GET /auth/handoff (one-time sign-in links).\n"
        "try:\n"
        "    import sys as _assistant_sys\n"
        "    from hermes_cli import dashboard_handoff as _assistant_handoff\n"
        "    _assistant_handoff.install(_assistant_sys.modules[__name__])\n"
        "except Exception:\n"
        "    _log.exception(\"dashboard handoff overlay failed to install\")\n"
    )
    edit(AUTH / "routes.py", [], MARKER, TAG, label="handoff route", append=install_call,
         requires=["\nrouter = APIRouter()\n", "\ndef _complete_login("])


def _patch_gate() -> None:
    old = '    "/auth/native/refresh", "/auth/password-login", "/auth/logout", "/login",\n'
    new = old + f'    "/auth/handoff",  {MARKER}\n'
    edit(AUTH / "middleware.py", [(old, new)], MARKER, TAG, label="public handoff path")


def main() -> None:
    install(BOOTSTRAP / "dashboard_handoff.py", ROOT / "hermes_cli" / "dashboard_handoff.py", TAG)
    _patch_routes()
    _patch_gate()


if __name__ == "__main__":
    main()
