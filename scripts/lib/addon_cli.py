#!/usr/bin/env python3
"""The parsing and file side of scripts/addon.sh. Standard library only: it
runs in the Hermes image through stack_python, so the host needs no python3.

    addon_cli.py --addons-dir DIR --data-root DIR COMMAND ...

    list --flags JSON
        A JSON array of addon_store.listing() objects, sorted by short id.
        JSON maps env flag names to "1" or "0" (addon.sh reads them from .env).
        A manifest that fails check_builtin_file is left out with a warning.
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
    plan-disable SID
        Prints {"builtin": FLAG, "flags": {...}, "services": [...], "volumes": [...]}: builtin.flag
        and every gate flag at "0", every compose service, and the compose
        volumes the manifest declares, plus "recreate", "uses" and "mcp" as
        plan-enable prints them.
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
        Reads a hook's stdout. JSON objects with a string `phase` other than
        `done` and `error` are printed with "step": STEP; every other line
        goes to stderr.
    hook-result SID EXIT
        Reads a hook's whole stdout on stdin (not a file: the host's temp
        directory isn't mounted where this runs) and prints four lines: an
        error code (empty on success), its message, the `account` of the
        `done` line (empty when it has none) and the names in its `mcp` list,
        comma-separated (names that aren't lowercase letters, digits, _ and -
        are left out). A hook's `error` name is kept
        when it is a shared name or in instances.errors; any other name
        becomes command_failed with the name in the message. A non-zero
        EXIT without an `error` line is command_failed.
    purge SID
        Deletes data/addons/SID (addon_store.addon_dir refuses anything else).
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
import re
import shutil
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


def _manifest(addons_dir: Path, sid: str) -> dict:
    if not SID_RE.match(sid):
        raise Fail(f"unknown add-on {sid!r}")
    for path, manifest, parse_error in am.load_dir(addons_dir):
        if path.parent.name != sid:
            continue
        errors = am.check_builtin_file(path, manifest, parse_error)
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


def cmd_list(args) -> None:
    flags = _flags_arg(args.flags)
    out = []
    for path, manifest, parse_error in am.load_dir(args.addons_dir):
        errors = am.check_builtin_file(path, manifest, parse_error)
        if errors:
            print(f"warning: skipping {path.parent.name}: {errors[0]}", file=sys.stderr)
            continue
        out.append(store.listing(manifest, args.data_root, flags))
    out.sort(key=lambda a: a["shortId"])
    print(json.dumps(out, indent=2, ensure_ascii=False))


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
    exists = slug in store._slugs(store.addon_dir(args.data_root, manifest))
    print("exists" if exists else "new")


def _toggle_fields(manifest: dict) -> dict:
    """onToggle's compose services, `uses` mapped to compose services, and the
    mcp names ("name@FLAG" when the entry has a gate), for plan-enable and plan-disable."""
    mcp = manifest.get("mcp", [])
    entries = mcp if isinstance(mcp, list) else [mcp]
    names = []
    for entry in entries:
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
    stored = store._stored(manifest, store.addon_dir(args.data_root, manifest))
    for name, spec in manifest.get("settings", {}).items():
        if spec.get("type") == "bool" and spec.get("flag") in gates and name in stored:
            flags[spec["flag"]] = "1" if stored[name] else "0"
    services, oneshots = [], []
    for key, svc in manifest["services"].items():
        name = _compose(manifest, key)
        gate = svc.get("when", {}).get("gate")
        (oneshots if svc.get("oneshot") else services).append(f"{name}@{gate}" if gate else name)
    _dump({"builtin": manifest["builtin"]["flag"], "flags": flags, "services": services,
           "oneshots": oneshots, **_toggle_fields(manifest)})


def cmd_plan_disable(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
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
        if slug not in store._slugs(store.addon_dir(args.data_root, manifest)):
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


def cmd_hook_events(args) -> None:
    for line in sys.stdin:
        obj = _json_object(line)
        phase = obj.get("phase") if obj is not None else None
        if isinstance(phase, str) and phase not in ("done", "error"):
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
    code = message = account = mcp = ""
    if error is not None:
        code, message = hook_error(manifest, error.get("error"), error.get("message"))
    elif args.exit != 0:
        code, message = "command_failed", f"the hook exited with status {args.exit}"
    elif done is not None and isinstance(done.get("account"), str):
        account = clean_line(done["account"])
    if code == "" and done is not None and isinstance(done.get("mcp"), list):
        mcp = ",".join(n for n in done["mcp"] if isinstance(n, str) and MCP_NAME_RE.match(n))
    for item in (code, message, account, mcp):
        print(item.replace("\n", " "))


def cmd_purge(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    target = store.addon_dir(args.data_root, manifest)
    if target.name != args.sid or target.parent != (Path(args.data_root) / "addons").resolve():
        raise Fail("the add-on directory is outside data/addons")
    if target.is_dir():
        shutil.rmtree(target)


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
    s.set_defaults(func=cmd_list)
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
    for name, func in (("plan-enable", cmd_plan_enable), ("plan-disable", cmd_plan_disable),
                       ("purge", cmd_purge)):
        s = sub.add_parser(name)
        s.add_argument("sid")
        s.set_defaults(func=func)
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
