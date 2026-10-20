#!/usr/bin/env python3
"""CalDAV bootstrap leaves entries other clients registered alone.

Boundary registers calendars through the Hermes gateway as calendar-<slug>,
pointing at the same caldav-mcp URL, with BOUNDARY_CALENDAR_* password keys.
The bootstrap's CalDAV pass (register_caldav_mcp_servers and
reconcile_caldav_accounts) runs in the pinned Hermes image against a
synthetic HERMES_HOME, enabled and disabled, and must keep both the entry and
its key while still pruning its own stale caldav-* entries.

Needs the pinned Hermes image (skips without it, unless --image).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
import hermes_yaml as yaml

src = Path("/bootstrap/bootstrap-api-profile.sh").read_text()

def func(name):
    """One shell function, by its start line up to the next top-level line that closes it."""
    m = re.search(rf"^{name}\(\) {{\n.*?^}}\n(?=\n|#|[a-z_]+\(\) {{)", src, re.S | re.M)
    if not m:
        raise SystemExit(f"FAIL function {name} not found")
    return m.group(0)

# The CalDAV block runs from caldav_password_env_key to the end of
# register_caldav_mcp_servers; its heredocs hold lines that start with "}".
start = src.index("caldav_password_env_key() {")
end = src.index('if [ ! -f "${HERMES_HOME}/config.yaml" ]; then')
caldav_block = src[start:end]
helpers = "".join(func(n) for n in ("replace_file", "upsert_env", "remove_env_key", "config_path_for"))

home = Path(tempfile.mkdtemp())
accounts = home / "accounts"
accounts.mkdir()
(accounts / "personal.env").write_text(
    "CALDAV_URL=https://caldav.example.com/\nCALDAV_USERNAME=sam@example.com\nCALDAV_PASSWORD=synthetic-pass\n")

URL = "http://caldav-mcp:8080/mcp"
FOREIGN = {
    "url": URL,
    "trust": "untrusted",
    "headers": {
        "X-Caldav-Url": "https://caldav.example.com/",
        "X-Caldav-Username": "sam@example.com",
        "X-Caldav-Password": "${BOUNDARY_CALENDAR_TEST_PASSWORD}",
    },
}

def seed():
    cfg = {"mcp_servers": {
        "calendar-test": FOREIGN,
        "caldav-old": {"url": URL, "headers": {"X-Caldav-Password": "${CALDAV_OLD_PASSWORD}"}},
    }}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    (home / ".env").write_text("BOUNDARY_CALENDAR_TEST_PASSWORD=synthetic-boundary\nCALDAV_OLD_PASSWORD=synthetic-old\n")

def run(enabled):
    script = (
        "set -eu\n"
        f"HERMES_HOME={home}\nDEFAULT_ENV={home}/.env\n"
        f"CALDAV_MCP_URL={URL}\nCALDAV_MCP_TIMEOUT=60\nCALDAV_MCP_CONNECT_TIMEOUT=30\n"
        f"CALDAV_MCP_ENABLED={enabled}\nCALDAV_MCP_ACCOUNTS_DIR={accounts}\n"
        "log() { :; }\npin_mcp_tools() { :; }\n"
        + helpers + caldav_block +
        'register_caldav_mcp_servers ""\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL bootstrap (enabled={enabled}): {out.stdout}{out.stderr}")
    servers = (yaml.safe_load((home / "config.yaml").read_text()) or {}).get("mcp_servers") or {}
    env = dict(line.split("=", 1) for line in (home / ".env").read_text().splitlines() if "=" in line)
    return servers, env

def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")

seed()
servers, env = run(1)
expect(servers.get("calendar-test") == FOREIGN, f"enabled: calendar-test changed or removed ({servers.get('calendar-test')!r})")
expect(env.get("BOUNDARY_CALENDAR_TEST_PASSWORD") == "synthetic-boundary", "enabled: BOUNDARY_CALENDAR_TEST_PASSWORD removed")
expect("caldav-personal" in servers, "enabled: caldav-personal not registered")
expect(env.get("CALDAV_PERSONAL_PASSWORD") == "synthetic-pass", "enabled: CALDAV_PERSONAL_PASSWORD not written")
expect("caldav-old" not in servers, "enabled: stale caldav-old kept")
expect("CALDAV_OLD_PASSWORD" not in env, "enabled: stale CALDAV_OLD_PASSWORD kept")

seed()
servers, env = run(0)
expect(servers.get("calendar-test") == FOREIGN, "disabled: calendar-test changed or removed")
expect(env.get("BOUNDARY_CALENDAR_TEST_PASSWORD") == "synthetic-boundary", "disabled: BOUNDARY_CALENDAR_TEST_PASSWORD removed")
expect("caldav-old" in servers, "disabled: bootstrap must not touch CalDAV entries at all")

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
