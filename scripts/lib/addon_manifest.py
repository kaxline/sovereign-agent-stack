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
        For tuning entries that already exist; registers nothing. An
        mcp.instances entry has no fixed URL and is left out; instances-plan
        gives each instance's.
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
name it carries, so a broken manifest can't open cron. An mcp.instances entry
is fenced by the name of each instance in the store (<data_dir>/<add-on
directory>), except with cron "ro", where the entries stay in cron with the
key of the profile the job runs on; only api-server is held to the ro key. cron_fence_toolsets()
does the same for the plugin toolsets of add-ons with a vaultBackend.

Settings (and the schema of `instances`, {name, settings}) take the types in
SETTING_TYPES. A setting with neither `default` nor `optional: true` is
required; a secret setting may not have a default; a bool setting may name a
`flag`, which must be a gate flag. Names that map to the same
ADDON_<SHORT>_<NAME> key (differing only by case, - or _) are refused. Two
`mcp` entries may not point at the same service. An `mcp` entry with `instances`
(built-in only) registers one server per add-on instance and has no `service`,
`path` or `name`: {name: "<prefix>{slug}", url: "{setting:<url setting>}...",
access: "{setting:<choice setting offering only read and write>}", auth: {header,
scheme, secret: {ro, rw}} naming secret instance settings, gate?: {setting: a
bool instance setting, flag: builtin.flag or a gate}}. The name's prefix may not
be a core server name or prefix. The entry keeps transport, timeout,
connectTimeout, env, tools, pin, writeTools and profiles. mcp-plan skips it;
instances-plan (instances_plan()) lists one line per instance in the add-on's
store for the bootstrap to register. `services.<name>.mounts` and
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
CORE_MCP_PREFIXES = ("caldav-",)
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
HOOK_SCRIPT_RE = re.compile(r"^scripts/[a-z0-9-]+\.sh\Z")
ERROR_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*\Z")
# ./data/ then one or more segments, none empty, "." or "..".
DATA_PATH_RE = re.compile(r"^\./data(?:/(?!\.{1,2}(?:/|\Z))[^\s/]+)+\Z")
# Error names every add-on's instance hooks share (spec, "instances.connect").
SHARED_ERRORS = {"bad_slug", "exists", "not_found", "login_refused", "token_expired", "no_access",
                 "timeout", "server_unreachable"}
HOOK_NAMES = ("init", "upgrade", "backup", "restore")
INSTANCE_HOOKS = ("connect", "disconnect")


def short_id(manifest_id: str) -> str:
    return manifest_id.rsplit(".", 1)[-1]


def canonical(manifest: dict) -> str:
    """The only layout allowed in the repo. sync-profiles.sh reads the
    `builtin` block with sed, which needs a fixed layout."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


_AUTO = object()


def _find_repo() -> Path | None:
    """The repo root this file sits in (scripts/lib/..), or None where it
    doesn't, such as the bootstrap containers."""
    root = Path(__file__).resolve().parent.parent.parent
    return root if (root / "docker-compose.yml").is_file() else None


