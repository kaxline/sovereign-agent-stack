#!/usr/bin/env python3
"""CalDAV accounts register through the add-on path and leave other clients' entries alone.

Runs the bootstrap's register_addon_instance_servers in the pinned Hermes image
with the real addons/ manifests and a synthetic store under
<data>/caldav/{instances,secrets}, on the default profile and on api-server,
with CALDAV_MCP_ENABLED on and off. Checks:

- caldav-<slug> gets the shared caldav-mcp URL, X-Caldav-* headers with the
  password as ${CALDAV_<SLUG>_PASSWORD}, all 14 tools and the key in .env;
- api-server gets no account (calendar is personal data);
- a stale caldav-* entry and its key are dropped;
- Boundary's calendar-<slug> at the same URL, and its BOUNDARY_CALENDAR_* key,
  stay whether the flag is on or off.

Needs the pinned Hermes image (skips without it, unless --image).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402

IMAGE_DRIVER = r'''
import json
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

helpers = "".join(func(n) for n in (
    "replace_file", "upsert_env", "remove_env_key", "config_path_for", "profile_env_file",
    "addon_instances_plan", "addon_instance_key_env", "addon_instances_warn_none",
    "write_headers_mcp_entry", "reconcile_addon_instance_servers",
    "register_addon_instance_servers"))

home = Path(tempfile.mkdtemp())
(home / "profiles" / "api-server").mkdir(parents=True)
data = home / "addon-data"
store = data / "caldav"
(store / "instances").mkdir(parents=True)
(store / "secrets").mkdir()
for slug, url in (("personal", "https://caldav.example.com/"), ("work-cal", "https://dav.example.com/w/")):
    (store / "instances" / f"{slug}.json").write_text(
        json.dumps({"url": url, "username": f"{slug}@example.com"}))
    (store / "secrets" / f"{slug}.password").write_text(f"synthetic-{slug}")

URL = "http://caldav-mcp:8080/mcp"
READ = ["caldav_list_calendars", "caldav_get_events", "caldav_get_today_events",
        "caldav_get_week_events", "caldav_get_event_by_uid", "caldav_search_events",
        "caldav_get_freebusy", "caldav_list_attendees"]
WRITE = ["caldav_create_event", "caldav_update_event", "caldav_delete_event",
         "caldav_move_event", "caldav_add_attendee", "caldav_remove_attendee"]
FOREIGN = {
    "url": URL,
    "trust": "untrusted",
    "headers": {
        "X-Caldav-Url": "https://caldav.example.com/",
        "X-Caldav-Username": "sam@example.com",
        "X-Caldav-Password": "${BOUNDARY_CALENDAR_TEST_PASSWORD}",
    },
}

def paths(profile):
    base = home if profile == "" else home / "profiles" / profile
    return base / "config.yaml", base / ".env"

def seed(profile):
    cfg, env = paths(profile)
    cfg.write_text(yaml.safe_dump({"mcp_servers": {
        "calendar-test": FOREIGN,
        "caldav-old": {"url": URL, "headers": {"X-Caldav-Password": "${CALDAV_OLD_PASSWORD}"}},
    }}))
    env.write_text("BOUNDARY_CALENDAR_TEST_PASSWORD=synthetic-boundary\nCALDAV_OLD_PASSWORD=synthetic-old\n")

def run(profile, enabled):
    script = (
        "set -eu\n"
        f"HERMES_HOME={home}\nDEFAULT_ENV={home}/.env\n"
        f"ADDONS_DIR=/addons\nADDON_DATA_DIR={data}\nADDON_MANIFEST_PY=/addon-lib/addon_manifest.py\n"
        f"CALDAV_MCP_ENABLED={enabled}\nexport CALDAV_MCP_ENABLED\n"
        "log() { :; }\npin_mcp_tools() { :; }\n"
        + helpers +
        f'register_addon_instance_servers "{profile}"\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL bootstrap ({profile or 'default'}, enabled={enabled}): {out.stdout}{out.stderr}")
    cfg, env = paths(profile)
    servers = (yaml.safe_load(cfg.read_text()) or {}).get("mcp_servers") or {}
    env = dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line)
    return servers, env

def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL {msg}")

seed("")
servers, env = run("", 1)
expect(servers.get("calendar-test") == FOREIGN, f"enabled: calendar-test changed or removed ({servers.get('calendar-test')!r})")
expect(env.get("BOUNDARY_CALENDAR_TEST_PASSWORD") == "synthetic-boundary", "enabled: BOUNDARY_CALENDAR_TEST_PASSWORD removed")
p = servers.get("caldav-personal") or {}
expect(p.get("url") == URL and p.get("timeout") == 60 and p.get("connect_timeout") == 30,
       f"enabled: caldav-personal url and timeouts ({p!r})")
expect(p.get("headers") == {"X-Caldav-Url": "https://caldav.example.com/",
                            "X-Caldav-Username": "personal@example.com",
                            "X-Caldav-Password": "${CALDAV_PERSONAL_PASSWORD}"},
       f"enabled: caldav-personal headers ({p.get('headers')!r})")
expect((p.get("tools") or {}).get("include") == READ + WRITE, f"enabled: caldav-personal tools ({p.get('tools')!r})")
expect(env.get("CALDAV_PERSONAL_PASSWORD") == "synthetic-personal", "enabled: CALDAV_PERSONAL_PASSWORD not written")
expect(env.get("CALDAV_WORK_CAL_PASSWORD") == "synthetic-work-cal", "enabled: CALDAV_WORK_CAL_PASSWORD not written")
expect("caldav-work-cal" in servers, "enabled: caldav-work-cal not registered")
expect("caldav-old" not in servers, "enabled: stale caldav-old kept")
expect("CALDAV_OLD_PASSWORD" not in env, "enabled: stale CALDAV_OLD_PASSWORD kept")

seed("api-server")
servers, env = run("api-server", 1)
expect(not [k for k in servers if k.startswith("caldav-")], f"api-server: got caldav entries ({sorted(servers)})")
expect(not [k for k in env if k.startswith("CALDAV_")], f"api-server: got CALDAV_ keys ({sorted(env)})")
expect(servers.get("calendar-test") == FOREIGN, "api-server: calendar-test changed or removed")

seed("")
run("", 1)
servers, env = run("", 0)
expect(servers.get("calendar-test") == FOREIGN, "disabled: calendar-test changed or removed")
expect(env.get("BOUNDARY_CALENDAR_TEST_PASSWORD") == "synthetic-boundary", "disabled: BOUNDARY_CALENDAR_TEST_PASSWORD removed")
expect(not [k for k in servers if k.startswith("caldav-")], f"disabled: caldav entries kept ({sorted(servers)})")
expect(not [k for k in env if k.startswith("CALDAV_")], f"disabled: CALDAV_ keys kept ({sorted(env)})")

print("image ok")
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER, mounts=((str(ROOT / "addons"), "/addons"),
                                                 (str(ROOT / "scripts" / "lib"), "/addon-lib")))


if __name__ == "__main__":
    main()
