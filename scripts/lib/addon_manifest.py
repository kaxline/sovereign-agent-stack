#!/usr/bin/env python3
"""Read and check add-on manifests (addons/<short-id>/addon.json).

Standard library only: the Hermes bootstrap containers run this with the
image's python3, mounted at /bootstrap/addon_manifest.py.

    addon_manifest.py validate <addons-dir>
        Check every manifest as a built-in add-on. Prints one line per problem
        and exits 1 if there are any.
    addon_manifest.py mcp-urls <addons-dir>
        Print every MCP URL of every valid built-in add-on, one per line,
        whatever the flag, `profiles` or `unmanaged` say (mcp.env.url applies).
        For tuning entries that already exist; registers nothing.
    addon_manifest.py mcp-plan <addons-dir> <profile>
        For each entry of each add-on's `mcp` block (one object, or a list of
        objects), print one line, in list order, that the bootstrap's
        register_addon_mcp_servers reads, fields separated by "|":
        name|title|enabled|access|url|timeout|connect_timeout|transport|tools|pins|all_urls
        `title` is the entry's own "title", else the manifest's. `enabled` is 1
        or 0 from the flag in the environment: the entry's `when.gate` flag when
        it has one, else builtin.flag. `access`
        is rw, ro, or empty when the profile gets no entry. Tools, pins and
        all_urls are space-separated. `mcp.service` is one service key or a map
        {"ro": key, "rw": key} that gives each access level its own service;
        `url` is then the one for this profile's access (the rw URL, else ro,
        when the profile gets no entry) and `all_urls` lists every URL the
        add-on can register, sorted, so the bootstrap can drop the ones this
        profile must not have. Invalid manifests are reported on stderr and
        skipped. mcp.env.access names a variable that caps access: "read" turns
        rw into ro, "write" or empty changes nothing, and any other value gives
        the profile no entry. For a profile whose access is ro, tools and pins leave out
        the manifest's writeTools.
    addon_manifest.py vault-plan <addons-dir> <profile>
        For each add-on with a `vaultBackend` block, print one line the
        bootstrap reads, fields separated by "|":
        name|enabled|access|plugin|tools
        `enabled` and `access` are as for mcp-plan. `plugin` is the repo-relative
        directory of the Hermes plugin. Tools are space-separated. Invalid
        manifests, and values containing | or a newline, are reported on stderr
        and skipped.

A built-in manifest may list `gates`, each {flag, profile, seed}: one more
sync-profiles.sh row, so a second flag can switch on part of an add-on. An
`mcp` entry or a `services` entry names one with "when": {"gate": FLAG}.

cron_fence() lists the MCP server names and URLs the bootstrap's
fence_mail_from_cron keeps out of platform_toolsets.cron: every MCP entry of
every add-on whose permissions.cron is not true. With cron "ro" the add-on's name is not fenced and
only its URLs other than the ro service's are, so a cron job keeps the ro
entry and loses the rw one. An invalid manifest is fenced by whatever MCP
name it carries, so a broken manifest can't open cron. cron_fence_toolsets()
does the same for the plugin toolsets of add-ons with a vaultBackend.

Settings (and the schema of `instances`, {name, settings}) take the types in
SETTING_TYPES. A setting with neither `default` nor `optional: true` is
required; a secret setting may not have a default; a bool setting may name a
`flag`, which must be a gate flag. Names that map to the same
ADDON_<SHORT>_<NAME> key (differing only by case, - or _) are refused. Two
`mcp` entries may not point at the same service. `services.<name>.mounts` and
`commands` are checked as validate() describes them. A built-in service may
list `volumes`, the compose volume names `scripts/addon.sh disable --purge`
removes; scripts/test-addon-manifests.py checks they exist in docker-compose.yml.

Policy follows docs/private/addon-manifest-spec.md (draft). `source` is
"builtin", "official" (a package from the official feed) or "community".
"""

from __future__ import annotations

import datetime
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
    "searxng", "opencode", "sql", "vikunja", "mail", "drafts",
    "messages",
}
CORE_MCP_PREFIXES = ("caldav-", "crm-")
# Names of the login backends core ships; an add-on's vaultBackend may not take one.
CORE_VAULT_NAMES = {"vault", "op", "bw"}

READ_CLASSES = {"mail", "messages", "contacts", "calendar", "projects", "memory", "sessions",
                "credentials"}