def validate(manifest, source: str = "builtin", repo=_AUTO) -> list[str]:
    """Return every problem with `manifest` for an add-on from `source`.

    `repo` is the repo root, for the checks that read it: a hook script must
    exist, and a data path must be a bind-mount source in docker-compose.yml,
    or the parent of one on whole path segments (`./data/twenty` for
    `${V:-./data/twenty}/storage`). By default it is the root this file sits
    in; with None, or where that root has no docker-compose.yml, those two
    checks are skipped."""
    if repo is _AUTO:
        repo = _find_repo()
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
    if "postInstallMessage" in manifest and not (isinstance(manifest["postInstallMessage"], str)
                                                 and manifest["postInstallMessage"].strip()):
        err("postInstallMessage must be a non-empty string")
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
            if "oneshot" in svc:
                if not isinstance(svc["oneshot"], bool):
                    err(f"services.{name}.oneshot must be true or false")
                elif svc["oneshot"]:
                    if source != "builtin":
                        err(f"services.{name}.oneshot: only built-in add-ons may have a oneshot service")
                    if "when" in svc:
                        err(f"services.{name}.oneshot: a oneshot service may not have when")
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
            errors.extend(_validate_mcp(entry, services, source, bool(reads), gate_flags,
                                        manifest.get("instances"),
                                        builtin.get("flag") if isinstance(builtin, dict) else None))
            if perms.get("cron") == "ro" and not (
                    isinstance(entry, dict) and (
                        (isinstance(entry.get("service"), dict) and "ro" in entry["service"])
                        # with mcp.instances, api-server gets only the instance's ro key;
                        # cron follows the profile it runs on
                        or (isinstance(entry.get("instances"), dict)
                            and isinstance(entry["instances"].get("auth"), dict)
                            and isinstance(entry["instances"]["auth"].get("secret"), dict)
                            and "ro" in entry["instances"]["auth"]["secret"]))):
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
                                                 flags=flags, in_instance=True,
                                                 has_connect="connect" in instances))
            extra = set(instances) - {"name", "settings", *INSTANCE_HOOKS, "errors", "optional"}
            if extra:
                err(f"instances has unknown fields: {', '.join(sorted(extra))}")
            if "optional" in instances:
                if not isinstance(instances["optional"], bool):
                    err("instances.optional must be true or false")
                elif instances["optional"] and source != "builtin":
                    err("instances.optional is for built-in add-ons only")
            if ("connect" in instances) != ("disconnect" in instances):
                err("instances.connect and instances.disconnect come as a pair")
            if "errors" in instances:
                names = instances["errors"]
                if not (_is_str_list(names) and all(ERROR_NAME_RE.match(n) for n in names)
                        and len(set(names)) == len(names)):
                    err("instances.errors must be a list of distinct names of lowercase letters, "
                        "digits, _ and -")
                else:
                    for n in names:
                        if n in SHARED_ERRORS:
                            err(f"instances.errors: {n!r} is a shared name; list only your own")

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

    for name, svc in services.items():
        if isinstance(svc, dict) and svc.get("oneshot") is True and name in mcp_services:
            err(f"services.{name}.oneshot: a oneshot service can't be the mcp target")

    hook_commands = commands if isinstance(commands, dict) else {}
    hooks = manifest.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            err(f"hooks must be an object of {', '.join(HOOK_NAMES)} to a hook")
        else:
            extra = set(hooks) - set(HOOK_NAMES)
            if extra:
                err(f"hooks has unknown fields: {', '.join(sorted(extra))}")
            for hname in HOOK_NAMES:
                if hname in hooks:
                    errors.extend(_validate_hook(hooks[hname], f"hooks.{hname}", source,
                                                 hook_commands, repo))
    if isinstance(instances, dict):
        for hname in INSTANCE_HOOKS:
            if hname in instances:
                errors.extend(_validate_hook(instances[hname], f"instances.{hname}", source,
                                             hook_commands, repo))

    if "onToggle" in manifest:
        errors.extend(_validate_on_toggle(manifest["onToggle"], source, repo))

    old_volumes = [(n, s_["volumes"]) for n, s_ in services.items()
                   if isinstance(s_, dict) and "volumes" in s_]
    if "data" in manifest:
        errors.extend(_validate_data(manifest["data"], source, repo, bool(old_volumes)))

    if "backup" in manifest:
        errors.extend(_validate_backup(manifest["backup"]))

    if "host" in manifest and source == "community":
        err("a host component is refused unless the add-on is built-in or on the official feed")

    return errors


def _validate_script(argv, where: str, source: str, repo) -> list[str]:
    """A repo-relative host script and its arguments (built-in only)."""
    if source != "builtin":
        return [f"{where}: a script is for built-in add-ons only"]
    if not (_is_str_list(argv) and argv):
        return [f"{where} must be a non-empty list of strings"]
    errors: list[str] = []
    if not HOOK_SCRIPT_RE.match(argv[0]):
        errors.append(f"{where}[0] must be scripts/<name>.sh with lowercase letters, digits and -")
    elif repo is not None and not (Path(repo) / argv[0]).is_file():
        errors.append(f"{where}[0]: {argv[0]} does not exist in the repo")
    if not all(a and "\n" not in a and "\r" not in a for a in argv):
        errors.append(f"{where} items must be non-empty strings without line breaks")
    return errors


def _validate_hook(hook_, where: str, source: str, commands: dict, repo) -> list[str]:
    if not isinstance(hook_, dict):
        return [f'{where} must be {{"command": NAME}} or {{"script": [argv]}}']
    if set(hook_) not in ({"command"}, {"script"}):
        return [f"{where} must have exactly one of command or script"]
    if "script" in hook_:
        return _validate_script(hook_["script"], f"{where}.script", source, repo)
    name = hook_["command"]
    cmd = commands.get(name) if isinstance(name, str) else None
    if not isinstance(cmd, dict):
        return [f"{where}.command must name one of commands"]
    if cmd.get("instance") is not False:
        return [f"{where}.command: {name!r} takes an instance, and a hook has none"]
    return []


