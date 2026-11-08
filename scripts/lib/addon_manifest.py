#!/usr/bin/env python3
"""Read and check add-on manifests (addons/<short-id>/addon.json).

Standard library only: the Hermes bootstrap containers run this with the
image's python3, mounted at /bootstrap/addon_manifest.py.

    addon_manifest.py validate <addons-dir>
        Check every manifest as a built-in add-on. Prints one line per problem
        and exits 1 if there are any.
    addon_manifest.py mcp-plan <addons-dir> <profile>
        For each add-on with an `mcp` block, print one line the bootstrap's
        register_addon_mcp_servers reads, fields separated by "|":
        name|title|enabled|access|url|timeout|connect_timeout|transport|tools|pins
        `enabled` is 1 or 0 from the add-on's flag in the environment. `access`
        is rw, ro, or empty when the profile gets no entry. Tools and pins are
        space-separated. Invalid manifests are reported on stderr and skipped.

Policy follows docs/private/addon-manifest-spec.md (draft). `source` is
"builtin", "official" (a package from the official feed) or "community".
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MANIFEST_VERSION = 1
SOURCES = ("builtin", "official", "community")

# Names core still registers itself. An add-on may not take one, or start
# with one of the prefixes core uses for per-instance entries.
CORE_MCP_NAMES = {
    "searxng", "lightrag", "opencode", "sql", "vikunja", "mail", "drafts",
    "messages",
}
CORE_MCP_PREFIXES = ("caldav-", "crm-")

READ_CLASSES = {"mail", "messages", "contacts", "calendar", "projects", "memory", "sessions"}
EGRESS_WORDS = {"llm-proxy", "search", "postgres", "internet"}
STACK_SERVICES = {"llm", "search", "storage", "postgres", "neo4j", "redis", "secrets", "scheduler"}
SETTING_TYPES = {"string", "int", "bool", "choice", "port", "secret", "url", "path"}
TRANSPORTS = {"sse", "http"}
ACCESS = {"ro", "rw"}
UNATTENDED_PROFILE = "api-server"

REQUIRED = ("manifestVersion", "id", "version", "title", "tagline", "description",
            "publisher", "minStackVersion", "services", "permissions")

ID_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


def short_id(manifest_id: str) -> str:
    return manifest_id.rsplit(".", 1)[-1]


def canonical(manifest: dict) -> str:
    """The only layout allowed in the repo. sync-profiles.sh reads the
    `builtin` block with sed, which needs a fixed layout."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate(manifest, source: str = "builtin") -> list[str]:
    """Return every problem with `manifest` for an add-on from `source`."""
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    errors: list[str] = []
    err = errors.append

    for key in REQUIRED:
        if key not in manifest:
            err(f"missing required field {key!r}")
    if manifest.get("manifestVersion", MANIFEST_VERSION) != MANIFEST_VERSION:
        err(f"manifestVersion must be {MANIFEST_VERSION}")
    mid = manifest.get("id", "")
    if "id" in manifest and not (isinstance(mid, str) and ID_RE.match(mid)):
        err("id must be reverse-domain, e.g. io.example.gptr")
    for key in ("version", "minStackVersion", "maxStackVersion"):
        if key in manifest and not (isinstance(manifest[key], str) and SEMVER_RE.match(manifest[key])):
            err(f"{key} must be semver")
    for key in ("title", "tagline", "description"):
        if key in manifest and not (isinstance(manifest[key], str) and manifest[key].strip()):
            err(f"{key} must be a non-empty string")
    pub = manifest.get("publisher")
    if "publisher" in manifest and not (isinstance(pub, dict) and isinstance(pub.get("name"), str)
                                        and isinstance(pub.get("url"), str)):
        err("publisher must be {name, url}")

    builtin = manifest.get("builtin")
    if source == "builtin":
        if not isinstance(builtin, dict):
            err("a built-in add-on needs a builtin block {flag, profile, seed}")
        else:
            if not (isinstance(builtin.get("flag"), str) and ENV_RE.match(builtin["flag"])):
                err("builtin.flag must be an env var name")
            if not (isinstance(builtin.get("profile"), str) and NAME_RE.match(builtin["profile"])):
                err("builtin.profile must be a compose profile name")
            if not isinstance(builtin.get("seed"), bool):
                err("builtin.seed must be true or false")
            extra = set(builtin) - {"flag", "profile", "seed"}
            if extra:
                err(f"builtin has unknown fields: {', '.join(sorted(extra))}")
    elif builtin is not None:
        err("only built-in add-ons may have a builtin block")

    services = manifest.get("services")
    if "services" in manifest:
        if not (isinstance(services, dict) and services):
            err("services must name at least one service")
            services = {}
        for name, svc in services.items():
            if not NAME_RE.match(name):
                err(f"services.{name}: name must be lowercase letters, digits and -")
            if not isinstance(svc, dict):
                err(f"services.{name} must be an object")
                continue
            if source == "builtin":
                if not (isinstance(svc.get("compose"), str) and svc["compose"]):
                    err(f"services.{name}.compose must name the compose service")
                if "image" in svc:
                    err(f"services.{name}: a built-in add-on keeps its image in docker-compose.yml")
            else:
                image = svc.get("image")
                if not (isinstance(image, str) and DIGEST_RE.search(image)):
                    err(f"services.{name}.image must be referenced by sha256 digest")
                if "compose" in svc:
                    err(f"services.{name}: only built-in add-ons name a compose service")
            if "port" in svc and not (isinstance(svc["port"], int) and 0 < svc["port"] < 65536):
                err(f"services.{name}.port must be a port number")
    else:
        services = {}

    uses = manifest.get("uses", [])
    if not _is_str_list(uses):
        err("uses must be a list of stack service names")
        uses = []
    for u in uses:
        if u not in STACK_SERVICES:
            err(f"uses: unknown stack service {u!r}")

    perms = manifest.get("permissions")
    if "permissions" in manifest and not isinstance(perms, dict):
        err("permissions must be an object")
    perms = perms if isinstance(perms, dict) else {}
    reads = perms.get("reads", [])
    if not _is_str_list(reads):
        err("permissions.reads must be a list")
        reads = []
    for r in reads:
        if r not in READ_CLASSES:
            err(f"permissions.reads: unknown class {r!r}")
    if not _is_str_list(perms.get("writes", [])):
        err("permissions.writes must be a list")
    else:
        for w in perms.get("writes", []):
            if not w.startswith("external:"):
                err(f"permissions.writes: {w!r} must be external:<what>")
    for key in ("sends", "exposesToAgent", "cron"):
        if key in perms and not isinstance(perms[key], bool):
            err(f"permissions.{key} must be true or false")
    egress = perms.get("egress", [])
    if not _is_str_list(egress):
        err("permissions.egress must be a list")
    sends = perms.get("sends", False) is True
    cron = perms.get("cron", False) is True

    if reads:
        if source == "community":
            err("permissions.reads is refused for community feeds")
        if sends:
            err("an add-on that reads personal data may not send; ship the send path as its own add-on")
        if cron:
            err("an add-on that reads personal data needs permissions.cron false")
    if sends and source == "community":
        err("permissions.sends is refused for community feeds")
    if {"postgres", "neo4j"} & set(uses) and perms.get("exposesToAgent") is not True:
        err("uses postgres or neo4j, so permissions.exposesToAgent must be true")

    mcp = manifest.get("mcp")
    if mcp is not None:
        errors.extend(_validate_mcp(mcp, services, source, bool(reads)))

    settings = manifest.get("settings", {})
    if not isinstance(settings, dict):
        err("settings must be an object")
        settings = {}
    for name, s in settings.items():
        if not isinstance(s, dict) or s.get("type") not in SETTING_TYPES:
            err(f"settings.{name}.type must be one of {', '.join(sorted(SETTING_TYPES))}")
            continue
        if s["type"] == "choice":
            opts = s.get("options")
            if not (_is_str_list(opts) and opts):
                err(f"settings.{name}.options must be a non-empty list")
            elif "default" in s and s["default"] not in opts:
                err(f"settings.{name}.default is not one of its options")
        if s["type"] == "path" and reads and "default" in s:
            err(f"settings.{name}: a path setting in an add-on that reads personal data may not have a default")

    if "host" in manifest and source == "community":
        err("a host component is refused unless the add-on is built-in or on the official feed")

    return errors