EGRESS_WORDS = {"llm-proxy", "search", "postgres", "internet"}
STACK_SERVICES = {"llm", "search", "storage", "postgres", "neo4j", "redis", "secrets", "scheduler"}
SETTING_TYPES = {"string", "int", "bool", "choice", "port", "secret", "url", "path", "list", "date"}
# Setting names the stack fills in itself; a schema may not declare them.
RESERVED_SETTINGS = {"slug"}
# services.<name>.mounts keys and the fixed container path each one names.
MOUNT_PATHS = {"secrets": "/run/addon/secrets", "config": "/run/addon/config",
               "instances": "/run/addon/instances"}
MOUNT_MODES = {"ro", "rw"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRANSPORTS = {"sse", "http"}
ACCESS = {"ro", "rw"}
UNATTENDED_PROFILE = "api-server"

REQUIRED = ("manifestVersion", "id", "version", "title", "tagline", "description",
            "publisher", "minStackVersion", "services", "permissions")

ID_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VAULT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
COMPOSE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


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

    gates = manifest.get("gates")
    gate_flags: set[str] = set()
    if gates is not None:
        if source != "builtin":
            err("only built-in add-ons may have gates")
        elif not (isinstance(gates, list) and gates):
            err("gates must be a non-empty list of {flag, profile, seed}")
        else:
            for i, g in enumerate(gates):
                if not isinstance(g, dict):
                    err(f"gates[{i}] must be an object {{flag, profile, seed}}")
                    continue
                if not (isinstance(g.get("flag"), str) and ENV_RE.match(g["flag"])):
                    err(f"gates[{i}].flag must be an env var name")
                elif g["flag"] in gate_flags or (isinstance(builtin, dict) and g["flag"] == builtin.get("flag")):
                    err(f"gates[{i}].flag {g['flag']!r} is already used by this add-on")
                else:
                    gate_flags.add(g["flag"])
                if not (isinstance(g.get("profile"), str) and NAME_RE.match(g["profile"])):
                    err(f"gates[{i}].profile must be a compose profile name")
                if not isinstance(g.get("seed"), bool):
                    err(f"gates[{i}].seed must be true or false")
                extra = set(g) - {"flag", "profile", "seed"}
                if extra:
                    err(f"gates[{i}] has unknown fields: {', '.join(sorted(extra))}")
                elif list(g) != ["flag", "profile", "seed"] and set(g) == {"flag", "profile", "seed"}:
                    err(f"gates keys must be in the order flag, profile, seed (gates[{i}]); sync-profiles.sh reads them line by line")

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
            if "when" in svc:
                errors.extend(_validate_when(svc["when"], gate_flags, f"services.{name}.when"))
            if "port" in svc and not (isinstance(svc["port"], int) and 0 < svc["port"] < 65536):
                err(f"services.{name}.port must be a port number")
            if "volumes" in svc:
                vols = svc["volumes"]
                if source != "builtin":
                    err(f"services.{name}.volumes: only built-in add-ons name compose volumes")
                elif not (_is_str_list(vols) and vols and all(COMPOSE_NAME_RE.match(v) for v in vols)
                          and len(set(vols)) == len(vols)):
                    err(f"services.{name}.volumes must be a non-empty list of distinct compose volume names")
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
    for key in ("sends", "exposesToAgent"):
        if key in perms and not isinstance(perms[key], bool):
            err(f"permissions.{key} must be true or false")
    if "cron" in perms and not (isinstance(perms["cron"], bool) or perms["cron"] == "ro"):
        err('permissions.cron must be true, false or "ro"')
    egress = perms.get("egress", [])
    if not _is_str_list(egress):
        err("permissions.egress must be a list")
    sends = perms.get("sends", False) is True
    cron = perms.get("cron", False) in (True, "ro")

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
        if isinstance(mcp, list) and not mcp:
            err("mcp must be an object or a non-empty list of objects")
        entries = mcp if isinstance(mcp, list) else [mcp]
        for entry in entries:
            errors.extend(_validate_mcp(entry, services, source, bool(reads), gate_flags))
            if perms.get("cron") == "ro" and not (
                    isinstance(entry, dict) and isinstance(entry.get("service"), dict)
                    and "ro" in entry["service"]):
                err('permissions.cron "ro" needs a mcp.service map with an ro service: '
                    'cron ro needs an ro service')
        names = [e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)]
        if len(names) != len(set(names)):
            err("mcp names must be unique within an add-on")
        # The bootstrap drops stale entries by URL, so two entries on one
        # service would drop each other's live registration.
        keys = [_mcp_service_keys(e) for e in entries]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for shared in sorted(keys[i] & keys[j]):
                    err(f"mcp entries {i} and {j} share service {shared!r}; each entry needs its own")

    vault = manifest.get("vaultBackend")
    if vault is not None:
        errors.extend(_validate_vault_backend(vault, source, bool(reads)))

    # A setting's flag names a gate: configure must not be able to switch
    # builtin.flag, which only enable and disable write.
    flags = set(gate_flags)
    settings = manifest.get("settings", {})
    if not isinstance(settings, dict):
        err("settings must be an object")
        settings = {}
    errors.extend(_validate_settings(settings, source=source, reads=bool(reads), flags=flags,
                                     in_instance=False))

    instances = manifest.get("instances")
    if instances is not None:
        if not isinstance(instances, dict):
            err("instances must be an object {name, settings}")
        else:
            iname = instances.get("name")
            if not (isinstance(iname, str) and NAME_RE.match(iname)):
                err("instances.name must be lowercase letters, digits and -")
            isettings = instances.get("settings")
            if not (isinstance(isettings, dict) and isettings):
                err("instances.settings must declare at least one setting")
            else:
                errors.extend(_validate_settings(isettings, source=source, reads=bool(reads),
                                                 flags=flags, in_instance=True))
            extra = set(instances) - {"name", "settings"}
            if extra:
                err(f"instances has unknown fields: {', '.join(sorted(extra))}")

    mcp_services: set[str] = set()
    for entry in mcp_entries(manifest) if isinstance(mcp, (dict, list)) else []:
        mcp_services |= _mcp_service_keys(entry)
    for name, svc in services.items():
        if not isinstance(svc, dict) or "mounts" not in svc:
            continue
        mounts = svc["mounts"]
        if not (isinstance(mounts, dict) and mounts and set(mounts) <= set(MOUNT_PATHS)
                and all(isinstance(v, str) and v in MOUNT_MODES for v in mounts.values())):
            err(f"services.{name}.mounts must map secrets, config and/or instances to ro or rw")
        elif "secrets" in mounts and name in mcp_services:
            err(f"services.{name} holds secrets, so no mcp entry may point at it")

    commands = manifest.get("commands")
    if commands is not None:
        if not isinstance(commands, dict):
            err("commands must be an object of name to {service, instance, argv}")
            commands = {}
        for cname, cmd in commands.items():
            if not NAME_RE.match(cname):
                err(f"commands.{cname}: name must be lowercase letters, digits and -")
            if not isinstance(cmd, dict):
                err(f"commands.{cname} must be an object {{service, instance, argv}}")
                continue
            if not (isinstance(cmd.get("service"), str) and cmd["service"] in services):
                err(f"commands.{cname}.service must name one of services")
            elif reads and cmd["service"] in mcp_services:
                err(f"commands.{cname}: command runs in an MCP service, which an add-on "
                    "that reads personal data may not do")
            if not isinstance(cmd.get("instance"), bool):
                err(f"commands.{cname}.instance must be true or false")
            argv = cmd.get("argv")
            if not (_is_str_list(argv) and argv):
                err(f"commands.{cname}.argv must be a non-empty list of strings")
            extra = set(cmd) - {"service", "instance", "argv"}
            if extra:
                err(f"commands.{cname} has unknown fields: {', '.join(sorted(extra))}")

    if "host" in manifest and source == "community":
        err("a host component is refused unless the add-on is built-in or on the official feed")

    return errors