def _validate_on_toggle(value, source: str, repo) -> list[str]:
    if not isinstance(value, dict):
        return ["onToggle must be an object {recreate, script}"]
    errors: list[str] = []
    if source != "builtin":
        errors.append("onToggle is for built-in add-ons only")
    rec = value.get("recreate")
    if not (_is_str_list(rec) and rec and all(COMPOSE_NAME_RE.match(r) for r in rec)
            and len(set(rec)) == len(rec)):
        errors.append("onToggle.recreate must be a non-empty list of distinct compose service names")
    if "script" in value:
        errors.extend(_validate_script(value["script"], "onToggle.script", source, repo))
    extra = set(value) - {"recreate", "script"}
    if extra:
        errors.append(f"onToggle has unknown fields: {', '.join(sorted(extra))}")
    return errors


def _bind_sources(compose_text: str) -> set[str]:
    """Sources of the short-syntax volume entries (`- SRC:DEST[:mode]`) in a compose file."""
    found = set()
    for m in re.finditer(r"^\s+-\s+[\"']?((?:\$\{[^}]*\}|[^:\s$\"'])+):", compose_text, re.M):
        found.add(m.group(1))
    return found


def _validate_data(data, source: str, repo, old_volumes: bool) -> list[str]:
    if not isinstance(data, dict):
        return ["data must be an object {paths, volumes}"]
    errors: list[str] = []
    err = errors.append
    if source != "builtin":
        err("data is for built-in add-ons only")
    extra = set(data) - {"paths", "volumes"}
    if extra:
        err(f"data has unknown fields: {', '.join(sorted(extra))}")
    if "volumes" in data:
        vols = data["volumes"]
        if not (_is_str_list(vols) and vols and all(COMPOSE_NAME_RE.match(v) for v in vols)
                and len(set(vols)) == len(vols)):
            err("data.volumes must be a non-empty list of distinct compose volume names")
        if old_volumes:
            err("data.volumes and services.<name>.volumes are both present; keep data.volumes")
    if "paths" in data:
        paths = data["paths"]
        if not (isinstance(paths, list) and paths):
            err("data.paths must be a non-empty list of {env, default}")
            paths = []
        bound = None
        if repo is not None:
            compose = Path(repo) / "docker-compose.yml"
            if compose.is_file():
                bound = _bind_sources(compose.read_text(encoding="utf-8"))
            else:
                err("data.paths: the repo has no docker-compose.yml to check against")
        for i, p in enumerate(paths):
            at = f"data.paths[{i}]"
            if not isinstance(p, dict):
                err(f"{at} must be an object {{env, default}}")
                continue
            extra = set(p) - {"env", "default"}
            if extra:
                err(f"{at} has unknown fields: {', '.join(sorted(extra))}")
            default = p.get("default")
            env = p.get("env")
            if "env" in p and not (isinstance(env, str) and ENV_RE.match(env)):
                err(f"{at}.env must be an env var name")
                env = None
            if not (isinstance(default, str) and DATA_PATH_RE.match(default)
                    and ".." not in default.split("/")):
                err(f"{at}.default must be ./data/<path> with no .. segment")
            elif bound is not None:
                forms = [default] + ([f"${{{env}:-{default}}}"] if env is not None else [])
                if not any(b == f or b.startswith(f + "/") for b in bound for f in forms):
                    err(f"{at}.default {default!r} is not a bind mount source, or the parent of one, "
                        "in docker-compose.yml")
    return errors


def _validate_backup(backup) -> list[str]:
    if not isinstance(backup, dict):
        return ["backup must be an object {secrets, recommended}"]
    errors: list[str] = []
    for key in ("secrets", "recommended"):
        if key in backup and not (_is_str_list(backup[key])
                                  and all(ENV_RE.match(v) for v in backup[key])):
            errors.append(f"backup.{key} must be a list of env var names")
    extra = set(backup) - {"secrets", "recommended"}
    if extra:
        errors.append(f"backup has unknown fields: {', '.join(sorted(extra))}")
    return errors


def data_volumes(manifest: dict) -> list[str]:
    """Compose volume names the add-on's data lives in: data.volumes, or the
    services.<name>.volumes of the old form (accepted for one patch)."""
    data = manifest.get("data")
    if isinstance(data, dict) and isinstance(data.get("volumes"), list):
        return list(data["volumes"])
    out: list[str] = []
    services = manifest.get("services")
    for svc in services.values() if isinstance(services, dict) else []:
        vols = svc.get("volumes") if isinstance(svc, dict) else None
        for v in vols if isinstance(vols, list) else []:
            if v not in out:
                out.append(v)
    return out


