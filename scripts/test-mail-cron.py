#!/usr/bin/env python3
"""Checks for check-mail-cron.py against made-up Hermes homes."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_mail_cron", HERE / "check-mail-cron.py")
cmc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmc)

SECRET_PROMPT = "PROMPT-TEXT-MUST-NOT-APPEAR"
SERVERS = """mcp_servers:
  searxng:
    url: http://mcp-searxng:3000/mcp
    timeout: 60
  mail:
    url: http://mail-mcp:8000/mcp
    tools:
      include:
      - mail_status
  drafts:
    url: http://mail-drafts-mcp:8000/mcp
"""


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def home(tmp: Path, name: str, config: str, jobs: list[dict], profile: str | None = None) -> Path:
    root = tmp / name
    path = root / "profiles" / profile if profile else root
    (path / "cron").mkdir(parents=True)
    (path / "config.yaml").write_text(config)
    for i, job in enumerate(jobs):
        job.setdefault("id", f"job{i}")
        job.setdefault("prompt", SECRET_PROMPT)
    (path / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    if profile:
        (root / "config.yaml").write_text("model: x\n")
    return root


def flagged(root: Path) -> set[str]:
    lines = cmc.check(root)
    for line in lines:
        expect(SECRET_PROMPT not in line, "prompts never appear in output")
    return {line.split("'")[1] for line in lines}


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        jobs = [
            {"name": "no list"},
            {"name": "web only", "enabled_toolsets": ["web"]},
            {"name": "mail only", "enabled_toolsets": ["mail"]},
            {"name": "mail and web", "enabled_toolsets": ["mail", "web"]},
            {"name": "mail and drafts", "enabled_toolsets": ["mail", "drafts"]},
            {"name": "names searxng", "enabled_toolsets": ["web", "searxng"]},
            {"name": "no mcp", "enabled_toolsets": ["web", "no_mcp"]},
            {"name": "script only", "no_agent": True},
            {"name": "paused", "enabled": False},
        ]
        got = flagged(home(tmp, "a", SERVERS, [dict(j) for j in jobs]))
        expect(got == {"no list", "web only", "mail and web", "mail and drafts", "paused"}, f"flagged: {got}")
        line = next(l for l in cmc.check(tmp / "a") if "'paused'" in l)
        expect("(paused)" in line, "paused jobs are labelled")

        got = flagged(home(tmp, "b", SERVERS, [dict(j) for j in jobs], profile="browser"))
        expect("no list" in got and "mail only" not in got, f"named profiles are checked too: {got}")

        no_mail = SERVERS.replace("mail-mcp:8000", "elsewhere:1")
        expect(flagged(home(tmp, "c", no_mail, [{"name": "x"}])) == set(), "profiles without mail are skipped")
        off = SERVERS.replace("    url: http://mail-mcp:8000/mcp\n",
                              "    url: http://mail-mcp:8000/mcp\n    enabled: false\n")
        expect(flagged(home(tmp, "d", off, [{"name": "x"}])) == set(), "a disabled mail server doesn't count")

        block = SERVERS + "platform_toolsets:\n  cli:\n  - hermes-cli\n  cron:\n  - hermes-cron\n  - searxng\n"
        got = flagged(home(tmp, "e", block, [{"name": "no list"}, {"name": "web only", "enabled_toolsets": ["web"]}]))
        expect(got == {"web only"}, f"a cron list without mail covers jobs with no list of their own: {got}")
        indented = SERVERS + "platform_toolsets:\n  cron:\n    - hermes-cron\n    - no_mcp\n"
        expect(flagged(home(tmp, "f", indented, [{"name": "x"}])) == set(), "indented list and no_mcp")
        flow = SERVERS + "platform_toolsets:\n  cron: [web]\n"
        expect(flagged(home(tmp, "g", flow, [{"name": "x"}])) == {"x"},
               "a cron list naming no MCP server still gets mail")
        expect(cmc.cron_list(tmp / "e" / "config.yaml") == ["hermes-cron", "searxng"], "block list parsed")
        expect(cmc.cron_list(tmp / "a" / "config.yaml") is None, "no cron list")

        sys.argv = ["check-mail-cron.py", "--hermes-home", str(tmp / "c")]
        expect(cmc.main() == 0, "exit 0 when clean")
        sys.argv = ["check-mail-cron.py", "--hermes-home", str(tmp / "a")]
        expect(cmc.main() == 1, "exit 1 when a job is risky")
    print("OK mail cron check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