def _validate_settings(settings: dict, *, source: str, reads: bool, flags: set[str],
                       in_instance: bool) -> list[str]:
    """Check a settings schema (top-level, or an instance's). A setting with
    neither `default` nor `optional: true` is required. Messages name the
    setting, never a value."""
    errors: list[str] = []
    err = errors.append
    where = "instances.settings" if in_instance else "settings"
    env_names: dict[str, str] = {}
    for name, s in settings.items():
        at = f"{where}.{name}"
        if name in RESERVED_SETTINGS:
            err(f"{at}: {name!r} is reserved")
        # addon_store keys settings.env (and names secret files) by this form.
        key = name.upper().replace("-", "_")
        if key in env_names:
            err(f"{at}: same settings.env key as {env_names[key]!r}; names may not differ only "
                "by case, - or _")
        else:
            env_names[key] = name
        if not isinstance(s, dict) or not (isinstance(s.get("type"), str) and s["type"] in SETTING_TYPES):
            err(f"{at}.type must be one of {', '.join(sorted(SETTING_TYPES))}")
            continue
        t = s["type"]
        if "label" in s and not (isinstance(s["label"], str) and s["label"].strip()):
            err(f"{at}.label must be a non-empty string")
        if "optional" in s and not isinstance(s["optional"], bool):
            err(f"{at}.optional must be true or false")
        if t == "choice":
            opts = s.get("options")
            if not (_is_str_list(opts) and opts):
                err(f"{at}.options must be a non-empty list")
            elif "default" in s and s["default"] not in opts:
                err(f"{at}.default is not one of its options")
        if t == "path" and reads and "default" in s:
            err(f"{at}: a path setting in an add-on that reads personal data may not have a default")
        if t == "secret" and source not in ("builtin", "official"):
            err(f"{at}: secret settings are refused unless the add-on is built-in or on the official feed")
        if t == "secret" and "default" in s:
            err(f"{at}: a secret setting may not have a default")
        if "default" in s and t != "choice" and not _default_fits(t, s["default"]):
            err(f"{at}.default does not match type {t}")
        if "flag" in s:
            if in_instance:
                err(f"{at}.flag is refused inside an instance")
            elif t != "bool":
                err(f"{at}.flag is for bool settings only")
            elif not (isinstance(s["flag"], str) and ENV_RE.match(s["flag"])):
                err(f"{at}.flag must be an env var name")
            elif s["flag"] not in flags:
                err(f"{at}.flag must name a flag listed in gates")
    return errors