def hook(manifest: dict, name: str) -> dict | None:
    """The hook `name` (init, upgrade, backup, restore, connect, disconnect), or None."""
    where = manifest.get("instances") if name in INSTANCE_HOOKS else manifest.get("hooks")
    if name not in INSTANCE_HOOKS and name not in HOOK_NAMES:
        return None
    found = where.get(name) if isinstance(where, dict) else None
    return found if isinstance(found, dict) else None


def _validate_settings(settings: dict, *, source: str, reads: bool, flags: set[str],
                       in_instance: bool, has_connect: bool = False) -> list[str]:
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
        if "transient" in s:
            if not isinstance(s["transient"], bool):
                err(f"{at}.transient must be true or false")
            elif s["transient"]:
                if not in_instance or t != "secret":
                    err(f"{at}.transient is for secret settings inside instances.settings only")
                elif source != "builtin":
                    err(f"{at}.transient is for built-in add-ons only")
                elif not has_connect:
                    err(f"{at}.transient needs instances.connect, which receives the value")
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
                  gate_flags: set[str] = frozenset(), instances=None,
                  builtin_flag: str | None = None) -> list[str]:
    errors: list[str] = []
    err = errors.append
    if not isinstance(mcp, dict):
        return ["mcp must be an object"]
    per_instance = "instances" in mcp
    if "title" in mcp and not (isinstance(mcp["title"], str) and mcp["title"].strip()):
        err("mcp.title must be a non-empty string")
    if "when" in mcp:
        errors.extend(_validate_when(mcp["when"], gate_flags, "mcp.when"))
    svc = mcp.get("service")
    levels: set[str] = set()  # access levels that have a service
    if per_instance:
        # The URL and the server names come from mcp.instances.
        keys = []
        for key in ("service", "path", "name"):
            if key in mcp:
                err(f"mcp.instances replaces mcp.{key}; an entry has one or the other")
        errors.extend(_validate_mcp_instances(mcp["instances"], source, instances,
                                              gate_flags, builtin_flag))
    elif isinstance(svc, str):
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
    if not per_instance:
        name = mcp.get("name")
        if not (isinstance(mcp.get("path"), str) and PATH_RE.match(mcp["path"])):
            err("mcp.path must be an absolute URL path")
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
    return errors


SETTING_REF_RE = re.compile(r"\{setting:([^{}:]+)\}")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]+$")
# The options an instance's access setting may offer, and the level each maps to.
INSTANCE_ACCESS_LEVELS = {"read": "ro", "write": "rw"}