def _validate_mcp(mcp, services: dict, source: str, reads: bool) -> list[str]:
    errors: list[str] = []
    err = errors.append
    if not isinstance(mcp, dict):
        return ["mcp must be an object"]
    svc = mcp.get("service")
    if svc not in services:
        err("mcp.service must name one of services")
    elif source != "builtin" and not services[svc].get("healthCheck"):
        err(f"services.{svc}.healthCheck is required on the service mcp points at")
    elif source == "builtin" and "port" not in services[svc]:
        err(f"services.{svc}.port is needed to build the MCP URL")
    if mcp.get("transport") not in TRANSPORTS:
        err(f"mcp.transport must be one of {', '.join(sorted(TRANSPORTS))}")
    if not (isinstance(mcp.get("path"), str) and PATH_RE.match(mcp["path"])):
        err("mcp.path must be an absolute URL path")
    name = mcp.get("name")
    if not (isinstance(name, str) and NAME_RE.match(name)):
        err("mcp.name must be lowercase letters, digits and -")
    elif name in CORE_MCP_NAMES or name.startswith(CORE_MCP_PREFIXES):
        err(f"mcp.name {name!r} collides with a server core registers")
    for key in ("timeout", "connectTimeout"):
        if not (isinstance(mcp.get(key), int) and mcp[key] > 0):
            err(f"mcp.{key} must be a positive number of seconds")
    tools = mcp.get("tools")
    if not (_is_str_list(tools) and tools and all(TOOL_RE.match(t) for t in tools)):
        err("mcp.tools must list the tools Hermes may use")
        tools = []
    for key in ("pin", "writeTools"):
        value = mcp.get(key, [])
        if not _is_str_list(value):
            err(f"mcp.{key} must be a list")
            continue
        stray = [t for t in value if t not in tools]
        if stray:
            err(f"mcp.{key} names tools not in mcp.tools: {', '.join(stray)}")
    profiles = mcp.get("profiles")
    if not (isinstance(profiles, dict) and profiles):
        err("mcp.profiles must map at least one Hermes profile to ro or rw")
        profiles = {}
    for prof, level in profiles.items():
        if level not in ACCESS:
            err(f"mcp.profiles.{prof} must be ro or rw")
    if reads and UNATTENDED_PROFILE in profiles:
        err(f"an add-on that reads personal data may not be on {UNATTENDED_PROFILE}")
    env = mcp.get("env", {})
    if env:
        if source != "builtin":
            err("mcp.env is for built-in add-ons only")
        elif not isinstance(env, dict) or set(env) - {"url", "timeout", "connectTimeout"} \
                or not all(isinstance(v, str) and ENV_RE.match(v) for v in env.values()):
            err("mcp.env maps url, timeout and connectTimeout to env var names")
    if mcp.get("instances") is not None:
        err("mcp.instances is not implemented yet")
    return errors


