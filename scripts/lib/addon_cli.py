#!/usr/bin/env python3
"""The parsing and file side of scripts/addon.sh. Standard library only: it
runs in the Hermes image through stack_python, so the host needs no python3.

    addon_cli.py --addons-dir DIR --data-root DIR COMMAND ...

    list --flags JSON
        A JSON array of addon_store.listing() objects, sorted by short id.
        JSON maps env flag names to "1" or "0" (addon.sh reads them from .env).
        A manifest that fails check_builtin_file or am.dir_conflicts is left out with a warning.
    configure SID [--instance SLUG] [--flags JSON]
        Reads one JSON object on stdin and stores it with apply_settings, or
        apply_instance for SLUG. Prints {"flags": {FLAG: "1"|"0"}}, the .env
        writes addon.sh makes: the gate flags of bool settings given, only
        while builtin.flag is 1 in --flags (the .env flags, as for list).
        Always {} for an instance or a disabled add-on; plan-enable restores
        gate flags from the stored settings.
    remove-instance SID SLUG
        Deletes the instance's file and secrets.
    check-instance SID SLUG [--slug-only]
        Checks SLUG and the JSON object on stdin against instances.settings
        (unknown or ill-typed settings, a missing required one) without
        writing anything, then prints `exists` when SLUG already has an
        instance, else `new`. --slug-only reads no stdin and checks only SLUG.
    plan-enable SID
        Exits 3 if a required setting is missing. Prints {"builtin": FLAG,
        "flags": {...}, "services": [...]}: builtin.flag at "1" plus the stored value of each
        bool setting whose flag is a gate, and every compose service, written
        "service@FLAG" when it runs only while its gate flag is 1. Also
        "oneshots", "recreate" (onToggle.recreate), "uses" (the compose
        service of each `uses` name in USES_COMPOSE) and "mcp" (each mcp name,
        "name@FLAG" when the entry has a gate).
    plan-disable SID [--flags JSON]
        Prints {"builtin": FLAG, "flags": {...}, "services": [...], "volumes": [...]}: builtin.flag
        and every gate flag at "0", every compose service, and the compose
        volumes the manifest declares, plus "recreate", "uses" and "mcp" as
        plan-enable prints them. First, a bool setting whose flag is a gate
        that is "1" in --flags (the .env flags) and that isn't stored is
        stored as true, so plan-enable restores the flag.
    toggle-script SID
        Prints onToggle.script's argv, one item per line; nothing without one.
    plan-run SID COMMAND [--instance SLUG]
        Prints the compose service, then the command's argv, one per line, with
        `--account SLUG` appended when the command takes an instance.
    plan-hook SID HOOK [--slug SLUG] [--reinit]
        HOOK is init, upgrade, backup, restore, connect or disconnect. Prints
        `script` and then the script's argv, or `service <compose name>` and
        then the command's argv, one item per line, ending in `--json`
        (`--slug SLUG --json` for connect and disconnect, which need --slug).
        Prints nothing when the manifest has no such hook, or for init when
        state.json records it and --reinit is not given.
    mark-init SID
        Records {"init": {"version": <manifest version>, "at": <UTC time>}}
        in data/addons/SID/state.json.
    hook-events STEP
        Reads a hook's stdout. JSON objects whose `phase` is `setup` or
        `pull` are printed with "step": STEP and every string in them through
        clean_line; every other line goes to stderr.
    hook-result SID EXIT
        Reads a hook's whole stdout on stdin (not a file: the host's temp
        directory isn't mounted where this runs) and prints five lines: an
        error code (empty on success), its message, the `account` of the
        `done` line (empty when it has none), the names in its `mcp` list,
        comma-separated (names that aren't lowercase letters, digits, _ and -
        are left out), and its `message` through clean_line (empty when it
        has none or the hook failed). A hook's `error` name is kept
        when it is a shared name or in instances.errors; any other name
        becomes command_failed with the name in the message. A non-zero
        EXIT without an `error` line is command_failed.
    data-envs SID
        Prints the env names the data.paths of every add-on declare, SID's
        first, one per line. addon.sh passes only these to plan-remove.
    plan-remove SID --env JSON --flags JSON --repo DIR [--lines]
        Exits 2 while SID's builtin flag is "1" in --flags (the .env flags).
        Prints {"services": [...], "volumes": [...], "delete": [...],
        "kept": [{"path", "reason"}]}: every one-shot and service, the
        compose volumes, and SID's data.paths sorted into those to delete and
        those to keep. A path is the value of its env in --env (addon.sh puts
        the caller's environment first, then .env), else its default, joined
        to DIR (the repo root) and normalised lexically: symlinks are never
        resolved. It is kept, with the reason, when it is a data directory
        (DIR/data or --data-root), lies outside both, holds a data directory,
        or is, holds or lies inside hermes, addons or a USER_DATA_SUBDIRS
        directory under either (as written or with symlinks resolved), has a symlink between the data
        directory and itself, or overlaps a path that another add-on whose
        builtin flag is "1" declares. data/addons/SID is always the last item
        of "delete". --lines prints `service NAME`, `volume NAME`,
        `delete PATH` and `kept REASON<tab>PATH` lines instead, for addon.sh;
        a path with a control character is refused (exit 2).
    delete-path PATH --repo DIR --sid SID
        Deletes PATH: a symlink or file is unlinked, a directory removed with
        shutil.rmtree, which doesn't follow symlinks inside it. Refuses
        (exit 2) a PATH that plan-remove would not put in "delete", apart
        from the overlap rule (data/addons/SID is allowed), and does nothing
        when PATH doesn't exist.
    pull-events SERVICES_CSV
        Reads `docker compose --progress json pull` output on stdin and prints
        {"phase":"pull","service":S,"current":N,"total":N} lines, bytes
        downloaded summed over layers, at most one per service per second.
        An event belongs to the service its id or parent_id names (older
        compose), else to the service of the last {"addonPull":"start",
        "service":S} marker addon.sh wrote, else to the only service given.
        With markers, {"phase":"pull","service":S,"status":"done"} follows
        each {"addonPull":"done","service":S}; without, it follows end of
        input for every service given. Lines that aren't JSON are ignored.
    log-tail [--lines N] [--redact FILE --sid SID]
        Reads a log on stdin and prints its last N lines (default 20) as a
        JSON array, cleaned as every addon.sh output line is: control
        characters other than tab dropped, tab made a space, cut to 500
        characters. Blank lines are left out. --redact first replaces each
        value of a secret-typed instance setting (SID's manifest) in FILE's
        JSON object with [redacted], longest first, as written and as it
        appears inside a JSON string. A FILE that isn't a JSON object fails
        (exit 2), so nothing unredacted is printed.
    redact SID FILE
        Copies stdin to stdout line by line with --redact's replacements.

Errors are one line, `error: <message>`, on stderr. Exit codes: 2 invalid
input (unknown add-on, bad JSON, unknown or ill-typed setting), 3 a required
setting is missing, 1 anything else. Messages name a setting, never its
value. An unexpected exception prints `error: <type>: <message>` for an
OSError (its message holds a path, not a value) and `error: <type>` for any
other, whose message could hold one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import addon_manifest as am  # noqa: E402
import addon_store as store  # noqa: E402

SID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
FLAG_VALUE = {"1", "0"}
# Manifest `uses` names are abstract stack services. Only these have a compose
# service that disable may stop when nothing else keeps it running.
USES_COMPOSE = {"neo4j": "neo4j"}


class Fail(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _load(addons_dir: Path):
    """(path, manifest, parse error) for each manifest under addons_dir. Every
    command that reads manifests loads them here, through am.checked_dir."""
    return am.checked_dir(addons_dir)


def _manifest(addons_dir: Path, sid: str) -> dict:
    if not SID_RE.match(sid):
        raise Fail(f"unknown add-on {sid!r}")
    for path, manifest, errors in _load(addons_dir):
        if path.parent.name != sid:
            continue
        if errors:
            raise Fail(f"{sid}: {errors[0]}")
        return manifest
    raise Fail(f"unknown add-on {sid!r}")


def _compose(manifest: dict, key: str) -> str:
    name = manifest["services"][key]["compose"]
    if not am.COMPOSE_NAME_RE.match(name):
        raise Fail(f"services.{key}.compose is not a compose service name")
    return name


def _gate_flags(manifest: dict) -> list[str]:
    return [g["flag"] for g in manifest.get("gates", [])]


def _dump(value) -> None:
    print(json.dumps(value, separators=(",", ":")))


def _flags_arg(text: str) -> dict[str, str]:
    try:
        flags = json.loads(text)
    except ValueError:
        raise Fail("--flags is not JSON") from None
    if not (isinstance(flags, dict) and all(
            isinstance(k, str) and am.ENV_RE.match(k) and v in FLAG_VALUE for k, v in flags.items())):
        raise Fail('--flags must map env names to "1" or "0"')
    return flags


def _stdin_object() -> dict:
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
        values = json.loads(text, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        raise Fail("stdin is not a JSON object") from None
    if not isinstance(values, dict):
        raise Fail("stdin is not a JSON object")
    return values


def _no_constant(name: str):
    raise ValueError(name)


def _compose_config() -> dict:
    """Service figures from `docker compose config --format json` on stdin.
    Empty or unreadable input gives no figures, with a warning for the latter."""
    text = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not text.strip():
        return {}
    try:
        return store.service_figures(json.loads(text))
    except ValueError:
        print("warning: compose config is not JSON; listing without image and memory figures", file=sys.stderr)
        return {}


def _listed(addons_dir: Path):
    """(manifest) for each valid built-in, warning about the rest."""
    for path, manifest, parse_error in _load(addons_dir):
        errors = am.check_builtin_file(path, manifest, parse_error)
        if errors:
            print(f"warning: skipping {path.parent.name}: {errors[0]}", file=sys.stderr)
            continue
        yield manifest


def _read_sizes(sizes_dir: Path, platform: str) -> dict:
    """{image: download bytes} from the <n>.image / <n>.json pairs addon.sh
    wrote to sizes_dir. An image without a readable manifest or platform match
    is left out."""
    sizes = {}
    for ref_file in sorted(sizes_dir.glob("*.image")):
        try:
            image = ref_file.read_text().strip()
            manifests = json.loads(ref_file.with_suffix(".json").read_text())
        except (OSError, ValueError):
            continue
        size = store.image_size(manifests, platform)
        if size is not None:
            sizes[image] = size
    return sizes


def cmd_list(args) -> None:
    flags = _flags_arg(args.flags)
    services = _compose_config()
    figures = {"services": services}
    if args.sizes_dir is not None:
        figures["sizes"] = _read_sizes(args.sizes_dir, args.platform)
    out = [store.listing(m, args.data_root, flags, figures) for m in _listed(args.addons_dir)]
    out.sort(key=lambda a: a["shortId"])
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_images(args) -> None:
    """The distinct images of the built-in add-ons' compose services, one per
    line, from `docker compose config --format json` on stdin."""
    services = _compose_config()
    seen = []
    for manifest in _listed(args.addons_dir):
        for spec in (manifest.get("services") or {}).values():
            image = (services.get(spec.get("compose")) or {}).get("image") if isinstance(spec, dict) else None
            if image and image not in seen and not image.startswith("-") and "\n" not in image:
                seen.append(image)
    for image in sorted(seen):
        print(image)


def cmd_configure(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    env_flags = _flags_arg(args.flags)
    values = _stdin_object()
    if args.instance is None:
        flags = store.apply_settings(manifest, args.data_root, values)
        if env_flags.get(manifest["builtin"]["flag"]) != "1":
            flags = {}
    else:
        store.apply_instance(manifest, args.data_root, args.instance, values)
        flags = {}
    _dump({"flags": flags})


def cmd_remove_instance(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    store.remove_instance(manifest, args.data_root, args.slug)


def cmd_check_instance(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    slug = store.slug(args.slug)
    if not args.slug_only:
        store.check_instance(manifest, _stdin_object())
    exists = slug in store.slugs(store.addon_dir(args.data_root, manifest))
    print("exists" if exists else "new")


def _toggle_fields(manifest: dict) -> dict:
    """onToggle's compose services, `uses` mapped to compose services, and the
    mcp names ("name@FLAG" when the entry has a gate), for plan-enable and plan-disable."""
    mcp = manifest.get("mcp", [])
    entries = mcp if isinstance(mcp, list) else [mcp]
    names = []
    for entry in entries:
        if not (isinstance(entry, dict) and "name" in entry):
            continue  # an `instances` entry is named per instance; only connect reports the names
        gate = entry.get("when", {}).get("gate")
        names.append(f"{entry['name']}@{gate}" if gate else entry["name"])
    return {"recreate": list(manifest.get("onToggle", {}).get("recreate", [])),
            "uses": [USES_COMPOSE[u] for u in manifest.get("uses", []) if u in USES_COMPOSE],
            "mcp": names}


def cmd_toggle_script(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    argv = list(manifest.get("onToggle", {}).get("script", []))
    _check_argv(argv, "onToggle.script")
    for a in argv:
        print(a)


def cmd_plan_enable(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    missing = store.missing(manifest, args.data_root)
    if missing:
        raise Fail(f"missing settings: {', '.join(missing)}", 3)
    gates = set(_gate_flags(manifest))
    flags = {manifest["builtin"]["flag"]: "1"}
    # A gate flag that a bool setting owns follows the stored setting, so
    # enabling after a disable (which zeroes every gate) restores it.
    stored = store.stored(manifest, store.addon_dir(args.data_root, manifest))
    for name, spec in manifest.get("settings", {}).items():
        if spec.get("type") == "bool" and spec.get("flag") in gates and name in stored:
            flags[spec["flag"]] = "1" if stored[name] else "0"
    services, oneshots, gated, mounts = [], [], [], []
    for key, svc in manifest["services"].items():
        name = _compose(manifest, key)
        gate = svc.get("when", {}).get("gate")
        (oneshots if svc.get("oneshot") else services).append(f"{name}@{gate}" if gate else name)
        if gate:
            gated.append(f"{name}@{gate}")
        if svc.get("mounts"):
            mounts.append(name)
    if mounts:
        store.prepare(manifest, args.data_root)
    # `stop` lists the gated services and one-shots; addon.sh stops those
    # whose gate is off once the flags are written.
    _dump({"builtin": manifest["builtin"]["flag"], "flags": flags, "services": services,
           "oneshots": oneshots, "stop": gated, "mounts": mounts, **_toggle_fields(manifest)})


def cmd_plan_disable(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    env_flags = _flags_arg(args.flags)
    # A gate flag that a bool setting owns, on in .env with the setting never
    # stored (set by hand, as v0.3.12 did ACTUAL_LOCAL_ENABLED), is stored as
    # true before the flag is zeroed, so the next enable turns it back on.
    gates = set(_gate_flags(manifest))
    stored = store.stored(manifest, store.addon_dir(args.data_root, manifest))
    keep = {name: True for name, spec in manifest.get("settings", {}).items()
            if spec.get("type") == "bool" and spec.get("flag") in gates
            and env_flags.get(spec["flag"]) == "1" and name not in stored}
    if keep:
        store.apply_settings(manifest, args.data_root, keep)
    flags = {f: "0" for f in [manifest["builtin"]["flag"], *_gate_flags(manifest)]}
    services = [_compose(manifest, key) for key in manifest["services"]]
    _dump({"builtin": manifest["builtin"]["flag"], "flags": flags, "services": services,
           "volumes": am.data_volumes(manifest),
           **_toggle_fields(manifest)})


def _check_argv(argv: list[str], where: str) -> None:
    """argv is printed one item per line, so no item may be empty or hold a
    line break or NUL."""
    if any(not a or "\n" in a or "\0" in a for a in argv):
        raise Fail(f"{where} has an empty item or a line break")


def cmd_plan_run(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    cmd = manifest.get("commands", {}).get(args.command)
    if cmd is None:
        raise Fail(f"{args.sid} has no command {args.command!r}")
    argv = list(cmd["argv"])
    if cmd["instance"]:
        if args.instance is None:
            raise Fail(f"{args.command} needs --instance")
        slug = store.slug(args.instance)
        if slug not in store.slugs(store.addon_dir(args.data_root, manifest)):
            raise Fail(f"no instance {slug!r}; configure it first")
        argv += ["--account", slug]
    elif args.instance is not None:
        raise Fail(f"{args.command} takes no --instance")
    _check_argv(argv, f"commands.{args.command}.argv")
    print(_compose(manifest, cmd["service"]))
    for a in argv:
        print(a)


def cmd_plan_hook(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    if args.hook not in (*am.HOOK_NAMES, *am.INSTANCE_HOOKS):
        raise Fail(f"unknown hook {args.hook!r}")
    instance_hook = args.hook in am.INSTANCE_HOOKS
    if instance_hook != (args.slug is not None):
        raise Fail(f"{args.hook} needs --slug" if instance_hook else f"{args.hook} takes no --slug")
    spec = am.hook(manifest, args.hook)
    if spec is None:
        return
    if args.hook == "init" and not args.reinit \
            and "init" in store.state(manifest, args.data_root):
        return
    if "script" in spec:
        head, argv = ["script"], list(spec["script"])
    else:
        cmd = manifest["commands"][spec["command"]]
        head, argv = [f"service {_compose(manifest, cmd['service'])}"], list(cmd["argv"])
    if instance_hook:
        argv += ["--slug", store.slug(args.slug)]
    argv.append("--json")
    _check_argv(argv, f"hooks.{args.hook}")
    for line in head + argv:
        print(line)


def cmd_mark_init(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_state(manifest, args.data_root, "init", {"version": manifest["version"], "at": at})


def _json_object(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# The hook line phases hook-events relays inside the step. done and error are
# read from the raw output by hook-result.
HOOK_RELAY_PHASES = ("setup", "pull")


def _clean_strings(value):
    """VALUE with every string in it, keys included, through clean_line."""
    if isinstance(value, str):
        return clean_line(value)
    if isinstance(value, dict):
        return {clean_line(k): _clean_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_strings(v) for v in value]
    return value


def cmd_hook_events(args) -> None:
    for line in sys.stdin:
        obj = _json_object(line)
        phase = obj.get("phase") if obj is not None else None
        if phase in HOOK_RELAY_PHASES:
            obj = _clean_strings(obj)
            obj["step"] = args.step
            print(json.dumps(obj, separators=(",", ":")), flush=True)
        else:
            print(line.rstrip("\n"), file=sys.stderr, flush=True)


def hook_error(manifest: dict, name, message) -> tuple[str, str]:
    """(code, message) for the error a hook reported. A shared name or one in
    instances.errors passes through; any other becomes command_failed with the
    name in the message."""
    text = clean_line(message) if isinstance(message, str) else ""
    if name == "command_failed":  # addon.sh's own code, e.g. from a failed mark-init
        return name, text
    inst = manifest.get("instances")
    own = inst.get("errors", []) if isinstance(inst, dict) else []
    if isinstance(name, str) and am.ERROR_NAME_RE.match(name) \
            and (name in am.SHARED_ERRORS or name in own):
        return name, text
    label = clean_line(name) if isinstance(name, str) and name else "unnamed error"
    return "command_failed", f"hook error {label}: {text}" if text else f"hook error {label}"


def cmd_hook_result(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    done = error = None
    lines = sys.stdin.buffer.read().decode("utf-8", errors="replace").splitlines()
    for line in lines:
        obj = _json_object(line)
        if obj is not None and obj.get("phase") == "done":
            done = obj
        elif obj is not None and obj.get("phase") == "error":
            error = obj
    code = message = account = mcp = note = retry = ""
    if error is not None:
        code, message = hook_error(manifest, error.get("error"), error.get("message"))
        after = error.get("retryAfter")
        if isinstance(after, int) and not isinstance(after, bool) and after > 0:
            retry = str(after)
    elif args.exit != 0:
        code, message = "command_failed", f"the hook exited with status {args.exit}"
    elif done is not None and isinstance(done.get("account"), str):
        account = clean_line(done["account"])
    if code == "" and done is not None and isinstance(done.get("mcp"), list):
        mcp = ",".join(n for n in done["mcp"] if isinstance(n, str) and MCP_NAME_RE.match(n))
    if code == "" and done is not None and isinstance(done.get("message"), str):
        note = clean_line(done["message"])
    for item in (code, message, account, mcp, note, retry):
        print(item.replace("\n", " "))


def _data_paths(manifest) -> list[dict]:
    data = manifest.get("data") if isinstance(manifest, dict) else None
    paths = data.get("paths") if isinstance(data, dict) else None
    return [p for p in paths if isinstance(p, dict)] if isinstance(paths, list) else []


def cmd_data_envs(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    names = [p["env"] for p in _data_paths(manifest) if isinstance(p.get("env"), str)]
    for _path, other, _err in _load(args.addons_dir):
        names += [p["env"] for p in _data_paths(other) if isinstance(p.get("env"), str)]
    for name in dict.fromkeys(n for n in names if am.ENV_RE.match(n)):
        print(name)


def _env_arg(text: str) -> dict[str, str]:
    try:
        env = json.loads(text)
    except ValueError:
        raise Fail("--env is not JSON") from None
    if not (isinstance(env, dict) and all(
            isinstance(k, str) and am.ENV_RE.match(k) and isinstance(v, str) for k, v in env.items())):
        raise Fail("--env must map env names to strings")
    return env


def _inside(path: str, root: str) -> bool:
    """path lies strictly below root. Both are normalised absolute paths."""
    return path != root and path.startswith(root.rstrip("/") + "/")


def _overlap(a: str, b: str) -> bool:
    return a == b or _inside(a, b) or _inside(b, a)


DATA_ROOT_SH = Path(__file__).resolve().parent / "data-root.sh"


def user_data_subdirs() -> list[str]:
    """USER_DATA_SUBDIRS from scripts/lib/data-root.sh, the one list of the
    user-data directories under the data root."""
    try:
        text = DATA_ROOT_SH.read_text(encoding="utf-8")
    except OSError:
        raise Fail("cannot read scripts/lib/data-root.sh", 1) from None
    m = re.search(r"^USER_DATA_SUBDIRS=\(([^)]*)\)", text, re.M)
    names = m.group(1).split() if m else []
    if not names or not all(re.match(r"^[a-z0-9_]+\Z", n) for n in names):
        raise Fail("scripts/lib/data-root.sh has no USER_DATA_SUBDIRS list", 1)
    return names


class DataRoots:
    """The two data directories and the stack's data under them. Paths to
    delete are normalised lexically (os.path.normpath; their symlinks are
    never resolved). The protected directories are listed both as written and
    with symlinks resolved, so ./data as a symlink can't hide them."""

    def __init__(self, repo: str, data_root) -> None:
        self.repo = os.path.normpath(os.path.abspath(repo))
        repo_data = os.path.join(self.repo, "data")
        self.data_root = os.path.normpath(os.path.abspath(str(data_root)))
        self.roots = [repo_data, self.data_root]
        # Each data root as written and resolved: a path that holds one holds
        # stack data whichever form it names.
        self.all_roots = list(dict.fromkeys(
            r for root in self.roots for r in (root, os.path.realpath(root))))
        names = ["hermes", "addons", *user_data_subdirs()]
        # Each protected directory as written and resolved: data/hermes may
        # itself be a symlink to another directory under data/.
        written = [os.path.join(r, n) for r in self.all_roots for n in names]
        self.stack = list(dict.fromkeys(written + [os.path.realpath(d) for d in written]))

    def resolve(self, value: str) -> str:
        return os.path.normpath(os.path.join(self.repo, value))

    def refusal(self, path: str) -> str | None:
        """Why path may not be deleted, or None. Ignores other add-ons."""
        if path in self.roots:
            return "is the data directory"
        inside = [r for r in self.roots if _inside(path, r)]
        if not inside:
            return "outside the data directory"
        if any(_inside(r, path) for r in self.all_roots) or any(_overlap(path, s) for s in self.stack):
            return "holds stack data"
        # A symlink between the data directory and the path makes the path's
        # real place unknown without following it.
        root = max(inside, key=len)
        parts = os.path.relpath(path, root).split(os.sep)[:-1]
        cur = root
        for part in parts:
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                return "behind a symlink"
        return None


