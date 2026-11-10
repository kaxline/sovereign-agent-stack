#!/usr/bin/env python3
"""Checks configure_messages_webhook (bootstrap-api-profile.sh) in the pinned
Hermes image, against throwaway config files in the container's /tmp.

Host: the Python body can be cut out of the bootstrap function. Image (skips
without the pinned image unless --image): turning the webhook on adds the
messages route, a second run changes nothing, turning it off removes only what
it added, and a webhook platform the user set up keeps its routes and port.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import hermes_image  # noqa: E402
from testkit import expect  # noqa: E402

BOOTSTRAP = ROOT / "compose" / "hermes" / "bootstrap-api-profile.sh"


def script() -> str:
    m = re.search(r"^configure_messages_webhook\(\) \{\n(.*?)^\}", BOOTSTRAP.read_text(), re.S | re.M)
    expect(m is not None, "bootstrap-api-profile.sh defines configure_messages_webhook")
    body = re.search(r"<<'PY'\n(.*?)\nPY\n", m.group(1), re.S)
    expect(body is not None, "configure_messages_webhook runs a python3 heredoc")
    return body.group(1)


# Reads the configure script on stdin. The bootstrap runs it with the image's
# python3, which is the interpreter this driver runs under.
IMAGE_DRIVER = r'''
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
try:
    import yaml
except ImportError:  # Hermes v0.22+ has no PyYAML; hermes_yaml wraps ruamel, as in the bootstrap
    import hermes_yaml as yaml

SECRET = "webhook-test-secret-0123456789"
CONFIGURE = sys.stdin.read()
work = Path(tempfile.mkdtemp(prefix="messages-webhook-"))
cfg_path = work / "config.yaml"


def expect(ok, msg):
    if not ok:
        raise SystemExit(f"FAIL {msg}")


def configure(on, port=8646):
    r = subprocess.run([sys.executable, "-", str(cfg_path), "1" if on else "0", str(port)], input=CONFIGURE,
                       capture_output=True, text=True, env={"MESSAGES_WEBHOOK_SECRET": SECRET, "PATH": "/usr/bin:/bin"})
    expect(r.returncode == 0, f"configure failed: {r.stderr[-400:]}")
    return r.stdout.strip()


def read():
    return yaml.safe_load(cfg_path.read_text())


def write(text):
    cfg_path.write_text(text)
    (work / ".messages-webhook.json").unlink(missing_ok=True)


write("model: x\n")
out = configure(True)
cfg = read()
wh = cfg["platforms"]["webhook"]
route = wh["extra"]["routes"]["messages"]
expect("set the messages webhook route" in out and wh["enabled"] is True, f"enabled: {out}")
expect(wh["extra"]["port"] == 8646 and wh["extra"]["host"] == "0.0.0.0", "port and host")
expect(dict(route) == {"secret": SECRET, "events": ["messages.new"], "cron_job": "messages-triage",
                       "prompt": route["prompt"]}, f"route: {json.dumps(route, default=str)}")
expect("{count}" in route["prompt"] and "{chat" not in route["prompt"], "prompt carries only the count")
expect(cfg["model"] == "x", "other settings kept")
expect("already set" in configure(True), "second run changes nothing")

out = configure(False)
expect("removed" in out and "webhook" not in (read().get("platforms") or {}), "turning off removes what we added")

# A webhook platform the user set up: keep their routes and port, add ours.
write("platforms:\n  webhook:\n    enabled: true\n    extra:\n      port: 9000\n      routes:\n"
      "        github:\n          secret: s\n")
out = configure(True)
wh = read()["platforms"]["webhook"]
expect("WARNING" in out and wh["extra"]["port"] == 9000, f"their port is kept, with a warning: {out}")
expect(set(wh["extra"]["routes"]) == {"github", "messages"}, "their route is kept")
configure(False)
wh = read()["platforms"]["webhook"]
expect(wh["enabled"] is True and set(wh["extra"]["routes"]) == {"github"}, "turning off leaves theirs alone")
print("image ok")
'''


def main() -> int:
    configure = script()
    hermes_image.maybe_run(IMAGE_DRIVER, stdin=configure)
    print("OK messages webhook config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
