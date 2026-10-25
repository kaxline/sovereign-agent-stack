#!/usr/bin/env python3
"""Open the Hermes dashboard signed in, through a one-time /auth/handoff link.

Usage: scripts/dashboard-open.py [PATH] [--print]

PATH is where to land (default /), for example /kanban or
"/kanban?task=t_1234abcd". --print writes the link instead of opening it; it
works for 60 seconds and only once. Reads HERMES_DASHBOARD_HANDOFF_SECRET and
HERMES_DASHBOARD_PORT from .env (ENV_FILE overrides the path).
See docs/hermes.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))

import dashboard_handoff  # noqa: E402


def env_get(key: str, default: str = "") -> str:
    path = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                return value or default
    return default


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--print"]
    target = args[0] if args else "/"
    if not target.startswith("/"):
        target = f"/{target}"
    secret = env_get(dashboard_handoff.SECRET_ENV)
    if not dashboard_handoff.usable_secret(secret):
        sys.exit(f"error: {dashboard_handoff.SECRET_ENV} is not set in .env. "
                 "Run ./scripts/setup.sh, then recreate hermes: docker compose up -d hermes")
    port = env_get("HERMES_DASHBOARD_PORT", "9119")
    code = dashboard_handoff.mint_code(secret, target)
    url = f"http://localhost:{port}/auth/handoff?code={quote(code, safe='')}"
    opener = shutil.which("open") or shutil.which("xdg-open")
    if "--print" in sys.argv[1:] or not opener:
        print(url)
        return
    subprocess.run([opener, url], check=True)


if __name__ == "__main__":
    main()