def _path_of(entry: dict, env: dict[str, str], roots: DataRoots) -> str | None:
    name = entry.get("env")
    value = env.get(name, "") if isinstance(name, str) else ""
    if not value:
        value = entry.get("default")
    if not isinstance(value, str) or not value:
        return None
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise Fail(f"{name or 'data.paths'}: the path has a control character")
    return roots.resolve(value)


def cmd_plan_remove(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    flags = _flags_arg(args.flags)
    env = _env_arg(args.env)
    if flags.get(manifest["builtin"]["flag"]) == "1":
        raise Fail(f"{args.sid} is enabled; disable it first")
    roots = DataRoots(args.repo, args.data_root)
    # Paths other enabled add-ons declare. A manifest that fails its checks
    # still counts while it parses: keeping too much is the safe side.
    used: list[tuple[str, str]] = []
    for path, other, _err in _load(args.addons_dir):
        builtin = other.get("builtin") if isinstance(other, dict) else None
        flag = builtin.get("flag") if isinstance(builtin, dict) else None
        if path.parent.name == args.sid or not isinstance(flag, str) or flags.get(flag) != "1":
            continue
        for entry in _data_paths(other):
            try:
                other_path = _path_of(entry, env, roots)
            except Fail:
                continue
            if other_path is not None:
                used.append((other_path, path.parent.name))
    own = os.path.join(roots.data_root, "addons", _own_sid(manifest, args.sid))
    delete: list[str] = []
    kept: list[dict] = []
    for entry in _data_paths(manifest):
        target = _path_of(entry, env, roots)
        # data/addons/SID and what lies in it go with the add-on's own directory.
        if target is None or target == own or _inside(target, own) \
                or target in delete or any(k["path"] == target for k in kept):
            continue
        reason = roots.refusal(target)
        if reason is None:
            owner = next((sid for p, sid in used if _overlap(target, p)), None)
            reason = f"used by {owner}" if owner else None
        if reason is None:
            delete.append(target)
        else:
            kept.append({"path": target, "reason": reason})
    delete.append(own)
    services, oneshots = [], []
    for key, svc in manifest["services"].items():
        (oneshots if svc.get("oneshot") else services).append(_compose(manifest, key))
    volumes = am.data_volumes(manifest)
    if not args.lines:
        _dump({"services": oneshots + services, "volumes": volumes, "delete": delete, "kept": kept})
        return
    for item in delete + [k["path"] for k in kept]:
        if any(unicodedata.category(c) == "Cc" for c in item):
            raise Fail("a data path has a control character")
    for name in oneshots + services:
        print(f"service {name}")
    for name in volumes:
        print(f"volume {name}")
    for item in delete:
        print(f"delete {item}")
    for k in kept:
        print(f"kept {k['reason']}\t{k['path']}")


def _own_sid(manifest: dict, sid: str) -> str:
    short = am.short_id(manifest["id"])
    if short != sid or not SID_RE.match(short):
        raise Fail("the add-on directory is outside data/addons")
    return short


def cmd_delete_path(args) -> None:
    path = args.path
    roots = DataRoots(args.repo, args.data_root)
    if path != os.path.normpath(os.path.abspath(path)):
        raise Fail("delete-path takes a normalised absolute path")
    reason = roots.refusal(path)
    own_dir = os.path.dirname(path) == os.path.join(roots.data_root, "addons") \
        and SID_RE.match(args.sid) is not None and os.path.basename(path) == args.sid
    if reason is not None and not (reason == "holds stack data" and own_dir
                                   and not os.path.islink(os.path.dirname(path))):
        raise Fail(f"refusing to delete {path}: {reason}")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


LINE_MAX = 500
LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def clean_line(text: str) -> str:
    """The rule for every addon.sh output string. json_str in addon-events.sh
    drops the same characters (category Cc: 0x00-0x1F, 0x7F, 0x80-0x9F) and
    replaces invalid UTF-8 the same way (errors="replace" in cmd_log_tail), but
    cuts at 500 bytes, keeping UTF-8 sequences whole, where this cuts at 500
    characters."""
    text = text.replace("\t", " ")
    return "".join(c for c in text if unicodedata.category(c) != "Cc")[:LINE_MAX]


MCP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
REDACTED = "[redacted]"


def _redactor(addons_dir: Path, sid: str, path: str):
    """A function that replaces every secret value of the instance object in
    PATH with [redacted]. Each value is matched as written and as it appears
    inside a JSON string (escaped quotes, backslashes, \\u escapes), longest
    first so a value that contains another is replaced whole."""
    manifest = _manifest(addons_dir, sid)
    try:
        values = json.loads(Path(path).read_bytes().decode("utf-8"), parse_constant=_no_constant)
    except (OSError, UnicodeDecodeError, ValueError):
        raise Fail("--redact: the file is not a JSON object") from None
    if not isinstance(values, dict):
        raise Fail("--redact: the file is not a JSON object")
    schema = store._inst_schema(manifest)
    forms = set()
    for name, value in values.items():
        spec = schema.get(name)
        if isinstance(spec, dict) and spec.get("type") == "secret" and isinstance(value, str) and value:
            forms.update({value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]})
    ordered = sorted(forms, key=len, reverse=True)

    def redact(text: str) -> str:
        for form in ordered:
            text = text.replace(form, REDACTED)
        return text
    return redact


