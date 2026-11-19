#!/usr/bin/env python3
"""compose/hermes/profile_config.py, the bootstrap's config.yaml editor.

Host: both bootstrap services mount it at /bootstrap/profile_config.py, the
bootstrap's inputs hash covers it, scripts/lib/hermes-yaml.sh mounts it, and
no bootstrap heredoc carries its own YAML import or writer any more.
Image (skips without the pinned image unless --image), as a non-root user like
hermes_set_platform_disabled: the three list policies, walk, and
platform-disabled on a file with a duplicate platforms: block.
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
MOUNT = "- ./compose/hermes/profile_config.py:/bootstrap/profile_config.py:ro"


def host_checks() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for svc in ("hermes-api-bootstrap", "hermes-browser-bootstrap"):
        m = re.search(rf"^  {svc}:\n(.*?)(?=^  \S)", compose, re.S | re.M)
        expect(m is not None, f"docker-compose.yml defines {svc}")
        expect(MOUNT in m.group(1), f"{svc} mounts profile_config.py at /bootstrap")

    text = BOOTSTRAP.read_text()
    m = re.search(r"^bootstrap_inputs_hash\(\) \{\n(.*?)^\}", text, re.S | re.M)
    expect(m is not None and 'cat "$PROFILE_CONFIG_PY"' in m.group(1),
           "bootstrap_inputs_hash covers profile_config.py, so editing it re-runs the bootstrap")
    for pattern, what in (
        (r"import hermes_yaml|import yaml\b", "a YAML import"),
        (r"atomic_yaml_write\(", "atomic_yaml_write"),
        (r"\byaml\.safe_(load|dump)\(", "yaml.safe_load or safe_dump"),
    ):
        hits = [n for n, line in enumerate(text.splitlines(), 1) if re.search(pattern, line)]
        expect(not hits, f"bootstrap heredocs use profile_config, not {what} (lines {hits})")

    yaml_sh = (ROOT / "scripts" / "lib" / "hermes-yaml.sh").read_text()
    expect("compose/hermes/profile_config.py:/bootstrap/profile_config.py:ro" in yaml_sh
           and "platform-disabled" in yaml_sh,
           "hermes_set_platform_disabled runs profile_config.py platform-disabled")
    print("ok host checks")


IMAGE_DRIVER = r'''
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/bootstrap")
import profile_config as pc
import hermes_yaml

def expect(cond, msg):
    if not cond:
        print(f"FAIL {msg}")
        raise SystemExit(1)

tmp = Path(tempfile.mkdtemp())
cfg = tmp / "profiles" / "browser" / "config.yaml"
cfg.parent.mkdir(parents=True)

def write(text):
    cfg.write_text(text)

def read():
    return hermes_yaml.safe_load(cfg.read_text())

def cli(*args):
    p = subprocess.run([sys.executable, "/bootstrap/profile_config.py", *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip() + p.stderr.strip()

# superset: set when missing, leave a longer list, warn about another list.
write("model:\n  default: test\n")
rc, out = cli("list", "--policy", "superset", str(cfg), "skills.external_dirs", "/opt/skills")
expect(rc == 0 and out == "Setting skills.external_dirs (browser)", out)
expect(read()["skills"]["external_dirs"] == ["/opt/skills"], read())
rc, out = cli("list", "--policy", "superset", str(cfg), "skills.external_dirs", "/opt/skills")
expect(out == "skills.external_dirs (browser) already set", out)
write("skills:\n  external_dirs: [/opt/skills, /opt/more]\n")
rc, out = cli("list", "--policy", "superset", str(cfg), "skills.external_dirs", "/opt/skills")
expect(out.startswith("skills.external_dirs (browser) already includes"), out)
write("skills:\n  external_dirs: [/opt/skills-backup]\n")
rc, out = cli("list", "--policy", "superset", str(cfg), "skills.external_dirs", "/opt/skills")
expect(out.startswith("WARNING:") and read()["skills"]["external_dirs"] == ["/opt/skills-backup"], out)
write("skills:\n  external_dirs: /opt/skills\n")
rc, out = cli("list", "--policy", "superset", str(cfg), "skills.external_dirs", "/opt/skills")
expect(out == "Repairing skills.external_dirs (browser)", out)

# exact: replace any list that differs, naming what was dropped.
write("mcp_servers:\n  mail:\n    tools:\n      include: [search, send]\n")
rc, out = cli("list", "--policy", "exact", str(cfg), "mcp_servers.mail.tools.include", "search")
expect(out == "Replacing mcp_servers.mail.tools.include (browser) (dropped send)", out)
expect(read()["mcp_servers"]["mail"]["tools"]["include"] == ["search"], read())

# merge: a scalar becomes the first item; missing items are appended.
write("tools:\n  always_include: session_search\n")
rc, out = cli("list", "--policy", "merge", str(cfg), "tools.always_include", "a", "session_search", "b")
expect(out == "Adding a, b to tools.always_include (browser)", out)
expect(read()["tools"]["always_include"] == ["session_search", "a", "b"], read())
rc, out = cli("list", "--policy", "merge", str(cfg), "tools.always_include", "a")
expect(out.endswith("already includes a"), out)

# A missing file is an error for the list editors, as before.
rc, out = cli("list", "--policy", "merge", str(tmp / "nope.yaml"), "a.b", "c")
expect(rc == 1 and out.startswith("ERROR:"), (rc, out))

# walk
data = {"a": "scalar"}
node, leaf = pc.walk(data, "a.b.c")
expect(leaf == "c" and data == {"a": {"b": {}}} and node is data["a"]["b"], data)
expect(pc.walk({}, "x.y", create=False) == (None, "y"), "walk without create")
expect(pc.profile_label(tmp / "config.yaml", "k") == "k (default)", "default label")

# platform-disabled: duplicate platforms: blocks merge, and the file is
# rewritten even though signal was already off.
write("platforms:\n  signal:\n    enabled: false\nmodel:\n  default: t\nplatforms:\n  buzz:\n    enabled: true\n")
rc, out = cli("platform-disabled", str(cfg), "signal", "--name", "browser")
expect(out == "pinned platforms.signal.enabled=false on browser", out)
data = read()
expect(data["platforms"] == {"signal": {"enabled": False}, "buzz": {"enabled": True}}, data)
expect(cfg.read_text().count("platforms:") == 1, cfg.read_text())
rc, out = cli("platform-disabled", str(cfg), "signal")
expect(out == "platforms.signal.enabled already false", out)
before = cfg.read_text()
rc, out = cli("platform-disabled", str(cfg), "buzz")
expect(read()["platforms"]["buzz"] == {"enabled": False} and before != cfg.read_text(), out)
rc, out = cli("platform-disabled", str(tmp / "nope.yaml"), "signal")
expect(rc == 0 and out == "", (rc, out))

# save writes what Hermes' own writer does: no sorting, block style.
write("")
pc.save(cfg, {"z": 1, "a": [1, 2]})
expect(cfg.read_text() == "z: 1\na:\n  - 1\n  - 2\n", repr(cfg.read_text()))
expect(pc.load(cfg) == {"z": 1, "a": [1, 2]} and (write("") or pc.load(cfg) == {}), "load")
print("image ok")
'''


def main() -> int:
    host_checks()
    hermes_image.maybe_run(IMAGE_DRIVER, docker_args=("--user", "1000:1000", "-e", "HOME=/tmp"))
    print("OK profile config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