def _default_fits(t: str, value) -> bool:
    if t in ("string", "url", "path", "secret"):
        return isinstance(value, str)
    if t in ("int", "port"):
        return isinstance(value, int) and not isinstance(value, bool) and (
            t == "int" or 0 < value < 65536)
    if t == "bool":
        return isinstance(value, bool)
    if t == "list":
        return _is_str_list(value)
    if t == "date":
        if not (isinstance(value, str) and DATE_RE.match(value)):
            return False
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return True


def _mcp_service_keys(entry) -> set[str]:
    """The service keys an mcp entry points at: a string key, or the values
    of an access-level map."""
    svc = entry.get("service") if isinstance(entry, dict) else None
    if isinstance(svc, str):
        return {svc}
    if isinstance(svc, dict):
        return {v for v in svc.values() if isinstance(v, str)}
    return set()


def _validate_when(when, gate_flags: set[str], where: str) -> list[str]:
    if not (isinstance(when, dict) and set(when) == {"gate"}):
        return [f'{where} must be {{"gate": FLAG}}']
    if when["gate"] not in gate_flags:
        return [f"{where}.gate {when['gate']!r} is not a flag listed in gates"]
    return []


def _validate_mcp(mcp, services: dict, source: str, reads: bool,
                  gate_flags: set[str] = frozenset()) -> list[str]:
    errors: list[str] = []
    err = errors.append
    if not isinstance(mcp, dict):
        return ["mcp must be an object"]
    if "title" in mcp and not (isinstance(mcp["title"], str) and mcp["title"].strip()):
        err("mcp.title must be a non-empty string")
    if "when" in mcp:
        errors.extend(_validate_when(mcp["when"], gate_flags, "mcp.when"))
    svc = mcp.get("service")
    levels: set[str] = set()  # access levels that have a service
    if isinstance(svc, str):
        keys = [svc]
        if svc in services:
            levels = set(ACCESS)
    elif isinstance(svc, dict) and svc and set(svc) <= ACCESS \
            and all(isinstance(v, str) for v in svc.values()):
        keys = list(svc.values())
        levels = set(svc)
    else:
        keys = []
        err("mcp.service must name one of services, or map ro and/or rw to one")
    for key in keys:
        if key not in services:
            err("mcp.service must name one of services")
        elif source != "builtin" and not services[key].get("healthCheck"):
            err(f"services.{key}.healthCheck is required on the service mcp points at")
        elif source == "builtin" and "port" not in services[key]:
            err(f"services.{key}.port is needed to build the MCP URL")
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
        elif isinstance(svc, dict) and level not in levels:
            err(f"mcp.profiles.{prof} is {level}, but mcp.service has no service for {level}")
    if reads and UNATTENDED_PROFILE in profiles:
        err(f"an add-on that reads personal data may not be on {UNATTENDED_PROFILE}")
    if "unmanaged" in mcp:
        unmanaged = mcp["unmanaged"]
        if source != "builtin":
            err("mcp.unmanaged is for built-in add-ons only")
        elif not (_is_str_list(unmanaged) and all(NAME_RE.match(n) for n in unmanaged)
                  and len(set(unmanaged)) == len(unmanaged)):
            err("mcp.unmanaged must be a list of distinct Hermes profile names")
        elif set(unmanaged) & set(profiles):
            err("mcp.unmanaged may not name a profile in mcp.profiles")
    env = mcp.get("env", {})
    if env:
        if source != "builtin":
            err("mcp.env is for built-in add-ons only")
        elif not isinstance(env, dict) or set(env) - {"url", "timeout", "connectTimeout", "access"} \
                or not all(isinstance(v, str) and ENV_RE.match(v) for v in env.values()):
            err("mcp.env maps url, timeout, connectTimeout and access to env var names")
    if mcp.get("instances") is not None:
        err("mcp.instances is not implemented yet")
    return errors


