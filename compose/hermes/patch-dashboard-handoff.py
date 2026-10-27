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
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
BOOTSTRAP = Path(os.environ.get("DASHBOARD_HANDOFF_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: dashboard handoff"
TAG = "[patch-dashboard-handoff]"
AUTH = ROOT / "hermes_cli" / "dashboard_auth"


def _install() -> None:
    src, dst = BOOTSTRAP / "dashboard_handoff.py", ROOT / "hermes_cli" / "dashboard_handoff.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, dst)
    print(f"{TAG} installed {dst}")


def _edit(path: Path, label: str, edits: list[tuple[str, str, int]], append: str = "") -> None:
    """Apply (old, new, expected_count) edits to *path* once; MARKER makes it idempotent."""
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({label})")
        return
    for old, new, count in edits:
        found = text.count(old)
        if found != count:
            raise SystemExit(
                f"{TAG} expected {count} x {old.strip()[:60]!r} in {path}, found {found}. "
                "Hermes image may have changed — update this patch.")
        text = text.replace(old, new)
    if append:
        text = text.rstrip("\n") + "\n" + append
    path.write_text(text)
    print(f"{TAG} patched {label}")


def _patch_routes() -> None:
    path = AUTH / "routes.py"
    text = path.read_text()
    if MARKER not in text and ("\nrouter = APIRouter()\n" not in text or "\ndef _complete_login(" not in text):
        raise SystemExit(f"{TAG} {path} has no module-level router or _complete_login. "
                         "Hermes image may have changed — update this patch.")
    install = (
        f"\n\n{MARKER}: GET /auth/handoff (one-time sign-in links).\n"
        "try:\n"
        "    import sys as _assistant_sys\n"
        "    from hermes_cli import dashboard_handoff as _assistant_handoff\n"
        "    _assistant_handoff.install(_assistant_sys.modules[__name__])\n"
        "except Exception:\n"
        "    _log.exception(\"dashboard handoff overlay failed to install\")\n"
    )
    _edit(path, "handoff route", [], append=install)


def _patch_gate() -> None:
    old = '    "/auth/native/refresh", "/auth/password-login", "/auth/logout", "/login",\n'
    new = old + f'    "/auth/handoff",  {MARKER}\n'
    _edit(AUTH / "middleware.py", "public handoff path", [(old, new, 1)])


def main() -> None:
    _install()
    _patch_routes()
    _patch_gate()


if __name__ == "__main__":
    main()