def _validate_mcp_instances(inst, source: str, instances, gate_flags: set[str],
                            builtin_flag: str | None) -> list[str]:
    """Check mcp.instances: {name, url, access, auth, gate?}. Every
    {setting:<name>} refers to a setting in the top-level instances.settings."""
    errors: list[str] = []
    err = errors.append
    if not isinstance(inst, dict):
        return ["mcp.instances must be an object"]
    if source != "builtin":
        err("mcp.instances is for built-in add-ons only")
    extra = set(inst) - {"name", "url", "access", "auth", "gate"}
    if extra:
        err(f"mcp.instances has unknown fields: {', '.join(sorted(extra))}")
    settings = instances.get("settings") if isinstance(instances, dict) else None
    if not isinstance(settings, dict):
        err("mcp.instances needs a top-level instances block with settings")
        settings = {}

    def setting_type(name) -> str | None:
        s = settings.get(name) if isinstance(name, str) else None
        return s.get("type") if isinstance(s, dict) else None

    name = inst.get("name")
    if not (isinstance(name, str) and name.count("{slug}") == 1 and not name.startswith("{slug}")
            and NAME_RE.match(name.replace("{slug}", "x"))):
        err("mcp.instances.name must be lowercase letters, digits and - with {slug} once, "
            "after a prefix such as crm-")
    else:
        prefix = name.split("{slug}")[0]
        # The bootstrap drops every entry whose name starts with the prefix and
        # has no instance, so no core name or prefix may start with it either.
        if (prefix.rstrip("-") in CORE_MCP_NAMES or prefix.startswith(CORE_MCP_PREFIXES)
                or any(n.startswith(prefix) for n in CORE_MCP_NAMES)
                or any(p.startswith(prefix) for p in CORE_MCP_PREFIXES)):
            err(f"mcp.instances.name prefix {prefix!r} collides with a server core registers")

    url = inst.get("url")
    refs = SETTING_REF_RE.findall(url) if isinstance(url, str) else []
    rest = SETTING_REF_RE.sub("", url) if isinstance(url, str) else ""
    if not (refs and "{" not in rest and "}" not in rest):
        err("mcp.instances.url must be a string with {setting:<name>} placeholders")
    else:
        for ref in refs:
            if setting_type(ref) not in ("url", "string"):
                err(f"mcp.instances.url: {ref!r} is not a url or string instance setting")

    access = inst.get("access")
    m = SETTING_REF_RE.fullmatch(access) if isinstance(access, str) else None
    if not m:
        err("mcp.instances.access must be {setting:<name>} naming a choice instance setting")
    elif setting_type(m.group(1)) != "choice":
        err(f"mcp.instances.access: {m.group(1)!r} is not a choice instance setting")
    else:
        options = settings[m.group(1)].get("options")
        if not isinstance(options, list) or any(
                not isinstance(o, str) or o not in INSTANCE_ACCESS_LEVELS for o in options):
            err(f"mcp.instances.access: {m.group(1)!r} may offer only "
                f"{' and '.join(INSTANCE_ACCESS_LEVELS)}")

    auth = inst.get("auth")
    if not isinstance(auth, dict):
        err("mcp.instances.auth must be {header, scheme, secret}")
    else:
        stray = set(auth) - {"header", "scheme", "secret"}
        if stray:
            err(f"mcp.instances.auth has unknown fields: {', '.join(sorted(stray))}")
        if not (isinstance(auth.get("header"), str) and HEADER_RE.match(auth["header"])):
            err("mcp.instances.auth.header must be an HTTP header name")
        if not (isinstance(auth.get("scheme"), str) and HEADER_RE.match(auth["scheme"])):
            err("mcp.instances.auth.scheme must be a word such as Bearer")
        secret = auth.get("secret")
        if not (isinstance(secret, dict) and set(secret) == ACCESS):
            err("mcp.instances.auth.secret must map ro and rw to secret instance settings")
        else:
            for level in sorted(ACCESS):
                if setting_type(secret[level]) != "secret":
                    err(f"mcp.instances.auth.secret.{level} must name a secret instance setting")

    gate = inst.get("gate")
    if gate is not None:
        if not (isinstance(gate, dict) and set(gate) == {"setting", "flag"}):
            err("mcp.instances.gate must be {setting, flag}")
        else:
            if setting_type(gate["setting"]) != "bool":
                err("mcp.instances.gate.setting must name a bool instance setting")
            if gate["flag"] != builtin_flag and gate["flag"] not in gate_flags:
                err("mcp.instances.gate.flag must be builtin.flag or a flag listed in gates")
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


def _raw_mcp_list(manifest) -> list:
    raw = manifest.get("mcp") if isinstance(manifest, dict) else None
    return raw if isinstance(raw, list) else [raw]


def dir_conflicts(loaded) -> dict[Path, list[str]]:
    """Errors that only show across the manifests of one addons dir, by path.
    The bootstrap drops every entry under an mcp.instances prefix that isn't a
    live instance, so an mcp.name under another add-on's prefix (or its own)
    would be written and then removed on every start, two prefixes where
    one starts with the other would remove each other's entries, and two
    add-ons may not register the same MCP server name. Read from
    the raw manifests, so an invalid one still counts."""
    names, prefixes = [], []
    for path, manifest, _parse_error in loaded:
        for entry in _raw_mcp_list(manifest):
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name:
                names.append((path, name))
            template = _raw_instance_template(entry)
            if template:
                prefixes.append((path, template.split("{slug}")[0]))
    out: dict[Path, list[str]] = {}
    owners: dict[str, list[Path]] = {}
    for path, name in names:
        if path not in owners.setdefault(name, []):
            owners[name].append(path)
    for name, paths in sorted(owners.items()):
        if len(paths) > 1:
            msg = f"mcp.name {name!r} is used by more than one add-on: {', '.join(p.parent.name for p in paths)}"
            for p in paths:
                out.setdefault(p, []).append(msg)
    for path, name in names:
        for other, prefix in prefixes:
            if name.startswith(prefix):
                out.setdefault(path, []).append(
                    f"mcp.name {name!r} starts with {other.parent.name}'s instance prefix {prefix!r}")
    for i, (pa, a) in enumerate(prefixes):
        for pb, b in prefixes[i + 1:]:
            if a.startswith(b) or b.startswith(a):
                msg = (f"instance prefix {a!r} ({pa.parent.name}) and {b!r} ({pb.parent.name}) "
                       "overlap; one would drop the other's entries")
                out.setdefault(pa, []).append(msg)
                if pb != pa:
                    out.setdefault(pb, []).append(msg)
    return out