def load_dir(addons_dir: Path) -> list[tuple[Path, object, str | None]]:
    """(path, manifest or None, parse error or None) for each addon.json."""
    out = []
    for path in sorted(Path(addons_dir).glob("*/addon.json")):
        try:
            out.append((path, json.loads(path.read_text(encoding="utf-8")), None))
        except (OSError, ValueError) as exc:
            out.append((path, None, f"cannot parse: {exc}"))
    return out


def check_builtin_file(path: Path, manifest, parse_error: str | None) -> list[str]:
    """validate() plus the repo-only rules: canonical layout, dir name."""
    if parse_error:
        return [parse_error]
    errors = validate(manifest, "builtin")
    if isinstance(manifest, dict) and isinstance(manifest.get("id"), str):
        if path.parent.name != short_id(manifest["id"]):
            errors.append(f"directory must be named after the short id {short_id(manifest['id'])!r}")
    if not errors and path.read_text(encoding="utf-8") != canonical(manifest):
        errors.append("not canonical JSON (json.dumps(indent=2) plus a newline)")
    return errors


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def mcp_plan(addons_dir: Path, profile: str, environ=os.environ) -> list[str]:
    lines = []
    for path, manifest, parse_error in load_dir(addons_dir):
        errors = check_builtin_file(path, manifest, parse_error)
        if errors:
            for e in errors:
                print(f"warning: skipping {path.parent.name}: {e}", file=sys.stderr)
            continue
        mcp = manifest.get("mcp")
        if not mcp:
            continue
        svc = manifest["services"][mcp["service"]]
        env = mcp.get("env", {})
        url = environ.get(env.get("url", ""), "") or f"http://{svc['compose']}:{svc['port']}{mcp['path']}"
        timeout = environ.get(env.get("timeout", ""), "") or str(mcp["timeout"])
        connect = environ.get(env.get("connectTimeout", ""), "") or str(mcp["connectTimeout"])
        enabled = "1" if truthy(environ.get(manifest["builtin"]["flag"])) else "0"
        access = mcp["profiles"].get(profile, "")
        fields = [mcp["name"], manifest["title"], enabled, access, url, timeout, connect,
                  mcp["transport"], " ".join(mcp["tools"]), " ".join(mcp.get("pin", []))]
        if any("|" in f or "\n" in f for f in fields):
            print(f"warning: skipping {path.parent.name}: a value contains | or a newline",
                  file=sys.stderr)
            continue
        lines.append("|".join(fields))
    return lines


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "validate":
        bad = 0
        for path, manifest, parse_error in load_dir(Path(argv[1])):
            for e in check_builtin_file(path, manifest, parse_error):
                print(f"{path}: {e}")
                bad += 1
        return 1 if bad else 0
    if len(argv) == 3 and argv[0] == "mcp-plan":
        for line in mcp_plan(Path(argv[1]), argv[2]):
            print(line)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
