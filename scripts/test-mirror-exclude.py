#!/usr/bin/env python3
"""Checks for mirror_exclude on profiles.create (patch-mirror-exclude.py).

In the pinned Hermes image, the patch is applied twice (idempotent), then
profiles.create runs through the gateway's request handler with strict
contracts against a synthetic launch .env: with and without mirror_exclude,
with mirroring off, with a wrong type and with a filter that fails.

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
import subprocess
import sys
from pathlib import Path

home = Path("/tmp/hermes-home")
home.mkdir(parents=True)
os.environ["HERMES_HOME"] = str(home)
(home / "config.yaml").write_text("model:\n  provider: custom\n  default: synthetic-model\n")
LAUNCH_ENV = (
    "# launch profile\n"
    "OPENAI_API_KEY=synthetic-model-key\n"
    "CALDAV_PERSONAL_PASSWORD=synthetic-caldav\n"
    "BOUNDARY_CALENDAR_HOME_PASSWORD=synthetic-home\n"
    "export BOUNDARY_CALENDAR_WORK_PASSWORD=synthetic-work\n"
    "BOUNDARY_OTHER=keep\n"
)
(home / ".env").write_text(LAUNCH_ENV)

outs = []
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-mirror-exclude.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    outs.append(out.stdout)
assert outs[0].count("patched") == 2, outs[0]
assert outs[1].count("already applied") == 2, outs[1]

sys.path.insert(0, "/opt/hermes")
from tui_gateway.contracts import registry
from tui_gateway import server

registry.STRICT = True

def create(name, **params):
    resp = server.handle_request({"id": 1, "method": "profiles.create",
                                  "params": {"name": name, "no_alias": True, **params}})
    assert "result" in resp, resp
    env_path = home / "profiles" / name / ".env"
    keys = {line.split("=", 1)[0].replace("export ", "").strip()
            for line in env_path.read_text().splitlines() if "=" in line and not line.startswith("#")}
    return resp["result"]["mirrored"], keys, env_path.read_text()

# No mirror_exclude: unchanged behaviour, every key mirrored.
mirrored, keys, _ = create("plain")
assert mirrored["env"] is True, mirrored
assert {"CALDAV_PERSONAL_PASSWORD", "BOUNDARY_CALENDAR_HOME_PASSWORD"} <= keys, keys
assert mirrored.get("env_excluded") is None, mirrored

# The acceptance case: model credentials stay, matching keys go, values never reported.
mirrored, keys, text = create("off-agent", mirror_credentials=True,
                              mirror_exclude=["BOUNDARY_CALENDAR_*", "CALDAV_*_PASSWORD"])
assert mirrored["env"] is True, mirrored
assert keys == {"OPENAI_API_KEY", "BOUNDARY_OTHER"}, keys
assert "# launch profile" in text, "comments survive"
assert mirrored["env_excluded"] == ["BOUNDARY_CALENDAR_HOME_PASSWORD", "BOUNDARY_CALENDAR_WORK_PASSWORD",
                                    "CALDAV_PERSONAL_PASSWORD"], mirrored
assert "synthetic" not in repr(mirrored), "result names keys, never values"

# Globs are case-sensitive and anchored.
mirrored, keys, _ = create("case", mirror_exclude=["boundary_calendar_*", "CALENDAR_*"])
assert "BOUNDARY_CALENDAR_HOME_PASSWORD" in keys and mirrored["env_excluded"] == [], mirrored

# Mirroring off: nothing to filter.
mirrored, keys, _ = create("bare", mirror_credentials=False, mirror_exclude=["BOUNDARY_CALENDAR_*"])
assert mirrored["env"] is False and not keys & {"OPENAI_API_KEY"}, (mirrored, keys)

# An unknown key is refused before the handler runs (validate_params, 4000), so the
# contract field is what lets a client send mirror_exclude at all.
assert "mirror_exclude" in registry.METHODS["profiles.create"].params.model_fields

# A wrong type is the handler's to deal with (the contract check only logs outside
# tests): it must fail closed, a profile with no credentials rather than all of them.
registry.STRICT = False
try:
    mirrored, keys, _ = create("bad-type", mirror_exclude=5)
finally:
    registry.STRICT = True
assert mirrored["env"] is False and keys == set(), (mirrored, keys)

# So must a filter that raises.
import fnmatch
real = fnmatch.fnmatchcase
def broken(*a, **k):
    raise RuntimeError("synthetic failure")
fnmatch.fnmatchcase = broken
try:
    mirrored, keys, _ = create("broken", mirror_exclude=["BOUNDARY_CALENDAR_*"])
finally:
    fnmatch.fnmatchcase = real
assert mirrored["env"] is False and keys == set(), (mirrored, keys)

os.write(1, b"image ok\n")  # the gateway points sys.stdout at stderr
'''


def main() -> None:
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