def cmd_redact(args) -> None:
    redact = _redactor(args.addons_dir, args.sid, args.file)
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        out.write(redact(line.decode("utf-8", errors="replace")).encode("utf-8"))
        out.flush()


def cmd_log_tail(args) -> None:
    if (args.redact is None) != (args.sid is None):
        raise Fail("--redact and --sid go together")
    redact = _redactor(args.addons_dir, args.sid, args.redact) if args.redact else (lambda t: t)
    text = redact(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    lines = [line for line in (clean_line(raw) for raw in LINE_SPLIT_RE.split(text)) if line.strip()]
    tail = lines[-args.lines:] if args.lines > 0 else []
    print(json.dumps(tail, separators=(",", ":")))


def _pull_line(**fields) -> None:
    print(json.dumps({"phase": "pull", **fields}, separators=(",", ":")), flush=True)


def cmd_pull_events(args) -> None:
    services = [s for s in args.services.split(",") if s]
    layers: dict[str, dict[str, tuple[int, int]]] = {s: {} for s in services}
    last_sent: dict[str, float] = {}
    marked = False
    current = None
    for raw in sys.stdin:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        marker = event.get("addonPull")
        if marker is not None:
            name = event.get("service")
            if name not in layers:
                continue
            marked = True
            if marker == "start":
                current = name
            elif marker == "done":
                _pull_line(service=name, status="done")
                current = None
            continue
        ident, parent = event.get("id"), event.get("parent_id")
        if ident in layers:
            service = ident
        elif parent in layers:
            service = parent
        elif current is not None:
            service = current
        elif len(services) == 1:
            service = services[0]
        else:
            continue
        if not parent or not isinstance(ident, str):
            continue
        cur, total = event.get("current"), event.get("total")
        text = str(event.get("text", ""))
        if text.startswith("Downloading") and isinstance(cur, int) and isinstance(total, int) and total > 0:
            layers[service][ident] = (min(cur, total), total)
        elif text in ("Download complete", "Pull complete") and ident in layers[service]:
            layers[service][ident] = (layers[service][ident][1],) * 2
        else:
            continue
        now = time.monotonic()
        if now - last_sent.get(service, -2.0) < 1.0:
            continue
        last_sent[service] = now
        sums = layers[service].values()
        _pull_line(service=service, current=sum(c for c, _ in sums), total=sum(t for _, t in sums))
    if not marked:
        for service in services:
            _pull_line(service=service, status="done")


def cmd_failed_container(args) -> None:
    """Print the first of SERVICES_CSV (in that order) whose container exited
    non-zero, or nothing. Reads `docker compose ps -a --format json` on stdin:
    one JSON array, or one object per line, depending on the compose version."""
    text = sys.stdin.read().strip()
    rows = []
    if text:
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else [data]
        except ValueError:
            for line in text.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    failed = {r.get("Service") for r in rows if isinstance(r, dict)
              and r.get("State") == "exited" and isinstance(r.get("ExitCode"), int)
              and r["ExitCode"] != 0}
    for name in args.services.split(","):
        if name and name in failed:
            print(name)
            return


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="addon_cli.py")
    p.add_argument("--addons-dir", type=Path, required=True)
    p.add_argument("--data-root", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list")
    s.add_argument("--flags", default="{}")
    s.add_argument("--sizes-dir", type=Path)
    s.add_argument("--platform", default="")
    s.set_defaults(func=cmd_list)
    s = sub.add_parser("images")
    s.set_defaults(func=cmd_images)
    s = sub.add_parser("configure")
    s.add_argument("sid")
    s.add_argument("--instance")
    s.add_argument("--flags", default="{}")
    s.set_defaults(func=cmd_configure)
    s = sub.add_parser("remove-instance")
    s.add_argument("sid")
    s.add_argument("slug")
    s.set_defaults(func=cmd_remove_instance)
    s = sub.add_parser("check-instance")
    s.add_argument("sid")
    s.add_argument("slug")
    s.add_argument("--slug-only", action="store_true")
    s.set_defaults(func=cmd_check_instance)
    s = sub.add_parser("redact")
    s.add_argument("sid")
    s.add_argument("file")
    s.set_defaults(func=cmd_redact)
    for name, func in (("plan-enable", cmd_plan_enable), ("data-envs", cmd_data_envs)):
        s = sub.add_parser(name)
        s.add_argument("sid")
        s.set_defaults(func=func)
    s = sub.add_parser("plan-disable")
    s.add_argument("sid")
    s.add_argument("--flags", default="{}")
    s.set_defaults(func=cmd_plan_disable)
    s = sub.add_parser("plan-remove")
    s.add_argument("sid")
    s.add_argument("--env", default="{}")
    s.add_argument("--flags", default="{}")
    s.add_argument("--repo", required=True)
    s.add_argument("--lines", action="store_true")
    s.set_defaults(func=cmd_plan_remove)
    s = sub.add_parser("delete-path")
    s.add_argument("path")
    s.add_argument("--repo", required=True)
    s.add_argument("--sid", required=True)
    s.set_defaults(func=cmd_delete_path)
    s = sub.add_parser("toggle-script")
    s.add_argument("sid")
    s.set_defaults(func=cmd_toggle_script)
    s = sub.add_parser("plan-hook")
    s.add_argument("sid")
    s.add_argument("hook")
    s.add_argument("--slug")
    s.add_argument("--reinit", action="store_true")
    s.set_defaults(func=cmd_plan_hook)
    s = sub.add_parser("mark-init")
    s.add_argument("sid")
    s.set_defaults(func=cmd_mark_init)
    s = sub.add_parser("hook-events")
    s.add_argument("step")
    s.set_defaults(func=cmd_hook_events)
    s = sub.add_parser("hook-result")
    s.add_argument("sid")
    s.add_argument("exit", type=int)
    s.set_defaults(func=cmd_hook_result)
    s = sub.add_parser("plan-run")
    s.add_argument("sid")
    s.add_argument("command")
    s.add_argument("--instance")
    s.set_defaults(func=cmd_plan_run)
    s = sub.add_parser("pull-events")
    s.add_argument("services")
    s.set_defaults(func=cmd_pull_events)
    s = sub.add_parser("failed-container")
    s.add_argument("services")
    s.set_defaults(func=cmd_failed_container)
    s = sub.add_parser("log-tail")
    s.add_argument("--lines", type=int, default=20)
    s.add_argument("--redact")
    s.add_argument("--sid")
    s.set_defaults(func=cmd_log_tail)
    args = p.parse_args(argv)
    try:
        args.func(args)
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except store.StateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (store.InputError, store.Missing) as exc:
        msg = str(exc)
        if exc.setting and not msg.startswith(exc.setting):
            msg = f"{exc.setting}: {msg}"
        print(f"error: {msg}", file=sys.stderr)
        return 2 if isinstance(exc, store.InputError) else 3
    except Exception as exc:  # noqa: BLE001
        detail = f": {exc}" if isinstance(exc, OSError) else ""
        print(f"error: {type(exc).__name__}{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
