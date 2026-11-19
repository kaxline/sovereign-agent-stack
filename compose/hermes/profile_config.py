#!/usr/bin/env python3
"""Read and write a Hermes profile's config.yaml for the bootstrap.

Runs inside the pinned Hermes image, which ships ruamel only, through
/opt/hermes/hermes_yaml. Compose mounts this file at /bootstrap/profile_config.py
in both bootstrap containers, and scripts/lib/hermes-yaml.sh mounts it for
host-side edits.

As a module, for the bootstrap's Python heredocs:

    sys.path.insert(0, "/bootstrap")
    from profile_config import load, save, walk

As a command:

    profile_config.py list --policy exact|superset|merge CONFIG DOTTED [ITEM...]
    profile_config.py platform-disabled CONFIG PLATFORM [--name NAME]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

HERMES_DIR = "/opt/hermes"
if HERMES_DIR not in sys.path:
    sys.path.insert(0, HERMES_DIR)

import hermes_yaml  # noqa: E402
from utils import atomic_yaml_write  # noqa: E402


def load(path: str | Path, *, merge_duplicates: bool = False) -> Any:
    """The parsed file, or {} when it is empty. Raises FileNotFoundError.

    merge_duplicates reads a file with repeated mapping keys (older setup runs
    appended a second platforms: block) by merging them, where hermes_yaml
    refuses the file.
    """
    raw = Path(path).read_text()
    if merge_duplicates:
        return _merging_yaml().load(raw) or {}
    return hermes_yaml.safe_load(raw) or {}


def save(path: str | Path, data: Any) -> None:
    """Write atomically, as Hermes does (temp file, fsync, replace)."""
    atomic_yaml_write(path, data)


def walk(data: dict, dotted: str, *, create: bool = True) -> tuple[dict | None, str]:
    """(parent mapping, leaf key) for a dotted key.

    With create, a missing or non-mapping step becomes {}; without it the
    parent is None when a step is missing.
    """
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            if not create:
                return None, parts[-1]
            child = {}
            node[part] = child
        node = child
    return node, parts[-1]


def profile_label(cfg_path: str | Path, dotted: str) -> str:
    """'<dotted> (<profile>)': one key is written to several profiles."""
    parent = Path(cfg_path).parent
    profile = parent.name if parent.parent.name == "profiles" else "default"
    return f"{dotted} ({profile})"


def _merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    return b


def _merging_yaml():
    from ruamel.yaml import YAML
    from ruamel.yaml.constructor import SafeConstructor

    class MergingConstructor(SafeConstructor):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            out = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=True)
                value = self.construct_object(value_node, deep=True)
                out[key] = _merge(out[key], value) if key in out else value
            return out

    y = YAML(typ="safe", pure=True)
    y.Constructor = MergingConstructor
    return y


def set_list(cfg_path: str, dotted: str, desired: list[str], policy: str) -> str:
    """Make the list at dotted hold desired; returns the log line.

    Every key routed here must be a list, so a scalar found there is replaced.
    exact replaces any list that differs (keys the stack owns outright).
    superset leaves a longer list that covers desired alone and warns about
    any other real list. merge appends the missing items and keeps the rest;
    a non-empty scalar becomes the list's first item.
    """
    config = load(cfg_path)
    node, leaf = walk(config, dotted)
    current = node.get(leaf)
    label = profile_label(cfg_path, dotted)

    if policy == "merge":
        if isinstance(current, str) and current.strip():
            current = [current]
        elif not isinstance(current, list):
            current = []
        if all(item in current for item in desired):
            return f"{label} already includes {', '.join(desired)}"
        merged = list(current)
        added = []
        for item in desired:
            if item not in merged:
                merged.append(item)
                added.append(item)
        node[leaf] = merged
        save(cfg_path, config)
        return f"{'Setting' if not current else 'Adding ' + ', '.join(added) + ' to'} {label}"

    if current == desired:
        return f"{label} already set"
    if current == [] or not isinstance(current, list):
        node[leaf] = list(desired)
        save(cfg_path, config)
        return f"{'Setting' if current in (None, []) else 'Repairing'} {label}"
    if policy == "exact":
        node[leaf] = list(desired)
        save(cfg_path, config)
        dropped = [item for item in current if item not in desired]
        return f"Replacing {label}" + (f" (dropped {', '.join(map(str, dropped))})" if dropped else "")
    # Exact membership, not substring: '/opt/skills-backup' must not satisfy
    # a requirement for '/opt/skills'.
    if all(item in current for item in desired):
        return f"{label} already includes {', '.join(desired)}"
    return f"WARNING: {label} is {current!r} - not overwriting"


def set_platform_disabled(cfg_path: str, platform: str, name: str = "") -> str | None:
    """Pin platforms.<platform>.enabled=false; None when the file is missing.

    A file with duplicate top-level keys is rewritten even when the value is
    already false, so the duplicates go away.
    """
    path = Path(cfg_path)
    if not path.is_file():
        return None
    raw = path.read_text()
    data = load(path, merge_duplicates=True)
    dupes = bool(raw.strip()) and len(_merging_yaml().compose(raw).value) != len(data)
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
    entry = platforms.get(platform)
    if not isinstance(entry, dict):
        entry = {}
    suffix = f" on {name}" if name else ""
    if entry.get("enabled") is False and not dupes:
        return f"platforms.{platform}.enabled already false{suffix}"
    entry["enabled"] = False
    platforms[platform] = entry
    data["platforms"] = platforms
    save(path, data)
    return f"pinned platforms.{platform}.enabled=false{suffix}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profile_config.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--policy", choices=("exact", "superset", "merge"), required=True)
    p_list.add_argument("config")
    p_list.add_argument("dotted")
    p_list.add_argument("items", nargs="*")
    p_plat = sub.add_parser("platform-disabled")
    p_plat.add_argument("config")
    p_plat.add_argument("platform")
    p_plat.add_argument("--name", default="")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        try:
            print(set_list(args.config, args.dotted, args.items, args.policy))
        except FileNotFoundError:
            print(f"ERROR: {args.config} not found")
            return 1
        return 0
    line = set_platform_disabled(args.config, args.platform, args.name)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