def _validate_vault_backend(vb, source: str, reads: bool) -> list[str]:
    errors: list[str] = []
    err = errors.append
    if source == "community":
        err("vaultBackend is refused unless the add-on is built-in or on the official feed")
    if not isinstance(vb, dict):
        errors.append("vaultBackend must be an object")
        return errors
    name = vb.get("name")
    if not (isinstance(name, str) and VAULT_NAME_RE.match(name)):
        err("vaultBackend.name must be lowercase letters, digits and _")
    elif name in CORE_VAULT_NAMES:
        err(f"vaultBackend.name is reserved: {name!r} belongs to a backend core ships")
    plugin = vb.get("plugin")
    if not (isinstance(plugin, str) and plugin and not plugin.startswith("/")
            and ".." not in plugin.split("/") and "\\" not in plugin):
        err("vaultBackend.plugin must be a relative directory path without ..")
    toolset = vb.get("toolset")
    if not (isinstance(toolset, str) and VAULT_NAME_RE.match(toolset)):
        err("vaultBackend.toolset must name the plugin's toolset in lowercase letters, digits and _")
    tools = vb.get("tools")
    if not (_is_str_list(tools) and tools and all(TOOL_RE.match(t) for t in tools)):
        err("vaultBackend.tools must list the tools the plugin registers")
    profiles = vb.get("profiles")
    if not (isinstance(profiles, dict) and profiles):
        err("vaultBackend.profiles must map at least one Hermes profile to ro or rw")
        profiles = {}
    for prof, level in profiles.items():
        if level not in ACCESS:
            err(f"vaultBackend.profiles.{prof} must be ro or rw")
    if reads and UNATTENDED_PROFILE in profiles:
        err(f"an add-on that reads personal data may not be on {UNATTENDED_PROFILE}")
    extra = set(vb) - {"name", "plugin", "toolset", "tools", "profiles"}
    if extra:
        err(f"vaultBackend has unknown fields: {', '.join(sorted(extra))}")
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


def check_dir(addons_dir: Path) -> list[str]:
    """Rules across the set of built-in manifests: two add-ons may not
    register the same MCP server name."""
    owners: dict[str, list[str]] = {}
    for path, manifest, _err in load_dir(addons_dir):
        raw = manifest.get("mcp") if isinstance(manifest, dict) else None
        names = {e["name"] for e in (raw if isinstance(raw, list) else [raw])
                 if isinstance(e, dict) and isinstance(e.get("name"), str)}
        for name in names:
            owners.setdefault(name, []).append(path.parent.name)
    return [f"mcp.name {name!r} is used by more than one add-on: {', '.join(dirs)}"
            for name, dirs in sorted(owners.items()) if len(dirs) > 1]


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def mcp_entries(manifest: dict) -> list[dict]:
    """The manifest's mcp entries: `[mcp]` for an object, the list as is."""
    mcp = manifest.get("mcp")
    if isinstance(mcp, list):
        return mcp
    return [mcp] if mcp else []


