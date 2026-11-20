#!/usr/bin/env python3
"""scripts/lib/doctor_checks.py on synthetic config.yaml, override and JSON files."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

import doctor_checks as dc  # noqa: E402
from testkit import Checks  # noqa: E402

CONFIG = """\
# written by the bootstrap
model:
  default: qwen
  base_url: "http://llm-proxy:4000/v1"
agent:
  intent_ack_continuation: 'true'
terminal:
  backend: ssh
  cwd: /opt/projects
tools:
  tool_search:
    always_include:
      - session_search
      - 'mcp__searxng__searxng_web_search'
    defer: []
  other:
    always_include:
    - nested_only
mcp_servers:
  caldav-home:
    url: http://caldav-mcp:8080/mcp
    headers:
      X-Caldav-Password: ${CALDAV_HOME_PASSWORD}
  "quoted-key":
    url: http://x
flow: [a, 'b', "c"]
dash_at_key_indent:
- one
- two
after: 1
"""

OVERRIDE = """\
services:
  hermes:
    volumes:
      - /Users/me/code:/opt/code:ro
      - ./relative:/opt/rel
    environment:
      - /not/a/mount
  hermes-worker:
    volumes:
      - "/Users/me/code:/workspace/code"
"""


def run(cwd: Path, *args: str, stdin: str = "") -> tuple[int, list[str]]:
    res = subprocess.run(
        [sys.executable, "-I", str(LIB / "doctor_checks.py"), *args],
        cwd=cwd, input=stdin, capture_output=True, text=True,
    )
    return res.returncode, res.stdout.splitlines()


def main() -> int:
    c = Checks()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        hermes = root / "data/hermes"
        (hermes / "profiles/browser").mkdir(parents=True)
        (hermes / "profiles/worker").mkdir(parents=True)
        cfg = hermes / "config.yaml"
        cfg.write_text(CONFIG)

        get = lambda dotted: dc.yaml_get(cfg, dotted)  # noqa: E731
        c.expect(get("model.base_url") == "http://llm-proxy:4000/v1", "yaml_get unquotes a nested scalar")
        c.expect(get("agent.intent_ack_continuation") == "true", "yaml_get single quotes")
        c.expect(get("terminal.backend") == "ssh", "yaml_get terminal.backend")
        c.expect(get("tools.tool_search.defer") == "[]", "yaml_get finds a key with a flow value")
        c.expect(get("tools.tool_search.missing") is None, "yaml_get: absent leaf is None")
        c.expect(get("nope.backend") is None, "yaml_get: absent parent is None")
        c.expect(get("backend") is None, "yaml_get does not match a nested key at the top level")
        c.expect(get("mcp_servers.caldav-home.headers.X-Caldav-Password") == "${CALDAV_HOME_PASSWORD}",
                "yaml_get four levels down")
        c.expect(get("mcp_servers.quoted-key.url") == "http://x", "yaml_get a quoted key")
        c.expect(get("after") == "1", "yaml_get after a dash-at-key-indent list")

        lst = lambda dotted: dc.yaml_list(cfg, dotted)  # noqa: E731
        c.expect(lst("tools.tool_search.always_include") == ["session_search", "mcp__searxng__searxng_web_search"],
                f"yaml_list indented items: {lst('tools.tool_search.always_include')}")
        c.expect(lst("tools.other.always_include") == ["nested_only"], "yaml_list items at the key's indent, nested")
        c.expect(lst("dash_at_key_indent") == ["one", "two"], "yaml_list items at the key's indent, top level")
        c.expect(lst("flow") == ["a", "b", "c"], "yaml_list flow list")
        c.expect(lst("tools.tool_search.defer") == [], "yaml_list empty flow list")
        c.expect(lst("tools.missing") == [], "yaml_list absent key")

        rc, out = run(root, "yaml-get", str(cfg), "terminal.backend")
        c.expect((rc, out) == (0, ["ssh"]), f"yaml-get prints the value: {rc} {out}")
        rc, out = run(root, "yaml-get", str(cfg), "terminal.nope")
        c.expect((rc, out) == (1, []), f"yaml-get exits 1 when absent: {rc} {out}")
        rc, out = run(root, "yaml-list-missing", str(cfg), "tools.tool_search.always_include",
                      "session_search", "web_url_read", "searxng_web_search|mcp__searxng__searxng_web_search",
                      "coding_start_task|mcp__opencode__coding_start_task")
        c.expect((rc, out) == (0, ["web_url_read", "coding_start_task"]),
                f"yaml-list-missing with alternatives: {rc} {out}")

        # terminal-backend: default ssh, browser local, worker unset.
        (hermes / "profiles/browser/config.yaml").write_text("terminal:\n  backend: local\n")
        (hermes / "profiles/worker/config.yaml").write_text("model:\n  base_url: http://elsewhere/v1\n")
        rc, out = run(root, "terminal-backend", "ssh")
        c.expect(out == ["browser terminal.backend is local", "worker terminal.backend is unset"],
                f"terminal-backend: {out}")
        with tempfile.TemporaryDirectory() as empty:
            c.expect(run(Path(empty), "terminal-backend", "ssh")[1] == ["NOCONFIG"], "terminal-backend NOCONFIG")

        # proxy-mismatch: default matches; browser has no model; worker points elsewhere.
        (root / "opencode").mkdir()
        (root / "opencode/opencode.local.json").write_text(json.dumps({"provider": {
            "ok": {"options": {"baseURL": "http://llm-proxy:4000/v1"}},
            "direct": {"options": {"baseURL": "http://host.docker.internal:1234/v1"}},
        }}))
        rc, out = run(root, "proxy-mismatch", "http://llm-proxy:4000/v1")
        c.expect(out == ["hermes data/hermes/profiles/browser/config.yaml",
                        "hermes data/hermes/profiles/worker/config.yaml",
                        "opencode provider direct"], f"proxy-mismatch: {out}")

        ov = root / "docker-compose.override.yml"
        ov.write_text(OVERRIDE)
        c.expect(run(root, "override-mounts", str(ov), "hermes", "--host-only")[1] == ["/Users/me/code"],
                "override-mounts --host-only lists absolute binds on hermes only")
        c.expect(run(root, "override-mounts", str(ov), "hermes-worker")[1] == ["/Users/me/code:/workspace/code"],
                "override-mounts lists hermes-worker volumes")
        c.expect(run(root, "override-mounts", str(ov), "absent")[1] == [], "override-mounts: absent service")
        c.expect(run(root, "override-mounts", "-", "hermes", "--host-only", stdin=OVERRIDE)[1] == ["/Users/me/code"],
                 "override-mounts reads stdin for -")

        (hermes / "agents").mkdir()
        c.expect(run(root, "agents")[1] == [], "agents: no registry")
        (hermes / "agents/registry.json").write_text(json.dumps(
            {"agents": [{"name": "scout"}, {"other": 1}, "junk", {"name": "ops"}]}))
        c.expect(run(root, "agents")[1] == ["scout", "ops"], "agents lists named entries")
        (hermes / "agents/registry.json").write_text("{not json")
        c.expect(run(root, "agents") == (0, []), "agents: bad JSON prints nothing")

        reg = root / "registry.json"
        reg.write_text(json.dumps({"corpora": [{"slug": "a", "embedding_dim": 768}, {"slug": "b"}]}))
        c.expect(run(root, "corpus-dim", str(reg), "a")[1] == ["768"], "corpus-dim")
        c.expect(run(root, "corpus-dim", str(reg), "b")[1] == [""], "corpus-dim unset")
        c.expect(run(root, "corpus-dim", str(reg), "z")[1] == [], "corpus-dim unknown slug")

        proj = root / "projects"
        (proj / "here").mkdir(parents=True)
        compose = {"services": {"hermes": {"volumes": [
            {"type": "bind", "target": "/opt/projects/here"},
            {"type": "bind", "target": "/opt/projects/gone"},
            {"type": "volume", "target": "/opt/projects/vol"},
        ]}}}
        rc, out = run(root, "nested-binds", str(proj), stdin=json.dumps(compose))
        c.expect(out == [f"/opt/projects/gone needs {proj}/gone"], f"nested-binds: {out}")
        c.expect(run(root, "nested-binds", str(proj), stdin="not json") == (0, []), "nested-binds: bad JSON")

        c.expect(run(root, "bogus")[0] == 2, "unknown command exits 2")
        c.expect(run(root, "yaml-get", "x")[0] == 2, "wrong arity exits 2")
    c.finish("ok: doctor_checks.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