def checked_dir(addons_dir: Path) -> list[tuple[Path, object, list[str]]]:
    """(path, manifest or None, errors) for each addon.json: check_builtin_file
    plus dir_conflicts. Every reader of a built-in addons dir uses this."""
    loaded = load_dir(addons_dir)
    conflicts = dir_conflicts(loaded)
    return [(path, manifest, check_builtin_file(path, manifest, parse_error) + conflicts.get(path, []))
            for path, manifest, parse_error in loaded]


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


def cron_fence(addons_dir: Path, environ=os.environ,
               data_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """data_dir holds each add-on's store (<data_dir>/<short id>/instances); an
    mcp.instances entry is fenced by the names of the instances found there."""
    names, urls = [], []
    for path, manifest, errors in checked_dir(addons_dir):
        if errors:
            raw = manifest.get("mcp") if isinstance(manifest, dict) else None
            for entry in raw if isinstance(raw, list) else [raw]:
                name = entry.get("name") if isinstance(entry, dict) else None
                if isinstance(name, str) and name:
                    names.append(name)
                template = _raw_instance_template(entry)
                if template and data_dir is not None:
                    names.extend(template.replace("{slug}", s)
                                 for s in _store_slugs(Path(data_dir) / path.parent.name)[0])
            continue
        cron = manifest.get("permissions", {}).get("cron")
        if cron is True:
            continue
        for mcp in mcp_entries(manifest):
            if "instances" in mcp:
                # cron "ro": the entries stay in cron. A cron job gets the key
                # of the profile it runs on (rw on default for a write
                # instance); a profile mapped to ro gets only the ro key
                # (instances_plan).
                if cron != "ro" and data_dir is not None:
                    template = mcp["instances"]["name"]
                    names.extend(template.replace("{slug}", s)
                                 for s in _store_slugs(Path(data_dir) / path.parent.name)[0])
                continue
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
    for path, manifest, errors in checked_dir(addons_dir):
        vb = manifest.get("vaultBackend") if isinstance(manifest, dict) else None
        name = vb.get("name") if isinstance(vb, dict) else None
        toolset = vb.get("toolset") if isinstance(vb, dict) else None
        if not (isinstance(name, str) and name and isinstance(toolset, str) and toolset):
            continue
        valid = not errors
        if valid and manifest.get("permissions", {}).get("cron") is True:
            continue
        out.add((name, toolset))
    return sorted(out)


def mcp_plan(addons_dir: Path, profile: str, environ=os.environ) -> list[str]:
    lines = []
    drops = []  # (position in lines, name, title, urls) of fail-closed drop-only lines
    managed = set()  # URLs a valid manifest registers or manages on this profile
    for path, manifest, errors in checked_dir(addons_dir):
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
                drops.append((len(lines), name, title, urls))
                lines.append(None)
            continue
        for mcp in mcp_entries(manifest):
            if "instances" in mcp:
                continue  # instances_plan lists these, one line per instance
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
            managed.update(u.rstrip("/") for u in urls.values())
            lines.append("|".join(fields))
    # An invalid manifest's drop-only line must not undo a valid add-on's
    # registration at the same URL: leave out the URLs a valid manifest manages.
    for pos, name, title, urls in drops:
        urls = [u for u in urls if u.rstrip("/") not in managed]
        if urls:
            fields = [name, title, "0", "", urls[0], "0", "0", "http", "", "", " ".join(urls)]
            if not any("|" in f or "\n" in f for f in fields):
                lines[pos] = "|".join(fields)
    return [l for l in lines if l is not None]


def mcp_urls(addons_dir: Path, environ=os.environ) -> list[str]:
    urls = []
    for _path, manifest, errors in checked_dir(addons_dir):
        if errors:
            for _entry, raw_urls in _raw_mcp_urls(manifest, environ):
                for u in raw_urls:
                    if u not in urls:
                        urls.append(u)
            continue
        for mcp in mcp_entries(manifest):
            if "instances" in mcp:
                continue  # no fixed URL: each instance's comes from its store
            for u in sorted(set(_mcp_urls(manifest, mcp, environ).values())):
                if u not in urls:
                    urls.append(u)
    return urls


# The instance store's slug rule (addon_store.SLUG_RE; addon_store imports this module).
STORE_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
# A key that reaches a profile's .env must be one line of printable text.
KEY_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]+\Z")


