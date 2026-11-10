#!/usr/bin/env python3
"""Connected Actual budgets ("rows") as Hermes MCP entries.

    actual_rows.py plan INSTANCES_DIR ADDONS_DIR PROFILE

INSTANCES_DIR holds one <slug>.json per connected budget, written by addon.sh
connect (string fields server, sync_id, profile, access). Prints one line per
MCP entry PROFILE gets:

    name|title|url|timeout|connect_timeout|tools|pins

budget-<slug>-ro on http://actual-mcp-ro:3000/<slug>/mcp with the manifest's
read tools, and for a write row budget-<slug> on
http://actual-mcp-rw:3000/<slug>/mcp with every tool. Only rows whose profile
is default appear, and only on the default and browser profiles. A row the
actual-mcp launcher would skip (bad slug, access, server or sync_id) is
skipped here too, with one stderr line naming it; the launcher also skips a
row with no session token, which this never reads.

Tool lists, pins and timeouts come from addons/actual/addon.json through
addon_manifest. With no Actual manifest it prints nothing; with an invalid
one it exits 1, so the bootstrap leaves the entries as they were.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import addon_manifest  # noqa: E402

ACTUAL_DIR = "actual"
# addon_store.SLUG_RE: a lowercase letter, then up to 30 of a-z, 0-9 and -.
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
# Hermes profiles that get the default profile's rows.
ROW_PROFILES = {"default": "default", "browser": "default"}
RO_URL = "http://actual-mcp-ro:3000/{slug}/mcp"
RW_URL = "http://actual-mcp-rw:3000/{slug}/mcp"


def warn(slug: str, reason: str) -> None:
    print(f"warning: skipping Actual row {slug}: {reason}", file=sys.stderr)


def actual_mcp(addons_dir: Path) -> tuple[dict, dict] | None:
    """(manifest, its mcp entry), None without an Actual manifest. Raises
    ValueError when the manifest is invalid."""
    for path, manifest, errors in addon_manifest.checked_dir(addons_dir):
        if path.parent.name != ACTUAL_DIR:
            continue
        if errors:
            raise ValueError("; ".join(errors))
        entries = [m for m in addon_manifest.mcp_entries(manifest) if "instances" not in m]
        if len(entries) != 1:
            raise ValueError("the Actual manifest needs exactly one mcp entry")
        return manifest, entries[0]
    return None


def is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def rows(instances_dir: Path) -> list[tuple[str, str, str]]:
    """(slug, profile, access) for each row the launcher would serve."""
    try:
        names = sorted(p.name for p in instances_dir.iterdir() if p.name.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        slug = name[: -len(".json")]
        if not SLUG_RE.match(slug):
            warn(slug, "invalid slug")
            continue
        try:
            fields = json.loads((instances_dir / name).read_text(encoding="utf-8"))
        except OSError as exc:
            warn(slug, f"cannot read the instance file ({exc.strerror or type(exc).__name__})")
            continue
        except ValueError:
            warn(slug, "the instance file is not a JSON object")
            continue
        if not isinstance(fields, dict):
            warn(slug, "the instance file is not a JSON object")
            continue
        access = fields.get("access", "read")
        if access not in ("read", "write"):
            warn(slug, "access must be read or write")
            continue
        server = fields.get("server")
        if not (server == "local" or (isinstance(server, str) and is_http_url(server))):
            warn(slug, 'server must be an http(s) URL or "local"')
            continue
        sync_id = fields.get("sync_id")
        if not isinstance(sync_id, str) or not sync_id.strip():
            warn(slug, "missing sync_id")
            continue
        profile = fields.get("profile", "default")
        if profile != "default":
            warn(slug, "profile must be default in this build")
            continue
        out.append((slug, profile, access))
    return out


def plan(instances_dir: Path, addons_dir: Path, profile: str) -> list[str]:
    found = actual_mcp(addons_dir)
    if found is None:
        return []
    manifest, mcp = found
    row_profile = ROW_PROFILES.get(profile or "default")
    if row_profile is None:
        return []
    write_tools = set(mcp.get("writeTools", []))
    all_tools = list(mcp["tools"])
    read_tools = [t for t in all_tools if t not in write_tools]
    pins = list(mcp.get("pin", []))
    read_pins = [t for t in pins if t not in write_tools]
    title = mcp.get("title", manifest["title"])
    timeout, connect = str(mcp["timeout"]), str(mcp["connectTimeout"])
    lines = []
    for slug, row_owner, access in rows(instances_dir):
        if row_owner != row_profile:
            continue
        lines.append("|".join([f"budget-{slug}-ro", f"{title} '{slug}' (read-only)",
                               RO_URL.format(slug=slug), timeout, connect,
                               " ".join(read_tools), " ".join(read_pins)]))
        if access == "write":
            lines.append("|".join([f"budget-{slug}", f"{title} '{slug}'",
                                   RW_URL.format(slug=slug), timeout, connect,
                                   " ".join(all_tools), " ".join(pins)]))
    return lines


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "plan":
        try:
            lines = plan(Path(argv[1]), Path(argv[2]), argv[3])
        except ValueError as exc:
            print(f"error: invalid Actual manifest: {exc}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
