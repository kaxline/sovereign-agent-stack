#!/usr/bin/env python3
"""Warn about Hermes cron jobs that can read mail or messages alongside other tools.

    python3 scripts/check-mail-cron.py [--hermes-home data/hermes]

Cron runs unattended. A job that can read mail (untrusted text) and also
reach the web, a browser, a shell or another MCP server could be steered by a
message into leaking mail. The same goes for iMessage history (the messages
MCP). Only an allowlist of mail and/or messages alone is safe; see
docs/email.md and docs/messages.md. This mirrors Hermes's rule for a job's tools: with no
`enabled_toolsets`, or with an allowlist that names no MCP server, a job gets
every enabled MCP server, mail included. A job with no list of its own uses
`platform_toolsets.cron` when the profile has one (the bootstrap writes one
without mail; see fence_mail_from_cron).

Reads each profile's config.yaml (MCP server names only) and cron/jobs.json
(names and tool settings only, never prompts). Prints one line per risky job
and exits 1 if there are any, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# MCP servers that serve other people's text. A job may read any of them, but
# nothing else beside them.
READERS = {"mail-mcp:8000": "mail", "messages-mcp:8000": "messages"}
SAFE_NATIVE: set[str] = set()  # native toolsets allowed beside them: none


def mcp_servers(config: Path) -> dict[str, dict]:
    """{name: {"url": str, "enabled": bool}} from the top-level mcp_servers block."""
    servers: dict[str, dict] = {}
    try:
        lines = config.read_text().splitlines()
    except OSError:
        return servers
    inside = False
    current = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.rstrip() == "mcp_servers:"
            current = None
            continue
        if not inside:
            continue
        m = re.match(r"^  ([^\s:#][^:]*):\s*$", line)
        if m:
            current = m.group(1).strip().strip("'\"")
            servers[current] = {"url": "", "enabled": True}
            continue
        m = re.match(r"^    (url|enabled):\s*(.*?)\s*$", line)
        if m and current:
            value = m.group(2).strip("'\"")
            if m.group(1) == "url":
                servers[current]["url"] = value
            else:
                servers[current]["enabled"] = value.lower() not in ("false", "no", "off", "0")
    return servers


def cron_list(config: Path) -> list[str] | None:
    """platform_toolsets.cron as a list, or None when it isn't set."""
    try:
        lines = config.read_text().splitlines()
    except OSError:
        return None
    section = key = None
    items: list[str] | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            section = line.rstrip().rstrip(":")
            key = None
            continue
        if section != "platform_toolsets":
            continue
        m = re.match(r"^  ([^\s:-][^:]*):\s*(.*?)\s*$", line)
        if m:
            key = m.group(1).strip()
            if key == "cron":
                flow = m.group(2)
                if flow.startswith("["):
                    return [t.strip().strip("'\"") for t in flow.strip("[]").split(",") if t.strip()]
                items = []
            continue
        m = re.match(r"^\s+-\s*(.+?)\s*$", line)
        if m and key == "cron" and items is not None:
            items.append(m.group(1).strip("'\""))
    return items


def merge(names: list[str], enabled_mcp: set[str]) -> tuple[set[str], set[str]]:
    """Hermes's rule: listed MCP servers are an allowlist, none listed means all, no_mcp means none."""
    native = {t for t in names if t not in enabled_mcp and t != "no_mcp"}
    if "no_mcp" in names:
        return set(), native
    listed = {t for t in names if t in enabled_mcp}
    return (listed or set(enabled_mcp)), native


def effective(job: dict, enabled_mcp: set[str],
              platform: list[str] | None = None) -> tuple[set[str], set[str] | None]:
    """(MCP servers, native toolsets or None for "the profile's defaults") for one job."""
    per_job = job.get("enabled_toolsets")
    if not per_job:
        if platform:
            return merge([str(t) for t in platform], enabled_mcp)
        return set(enabled_mcp), None
    return merge([str(t) for t in per_job], enabled_mcp)


def check(home: Path) -> list[str]:
    problems = []
    profiles = [("default", home)] + sorted(
        (p.name, p) for p in (home / "profiles").glob("*") if p.is_dir())
    for label, path in profiles:
        servers = mcp_servers(path / "config.yaml")
        enabled_mcp = {n for n, s in servers.items() if s["enabled"]}
        kind = {n: k for n in enabled_mcp for url, k in READERS.items() if url in servers[n]["url"]}
        mail = set(kind)
        if not mail:
            continue
        platform = cron_list(path / "config.yaml")
        try:
            data = json.loads((path / "cron" / "jobs.json").read_text())
        except (OSError, ValueError):
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        for job in jobs:
            if not isinstance(job, dict) or job.get("no_agent"):
                continue
            mcp, native = effective(job, enabled_mcp, platform)
            if not mcp & mail:
                continue
            others = sorted(mcp - mail)
            if native is None:
                others.append("the profile's default tools")
            else:
                others += sorted(native - SAFE_NATIVE)
            if others:
                paused = "" if job.get("enabled", True) else " (paused)"
                reads = " and ".join(sorted({kind[n] for n in mcp & mail}))
                problems.append(f"{label}: cron job {job.get('id')} '{job.get('name')}'{paused} can read {reads}"
                                f" alongside {', '.join(others)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-home", default="data/hermes", type=Path)
    problems = check(parser.parse_args().hermes_home)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
