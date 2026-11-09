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
    plan-enable SID
        Exits 3 if a required setting is missing. Prints {"flags": {...},
        "services": [...]}: builtin.flag at "1" plus the stored value of each
        bool setting whose flag is a gate, and every compose service, written
        "service@FLAG" when it runs only while its gate flag is 1.
    plan-disable SID
        Prints {"flags": {...}, "services": [...], "volumes": [...]}: builtin.flag
        and every gate flag at "0", every compose service, and the compose
        volumes the manifest declares.
    plan-run SID COMMAND [--instance SLUG]
        Prints the compose service, then the command's argv, one per line, with
        `--account SLUG` appended when the command takes an instance.
    purge SID
        Deletes data/addons/SID (addon_store.addon_dir refuses anything else).

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import addon_manifest as am  # noqa: E402
import addon_store as store  # noqa: E402

SID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
FLAG_VALUE = {"1", "0"}


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
    services = []
    for key, svc in manifest["services"].items():
        name = _compose(manifest, key)
        gate = svc.get("when", {}).get("gate")
        services.append(f"{name}@{gate}" if gate else name)
    _dump({"flags": flags, "services": services})


def cmd_plan_disable(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    flags = {f: "0" for f in [manifest["builtin"]["flag"], *_gate_flags(manifest)]}
    services = [_compose(manifest, key) for key in manifest["services"]]
    _dump({"flags": flags, "services": services, "volumes": am.data_volumes(manifest)})


def cmd_plan_run(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    cmd = manifest.get("commands", {}).get(args.command)
    if cmd is None:
        raise Fail(f"{args.sid} has no command {args.command!r}")
    argv = list(cmd["argv"])
    if cmd["instance"]:
        if args.instance is None:
            raise Fail(f"{args.command} needs --instance")
        slug = store._slug(args.instance)
        if slug not in store._slugs(store.addon_dir(args.data_root, manifest)):
            raise Fail(f"no instance {slug!r}; configure it first")
        argv += ["--account", slug]
    elif args.instance is not None:
        raise Fail(f"{args.command} takes no --instance")
    if any(not a or "\n" in a or "\0" in a for a in argv):
        raise Fail(f"commands.{args.command}.argv has an empty item or a line break")
    print(_compose(manifest, cmd["service"]))
    for a in argv:
        print(a)


def cmd_purge(args) -> None:
    manifest = _manifest(args.addons_dir, args.sid)
    target = store.addon_dir(args.data_root, manifest)
    if target.name != args.sid or target.parent != (Path(args.data_root) / "addons").resolve():
        raise Fail("the add-on directory is outside data/addons")
    if target.is_dir():
        shutil.rmtree(target)


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
    for name, func in (("plan-enable", cmd_plan_enable), ("plan-disable", cmd_plan_disable),
                       ("purge", cmd_purge)):
        s = sub.add_parser(name)
        s.add_argument("sid")
        s.set_defaults(func=func)
    s = sub.add_parser("plan-run")
    s.add_argument("sid")
    s.add_argument("command")
    s.add_argument("--instance")
    s.set_defaults(func=cmd_plan_run)
    args = p.parse_args(argv)
    try:
        args.func(args)
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
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
