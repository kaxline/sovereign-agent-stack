#!/usr/bin/env python3
"""Checks configure_messages_webhook (bootstrap-api-profile.sh) inside the
running hermes container, against throwaway config files in its /tmp.

Needs a running hermes container (HERMES_CONTAINER, default "hermes");
skips otherwise. Never touches data/hermes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "compose" / "hermes" / "bootstrap-api-profile.sh"
SECRET = "webhook-test-secret-0123456789"
CONTAINER = os.environ.get("HERMES_CONTAINER", "hermes")


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def script() -> str:
    body = re.search(r"^configure_messages_webhook\(\) \{\n(.*?)^\}", BOOTSTRAP.read_text(), re.S | re.M).group(1)
    return re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.S).group(1)


def exec_in_hermes(cmd: list[str], stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    extra = [f"--env={k}={v}" for k, v in (env or {}).items()]
    return subprocess.run(["docker", "exec", "-i", *extra, CONTAINER, *cmd],
                          input=stdin, capture_output=True, text=True, timeout=60)


def main() -> int:
    up = subprocess.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                        capture_output=True, text=True)
    if up.returncode != 0 or up.stdout.strip() != CONTAINER:
        print("SKIP messages webhook config (hermes isn't running)")
        return 0
    py = script()
    work = "/tmp/messages-webhook-test"
    exec_in_hermes(["sh", "-c", f"rm -rf {work} && mkdir -p {work}"])

    def configure(on: bool, port: int = 8646) -> str:
        r = exec_in_hermes(["python3", "-", f"{work}/config.yaml", "1" if on else "0", str(port)], py,
                           env={"MESSAGES_WEBHOOK_SECRET": SECRET})
        expect(r.returncode == 0, f"configure failed: {r.stderr[-400:]}")
        return r.stdout.strip()

    def read() -> dict:
        # Hermes v0.22+ has no PyYAML; hermes_yaml wraps ruamel, as in the bootstrap.
        r = exec_in_hermes(["python3", "-c", "import json,sys; sys.path.insert(0, '/opt/hermes')\n"
                            "try:\n    import yaml\nexcept ImportError:\n    import hermes_yaml as yaml\n"
                            f"print(json.dumps(yaml.safe_load(open('{work}/config.yaml'))))"])
        return json.loads(r.stdout)

    def write(text: str) -> None:
        exec_in_hermes(["sh", "-c", f"cat > {work}/config.yaml && rm -f {work}/.messages-webhook.json"], text)

    write("model: x\n")
    out = configure(True)
    cfg = read()
    wh = cfg["platforms"]["webhook"]
    route = wh["extra"]["routes"]["messages"]
    expect("set the messages webhook route" in out and wh["enabled"] is True, f"enabled: {out}")
    expect(wh["extra"]["port"] == 8646 and wh["extra"]["host"] == "0.0.0.0", "port and host")
    expect(route == {"secret": SECRET, "events": ["messages.new"], "cron_job": "messages-triage",
                     "prompt": route["prompt"]}, f"route: {route}")
    expect("{count}" in route["prompt"] and "{chat" not in route["prompt"], "prompt carries only the count")
    expect(cfg["model"] == "x", "other settings kept")
    expect("already set" in configure(True), "second run changes nothing")

    out = configure(False)
    expect("removed" in out and "webhook" not in read().get("platforms", {}), "turning off removes what we added")

    # A webhook platform the user set up: keep their routes and port, just add ours.
    write("platforms:\n  webhook:\n    enabled: true\n    extra:\n      port: 9000\n      routes:\n"
          "        github:\n          secret: s\n")
    out = configure(True)
    wh = read()["platforms"]["webhook"]
    expect("WARNING" in out and wh["extra"]["port"] == 9000, f"their port is kept, with a warning: {out}")
    expect(set(wh["extra"]["routes"]) == {"github", "messages"}, "their route is kept")
    configure(False)
    wh = read()["platforms"]["webhook"]
    expect(wh["enabled"] is True and set(wh["extra"]["routes"]) == {"github"}, "turning off leaves theirs alone")

    exec_in_hermes(["rm", "-rf", work])
    print("OK messages webhook config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