def _raw_instance_template(entry) -> str | None:
    """mcp.instances.name of an entry in a manifest that may be invalid, when it
    has the {slug}-after-a-prefix shape."""
    inst = entry.get("instances") if isinstance(entry, dict) else None
    name = inst.get("name") if isinstance(inst, dict) else None
    if not (isinstance(name, str) and name.count("{slug}") == 1 and not name.startswith("{slug}")
            and NAME_RE.match(name.replace("{slug}", "x"))):
        return None
    return name


def _store_slugs(store: Path) -> tuple[list[str], list[str]]:
    """(slugs, bad file names) of instances/*.json under an add-on's store.
    Names starting with a dot are neither."""
    try:
        files = sorted(p.name for p in (store / "instances").iterdir()
                       if p.name.endswith(".json") and not p.name.startswith("."))
    except OSError:
        return [], []
    good = [f[:-len(".json")] for f in files if STORE_SLUG_RE.match(f[:-len(".json")])]
    bad = [f for f in files if not STORE_SLUG_RE.match(f[:-len(".json")])]
    return good, bad


def instance_key_env(name: str) -> str:
    """crm-sales-eu -> CRM_SALES_EU_KEY, the profile .env key holding an
    instance's key. The bootstrap's addon_instance_key_env does the same."""
    return re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_KEY"