def _mcp_urls(manifest: dict, mcp: dict, environ) -> dict[str, str]:
    """Access level -> URL. A single service gives {"": url}, which
    mcp.env.url may override; a service map gives one URL per level and
    ignores mcp.env.url."""
    services = manifest["services"]

    def build(key: str) -> str:
        return f"http://{services[key]['compose']}:{services[key]['port']}{mcp['path']}"

    if isinstance(mcp["service"], dict):
        return {level: build(key) for level, key in mcp["service"].items()}
    override = environ.get(mcp.get("env", {}).get("url", ""), "")
    return {"": override or build(mcp["service"])}


def _raw_mcp_urls(manifest, environ) -> list[tuple[dict, list[str]]]:
    """(raw mcp entry, URLs) for each entry of a manifest that failed
    validation and still names a URL. Reads the manifest defensively: nothing
    is assumed about its shape, and a value that is not usable is skipped."""
    out = []
    if not isinstance(manifest, dict):
        return out
    services = manifest.get("services")
    raw = manifest.get("mcp")
    if not isinstance(services, dict):
        return out
    for entry in raw if isinstance(raw, list) else [raw]:
        if not isinstance(entry, dict):
            continue
        try:
            path = entry.get("path")
            svc = entry.get("service")
            if not isinstance(path, str):
                continue

            def build(key) -> str:
                s = services[key]
                host, port = s["compose"], s["port"]
                if not isinstance(host, str) or not isinstance(port, (int, str)) or not host:
                    raise ValueError("bad service")
                return f"http://{host}:{port}{path}"

            if isinstance(svc, dict):
                urls = [build(key) for key in svc.values()]
            elif isinstance(svc, str):
                env = entry.get("env")
                var = env.get("url") if isinstance(env, dict) else None
                override = environ.get(var, "") if isinstance(var, str) else ""
                urls = [override or build(svc)]
            else:
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        urls = sorted({u for u in urls if "|" not in u and "\n" not in u and " " not in u})
        if urls:
            out.append((entry, urls))
    return out


def cron_fence(addons_dir: Path, environ=os.environ) -> tuple[list[str], list[str]]:
    names, urls = [], []
    for path, manifest, parse_error in load_dir(addons_dir):
        if check_builtin_file(path, manifest, parse_error):
            raw = manifest.get("mcp") if isinstance(manifest, dict) else None
            for entry in raw if isinstance(raw, list) else [raw]:
                name = entry.get("name") if isinstance(entry, dict) else None
                if isinstance(name, str) and name:
                    names.append(name)
            continue
        cron = manifest.get("permissions", {}).get("cron")
        if cron is True:
            continue
        for mcp in mcp_entries(manifest):
            mcp_urls = _mcp_urls(manifest, mcp, environ)
            if cron == "ro":
                # The ro service stays in cron; its other URLs (rw) are fenced.
                urls.extend(u for level, u in mcp_urls.items() if level != "ro")
                continue
            names.append(mcp["name"])
            urls.extend(mcp_urls.values())
    return sorted(set(names)), sorted(set(urls))


def cron_fence_toolsets(addons_dir: Path) -> list[tuple[str, str]]:
    """(vaultBackend.name, vaultBackend.toolset) for each add-on whose plugin toolset stays out of cron."""
    out = set()
    for path, manifest, parse_error in load_dir(addons_dir):
        vb = manifest.get("vaultBackend") if isinstance(manifest, dict) else None
        name = vb.get("name") if isinstance(vb, dict) else None
        toolset = vb.get("toolset") if isinstance(vb, dict) else None
        if not (isinstance(name, str) and name and isinstance(toolset, str) and toolset):
            continue
        valid = not check_builtin_file(path, manifest, parse_error)
        if valid and manifest.get("permissions", {}).get("cron") is True:
            continue
        out.add((name, toolset))
    return sorted(out)


