#!/usr/bin/env python3
"""The synced coding root registry, and coding roots as host/container pairs.

A synced root is a folder the stack creates under
<data root>/coding-roots/<id>/ and binds at a container path a client names
(CR 0046): Boundary keeps a copy of a folder from the computer it runs on and
syncs it over SFTP, and the container path is the folder's path there. The
registry, <data root>/coding-roots/roots.json, maps each one:

    {"roots": [{"id": ID, "containerPath": P, "hostPath": H, "created": T}]}

CODING_EXTRA_ROOTS keeps every root's container path, synced or not, because
opencode-mcp, HERMES_WRITE_SAFE_ROOT and the environment hint read it as
container paths. What turns roots into binds reads the pairs instead.

    coding_roots.py pairs EXTRAS REGISTRY_JSON
        host<TAB>container per CODING_EXTRA_ROOTS entry: the registry's host
        path for a synced root, the entry itself for a plain one. Takes the
        registry's text rather than its path, so the image needs no mount.
    coding_roots.py check REGISTRY ID CONTAINER PRIMARY EXTRAS CONTEXT STACK DATA
        Whether `add --id ID --container-path CONTAINER` may go ahead. Prints
        `new`, `exists<TAB>HOST` (same ID and path) or `error<TAB>MESSAGE`.
    coding_roots.py put REGISTRY ID CONTAINER HOST
        Adds the root. The file is replaced atomically.
    coding_roots.py get REGISTRY ID
        container<TAB>host, or nothing for an unknown ID.
    coding_roots.py drop REGISTRY ID
        Removes the root; prints what get printed.
    coding_roots.py list REGISTRY
        id<TAB>container<TAB>host<TAB>created per root.

Callers hold the stack's config lock (scripts/lib/stack-lock.sh) around a
check and the writes after it. Read from stdin by scripts/lib/coding-roots.sh,
so it imports nothing from scripts/lib.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Paths inside the containers that a root must not sit under (decision 5 of
# the plan): the images' own trees, and /opt, where the stack mounts its data.
SYSTEM_PATHS = (
    "/opt", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/proc", "/sys", "/dev", "/root", "/tmp", "/var", "/run", "/home/opencode",
)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return parse(path.read_text())


def parse(text: str) -> list[dict]:
    if not text.strip():
        return []
    data = json.loads(text)
    roots = data.get("roots", []) if isinstance(data, dict) else []
    return [r for r in roots if isinstance(r, dict) and r.get("id") and r.get("containerPath")]


def save(path: Path, roots: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"roots": roots}, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def split_roots(raw: str) -> list[str]:
    return [p for p in raw.split(":") if p]


def inside(inner: str, outer: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def overlaps(a: str, b: str) -> bool:
    return inside(a, b) or inside(b, a)


def path_problem(path: str) -> str | None:
    if not path.startswith("/"):
        return f"container path must be absolute: {path}"
    if path == "/":
        return "container path must not be /"
    if path.endswith("/"):
        return f"container path must not end with a slash: {path}"
    if any(ch in path for ch in ":,") or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return f"container path must not hold a colon, comma or control character: {path}"
    parts = path.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts):
        return f"container path must not hold //, . or .. parts: {path}"
    for system in SYSTEM_PATHS:
        if inside(path, system):
            return f"container path is under {system}, which the containers use: {path}"
    return None


def cmd_check(registry: Path, root_id: str, container: str, primary: str,
              extras: str, context: str, stack: str, data: str) -> None:
    if not ID_RE.match(root_id):
        print(f"error\tID must be 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit: {root_id}")
        return
    problem = path_problem(container)
    if problem:
        print(f"error\t{problem}")
        return
    roots = load(registry)
    mine = next((r for r in roots if r["id"] == root_id), None)
    if mine is not None:
        if mine["containerPath"] == container:
            print(f"exists\t{mine['hostPath']}")
        else:
            print(f"error\tID {root_id} is already bound at {mine['containerPath']}")
        return
    for label, other in (("the stack folder", stack), ("the data root", data)):
        if other and inside(container, other):
            print(f"error\tcontainer path is inside {label} ({other}): {container}")
            return
    taken = [("the primary coding root", primary)] if primary else []
    taken += [("coding root", p) for p in split_roots(extras)]
    taken += [("context root", p) for p in split_roots(context)]
    for label, other in taken:
        if overlaps(container, other):
            print(f"error\tcontainer path overlaps {label} {other}: {container}")
            return
    print("new")


def cmd_put(registry: Path, root_id: str, container: str, host: str) -> None:
    roots = [r for r in load(registry) if r["id"] != root_id]
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    roots.append({"id": root_id, "containerPath": container, "hostPath": host, "created": created})
    save(registry, roots)


def cmd_get(registry: Path, root_id: str) -> None:
    for r in load(registry):
        if r["id"] == root_id:
            print(f"{r['containerPath']}\t{r['hostPath']}")


def cmd_drop(registry: Path, root_id: str) -> None:
    roots = load(registry)
    kept = [r for r in roots if r["id"] != root_id]
    if len(kept) == len(roots):
        return
    gone = next(r for r in roots if r["id"] == root_id)
    save(registry, kept)
    print(f"{gone['containerPath']}\t{gone['hostPath']}")


def cmd_list(registry: Path) -> None:
    for r in load(registry):
        print(f"{r['id']}\t{r['containerPath']}\t{r['hostPath']}\t{r.get('created', '')}")


def cmd_pairs(extras: str, registry_text: str) -> None:
    hosts = {r["containerPath"]: r["hostPath"] for r in parse(registry_text)}
    for path in split_roots(extras):
        print(f"{hosts.get(path, path)}\t{path}")


def main(argv: list[str]) -> None:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "pairs" and len(args) == 2:
        cmd_pairs(*args)
    elif cmd == "check" and len(args) == 8:
        cmd_check(Path(args[0]), *args[1:])
    elif cmd == "put" and len(args) == 4:
        cmd_put(Path(args[0]), *args[1:])
    elif cmd == "get" and len(args) == 2:
        cmd_get(Path(args[0]), args[1])
    elif cmd == "drop" and len(args) == 2:
        cmd_drop(Path(args[0]), args[1])
    elif cmd == "list" and len(args) == 1:
        cmd_list(Path(args[0]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
