#!/usr/bin/env python3
"""List and install per-model request defaults for llm-proxy.

model-defaults/local.json (gitignored) is the file llm-proxy reads. The tested
files in model-defaults/examples/ can be merged into it by name:

    ./scripts/model-defaults.py list
    ./scripts/model-defaults.py add qwen3.6-35b-a3b
    ./scripts/model-defaults.py add qwen3.6-35b-a3b --replace

`add` copies each model entry of the example into local.json, keeping the
models already there. A model that local.json already lists is left alone
unless --replace is given. See docs/model-defaults.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "model-defaults"
EXAMPLES = DIR / "examples"
LOCAL = DIR / "local.json"


def die(message: str) -> None:
    sys.stderr.write("[model-defaults] ERROR: %s\n" % message)
    sys.exit(1)


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        die("%s is not valid JSON: %s" % (path.relative_to(ROOT), exc))
    if not isinstance(data, dict):
        die("%s must hold a JSON object" % path.relative_to(ROOT))
    return data


def models(data: dict) -> list[str]:
    return [key for key in data if not key.startswith("_")]


def write(path: Path, data: dict) -> None:
    """Replace the file in one step, so llm-proxy never reads half of it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".local-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def cmd_list(_args) -> None:
    print("Examples (model-defaults/examples/):")
    for path in sorted(EXAMPLES.glob("*.json")):
        print("  %-28s %s" % (path.stem, ", ".join(models(load(path)))))
    local = load(LOCAL)
    where = LOCAL.relative_to(ROOT)
    if not LOCAL.exists():
        print("%s: not created yet" % where)
    else:
        print("%s: %s" % (where, ", ".join(models(local)) or "no models"))


def cmd_add(args) -> None:
    example = EXAMPLES / ("%s.json" % args.name)
    if not example.is_file():
        names = ", ".join(sorted(p.stem for p in EXAMPLES.glob("*.json")))
        die("no example named %r (have: %s)" % (args.name, names))
    source = load(example)
    local = load(LOCAL)
    existing = {key.strip().lower(): key for key in models(local)}
    added, skipped = [], []
    for model in models(source):
        current = existing.get(model.strip().lower())
        if current is not None and not args.replace:
            skipped.append(model)
            continue
        if current is not None:
            del local[current]
        local[model] = source[model]
        added.append(model)
    if added:
        write(LOCAL, local)
    where = LOCAL.relative_to(ROOT)
    for model in added:
        print("[model-defaults] %s: set %s" % (where, model))
    for model in skipped:
        print("[model-defaults] %s already lists %s; kept it (--replace to overwrite)" % (where, model))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show the examples and what local.json holds").set_defaults(func=cmd_list)
    add = sub.add_parser("add", help="merge an example into local.json")
    add.add_argument("name", help="example file name without .json, e.g. qwen3.6-35b-a3b")
    add.add_argument("--replace", action="store_true", help="overwrite models local.json already lists")
    add.set_defaults(func=cmd_add)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