def mcp_plan(addons_dir: Path, profile: str, environ=os.environ) -> list[str]:
    lines = []
    for path, manifest, parse_error in load_dir(addons_dir):
        errors = check_builtin_file(path, manifest, parse_error)
        if errors:
            for e in errors:
                print(f"warning: skipping {path.parent.name}: {e}", file=sys.stderr)
            # Fail closed: an invalid manifest still has its URLs dropped from
            # the profile, so a stale entry or an unfiltered clone does not
            # outlive the manifest that would have filtered it.
            for entry, urls in _raw_mcp_urls(manifest, environ):
                unmanaged = entry.get("unmanaged")
                if isinstance(unmanaged, list) and profile in unmanaged:
                    continue
                name = entry.get("name") if isinstance(entry.get("name"), str) else path.parent.name
                title = entry.get("title") if isinstance(entry.get("title"), str) else name
                fields = [name, title, "0", "", urls[0], "0", "0", "http", "", "", " ".join(urls)]
                if not any("|" in f or "\n" in f for f in fields):
                    lines.append("|".join(fields))
            continue
        for mcp in mcp_entries(manifest):
            if profile in mcp.get("unmanaged", []):
                continue
            env = mcp.get("env", {})
            urls = _mcp_urls(manifest, mcp, environ)
            timeout = environ.get(env.get("timeout", ""), "") or str(mcp["timeout"])
            connect = environ.get(env.get("connectTimeout", ""), "") or str(mcp["connectTimeout"])
            flag = mcp["when"]["gate"] if "when" in mcp else manifest["builtin"]["flag"]
            enabled = "1" if truthy(environ.get(flag)) else "0"
            access = mcp["profiles"].get(profile, "")
            if "access" in env and access:
                # Fail closed: only read, write or empty are known values.
                cap = environ.get(env["access"], "")
                if cap == "read":
                    access = "ro"
                elif cap not in ("write", ""):
                    access = ""
            if "" in urls:
                url = urls[""]
            else:
                url = urls.get(access) or urls.get("rw") or urls["ro"]
            tools, pins = mcp["tools"], mcp.get("pin", [])
            if access == "ro":
                # A read-only profile never sees a write tool, listed or pinned.
                write = set(mcp.get("writeTools", []))
                tools = [t for t in tools if t not in write]
                pins = [t for t in pins if t not in write]
            fields = [mcp["name"], mcp.get("title", manifest["title"]), enabled, access, url, timeout, connect,
                      mcp["transport"], " ".join(tools), " ".join(pins),
                      " ".join(sorted(set(urls.values())))]
            if any("|" in f or "\n" in f for f in fields):
                print(f"warning: skipping {path.parent.name}: a value contains | or a newline",
                      file=sys.stderr)
                continue
            lines.append("|".join(fields))
    return lines


def mcp_urls(addons_dir: Path, environ=os.environ) -> list[str]:
    urls = []
    for path, manifest, parse_error in load_dir(addons_dir):
        if check_builtin_file(path, manifest, parse_error):
            for _entry, raw_urls in _raw_mcp_urls(manifest, environ):
                for u in raw_urls:
                    if u not in urls:
                        urls.append(u)
            continue
        for mcp in mcp_entries(manifest):
            for u in sorted(set(_mcp_urls(manifest, mcp, environ).values())):
                if u not in urls:
                    urls.append(u)
    return urls


def vault_plan(addons_dir: Path, profile: str, environ=os.environ) -> list[str]:
    lines = []
    for path, manifest, parse_error in load_dir(addons_dir):
        errors = check_builtin_file(path, manifest, parse_error)
        if errors:
            for e in errors:
                print(f"warning: skipping {path.parent.name}: {e}", file=sys.stderr)
            continue
        vb = manifest.get("vaultBackend")
        if not vb:
            continue
        enabled = "1" if truthy(environ.get(manifest["builtin"]["flag"])) else "0"
        fields = [vb["name"], enabled, vb["profiles"].get(profile, ""), vb["plugin"],
                  " ".join(vb["tools"])]
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
        for e in check_dir(Path(argv[1])):
            print(f"{argv[1]}: {e}")
            bad += 1
        return 1 if bad else 0
    if len(argv) == 2 and argv[0] == "mcp-urls":
        for u in mcp_urls(Path(argv[1])):
            print(u)
        return 0
    if len(argv) == 3 and argv[0] == "mcp-plan":
        for line in mcp_plan(Path(argv[1]), argv[2]):
            print(line)
        return 0
    if len(argv) == 3 and argv[0] == "vault-plan":
        for line in vault_plan(Path(argv[1]), argv[2]):
            print(line)
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