def _instance_values(store: Path, slug: str, schema: dict) -> dict | None:
    try:
        stored = json.loads((store / "instances" / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    values = {k: s["default"] for k, s in schema.items() if isinstance(s, dict) and "default" in s}
    values.update(stored)
    return values


def _fill_url(template: str, values: dict, schema: dict) -> str:
    """Fill {setting:<name>} refs. A url setting loses trailing slashes, and
    the literal text after its ref when it already ends with it, so a stored
    https://crm.example.com/mcp in "{setting:url}/mcp" stays .../mcp."""
    out, pos = [], 0
    for m in SETTING_REF_RE.finditer(template):
        out.append(template[pos:m.start()])
        v = values.get(m.group(1), "")
        # Only a string fills a ref; anything else leaves it empty, so the
        # url check turns the instance off.
        v = v if isinstance(v, str) else ""
        if schema.get(m.group(1), {}).get("type") == "url":
            v = v.rstrip("/")
            nxt = SETTING_REF_RE.search(template, m.end())
            after = template[m.end():nxt.start() if nxt else len(template)].rstrip("/")
            if after and v.endswith(after):
                v = v[:-len(after)]
        out.append(v)
        pos = m.end()
    out.append(template[pos:])
    return "".join(out)


def _key_problem(path: Path) -> str:
    """Why the key file can't be used, or "". Never returns the value."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "missing"
    if not text:
        return "empty"
    if not KEY_TEXT_RE.match(text):
        return "holds a line break or control character"
    return ""


def instances_plan(addons_dir: Path, data_dir: Path, profile: str, environ=os.environ) -> list[str]:
    """One line per instance of each add-on with mcp.instances, after a line for
    the add-on itself:

        name|title|on|url|keyenv|keyfile|timeout|connect|pins|reason|header|scheme

    The add-on's line has name "<prefix>*", on set from its flag, pins the
    names to unpin once no entry with the prefix is left, and reason the
    flag's name. An instance line is on (1) when the flag is on, the profile is
    in mcp.profiles, the url is http(s), the gate (if any) lets it through, the
    access setting is read or write and the key file it needs holds one line.
    Otherwise on is 0 and reason starts with flag, profile, file, url, gate,
    access or key. A profile mapped to ro in mcp.profiles takes the ro key whatever
    the access setting; an rw profile takes ro for read and rw for write.
    keyfile is a path under data_dir; key values are never printed.

    An invalid manifest still gives its add-on line, off, when its
    mcp.instances.name is readable, so the bootstrap drops its entries. A
    profile in the entry's mcp.unmanaged gets no line, valid or not."""
    data_dir = Path(data_dir)
    lines = []

    def emit(fields: list[str], where: str) -> None:
        if any("|" in f or "\n" in f for f in fields):
            print(f"warning: skipping {where}: a value contains | or a newline", file=sys.stderr)
            return
        lines.append("|".join(fields))

    for path, manifest, errors in checked_dir(addons_dir):
        sid_dir = path.parent.name
        if errors:
            for e in errors:
                print(f"warning: skipping {sid_dir}: {e}", file=sys.stderr)
            for entry in _raw_mcp_list(manifest):
                template = _raw_instance_template(entry)
                unmanaged = entry.get("unmanaged") if isinstance(entry, dict) else None
                if isinstance(unmanaged, list) and profile in unmanaged:
                    continue
                if template:
                    pins = entry.get("pin") if isinstance(entry, dict) else None
                    pins = [p for p in pins if TOOL_RE.match(p)] if _is_str_list(pins) else []
                    emit([template.split("{slug}")[0] + "*", sid_dir, "0", "", "", "", "", "",
                          " ".join(pins), "invalid manifest", "", ""], sid_dir)
            continue
        for mcp in mcp_entries(manifest):
            if "instances" not in mcp or profile in mcp.get("unmanaged", []):
                continue
            inst = mcp["instances"]
            template = inst["name"]
            title = mcp.get("title", manifest["title"])
            env = mcp.get("env", {})
            timeout = environ.get(env.get("timeout", ""), "") or str(mcp["timeout"])
            connect = environ.get(env.get("connectTimeout", ""), "") or str(mcp["connectTimeout"])
            flag = mcp["when"]["gate"] if "when" in mcp else manifest["builtin"]["flag"]
            flag_on = truthy(environ.get(flag))
            level = mcp["profiles"].get(profile, "")
            header, scheme = inst["auth"]["header"], inst["auth"]["scheme"]
            emit([template.split("{slug}")[0] + "*", title, "1" if flag_on else "0", "", "", "", timeout,
                  connect, " ".join(mcp.get("pin", [])), flag, "", ""], sid_dir)

            schema = manifest["instances"]["settings"]
            store = data_dir / sid_dir
            slugs, bad = _store_slugs(store)
            for f in bad:
                print(f"warning: {sid_dir}: skipping instance file {f}: a slug is lowercase letters, "
                      "digits and -, starting with a letter", file=sys.stderr)
            access_setting = SETTING_REF_RE.fullmatch(inst["access"]).group(1)
            gate = inst.get("gate")
            for slug in slugs:
                name = template.replace("{slug}", slug)

                def line(on: str, url="", keyfile="", pins="", reason="") -> None:
                    emit([name, title, on, url, instance_key_env(name), keyfile, timeout, connect,
                          pins, reason, header, scheme], f"{sid_dir}/{slug}")

                values = _instance_values(store, slug, schema)
                if not flag_on:
                    line("0", reason=f"flag {flag} is off")
                    continue
                if not level:
                    line("0", reason="profile not in the manifest's mcp.profiles")
                    continue
                if values is None:
                    line("0", reason=f"file instances/{slug}.json is not a JSON object")
                    continue

                url = _fill_url(inst["url"], values, schema)
                if not re.match(r"^https?://[^\s|]+\Z", url):
                    line("0", reason="url must start with http:// or https://")
                    continue
                if "@" in url.split("://", 1)[1].split("/", 1)[0]:
                    line("0", reason="url must not carry a user name or password")
                    continue
                if gate:
                    gated = values.get(gate["setting"])
                    if not isinstance(gated, bool):
                        line("0", url=url, reason=f"gate {gate['setting']} must be true or false")
                        continue
                    if gated and not truthy(environ.get(gate["flag"])):
                        line("0", url=url, reason=f"gate {gate['setting']} is set and {gate['flag']} is off")
                        continue
                access = values.get(access_setting)
                if not isinstance(access, str) or access not in INSTANCE_ACCESS_LEVELS:
                    line("0", url=url, reason=f"access {access_setting} must be read or write")
                    continue
                use = "ro" if level == "ro" else INSTANCE_ACCESS_LEVELS[access]
                secret = inst["auth"]["secret"][use]
                keyfile = store / "secrets" / f"{slug}.{secret}"
                problem = _key_problem(keyfile)
                if problem:
                    why = (f"{access_setting} is {access}" if level == "rw"
                           else "this profile is read-only")
                    line("0", url=url, reason=f"key {secret} is {problem}; {why}")
                    continue
                pins = mcp.get("pin", [])
                if use == "ro":
                    write = set(mcp.get("writeTools", []))
                    pins = [t for t in pins if t not in write]
                line("1", url=url, keyfile=str(keyfile), pins=" ".join(pins))
    return lines


def vault_plan(addons_dir: Path, profile: str, environ=os.environ) -> list[str]:
    lines = []
    for path, manifest, errors in checked_dir(addons_dir):
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
        for path, manifest, errors in checked_dir(Path(argv[1])):
            for e in errors:
                print(f"{path}: {e}")
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
    if len(argv) == 4 and argv[0] == "instances-plan":
        for line in instances_plan(Path(argv[1]), Path(argv[2]), argv[3]):
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
