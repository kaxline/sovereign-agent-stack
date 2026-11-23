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
    coding_roots.py changes REGISTRY ID [--since CURSOR] [--exclude-dir NAME]... [--text]
        The files in root ID changed since CURSOR, as one JSON done line (see
        cmd_changes), or one line per change with --text. Exits 3 for an
        unknown ID, 4 when its folder is missing, 5 for a malformed cursor.

Callers hold the stack's config lock (scripts/lib/stack-lock.sh) around a
check and the writes after it; changes takes its own lock on the root's
manifest folder. Read from stdin by scripts/lib/coding-roots.sh,
so it imports nothing from scripts/lib.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import secrets
import stat
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


CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MANIFEST_SUFFIX = ".manifest"
KEEP_MANIFESTS = 2


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
    if root_id.endswith(MANIFEST_SUFFIX):
        # <id>.manifest beside the folders holds another root's manifests.
        print(f"error\tID must not end in {MANIFEST_SUFFIX}: {root_id}")
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


# changes: a stateless scan (plan decision 7). Each call stats every entry,
# compares (size, mtime_ns, ctime_ns, inode) with the manifest the cursor
# names, hashes only what moved, writes a manifest under a new cursor and
# keeps the two newest. ctime and inode catch a same-size rewrite within the
# mtime's resolution; nothing runs between calls.
#
# Manifest: <data root>/coding-roots/<id>.manifest/<cursor>.json.gz,
#     {"version": 1, "excludeDirs": [...], "entries": {PATH: ENTRY}}
# with ENTRY [size, mtime_ns, ctime_ns, ino, sha256 | null] for a file and
# [size, mtime_ns, ctime_ns, ino, null, target] for a symbolic link. A null
# sha256 on a file means it changed while it was hashed; the next call hashes
# it again and reports it.


class Unreadable(Exception):
    pass


def stat_key(st: os.stat_result) -> list[int]:
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb", buffering=0) as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as e:
        raise Unreadable(e.strerror or str(e)) from e
    return h.hexdigest()


def rel_name(rel: str) -> str | None:
    """rel as text JSON can carry, or None when it isn't valid UTF-8."""
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return rel


def printable(rel: str) -> str:
    return os.fsencode(rel).decode("utf-8", "backslashreplace")


def scan(top: str, excludes: set[str], cache: dict, unreadable: list[str]) -> dict:
    """Every file and symbolic link under top as manifest entries.

    A file whose stat and inode match its cache entry keeps the cached hash;
    any other file is hashed. Directories named in excludes are not entered,
    links are not followed, and other file types are left out. Entries that
    can't be listed or read go to unreadable and keep their cache entries, so
    a passing permission problem doesn't read as a delete.
    """
    entries: dict[str, list] = {}

    def keep(rel: str, tree: bool = False) -> None:
        unreadable.append(printable(rel))
        if tree:
            prefix = f"{rel}/" if rel else ""
            entries.update((p, e) for p, e in cache.items() if p.startswith(prefix))
        elif rel in cache:
            entries[rel] = cache[rel]

    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            it = os.scandir(os.path.join(top, rel_dir) if rel_dir else top)
        except OSError:
            keep(rel_dir, tree=True)
            continue
        with it:
            for entry in it:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in excludes:
                            stack.append(rel)
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    keep(rel)
                    continue
                if rel_name(rel) is None:
                    unreadable.append(printable(rel))
                    continue
                key = stat_key(st)
                old = cache.get(rel)
                if stat.S_ISLNK(st.st_mode):
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        keep(rel)
                        continue
                    entries[rel] = key + [None, printable(target)]
                elif stat.S_ISREG(st.st_mode):
                    if old is not None and len(old) == 5 and old[:4] == key and old[4]:
                        entries[rel] = old
                        continue
                    try:
                        digest = sha256_of(entry.path)
                        after = os.stat(entry.path, follow_symlinks=False)
                    except (Unreadable, OSError):
                        keep(rel)
                        continue
                    if stat_key(after) != key or not stat.S_ISREG(after.st_mode):
                        # Written while we read it: report the new stat with
                        # no hash, and hash it again next time.
                        entries[rel] = stat_key(after) + [None]
                    else:
                        entries[rel] = key + [digest]
    return entries


def public(rel: str, e: list) -> dict:
    out = {"path": rel, "size": e[0], "mtimeNs": e[1]}
    if len(e) == 6:
        out["link"] = True
        out["target"] = e[5]
    else:
        out["sha256"] = e[4]
    return out


def read_manifest(path: Path) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), dict):
        return None
    return data["entries"]


def write_manifest(mdir: Path, cursor: str, entries: dict, excludes: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=mdir, prefix=".tmp-", suffix=".json.gz")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as gz:
            gz.write(json.dumps({"version": 1, "excludeDirs": excludes, "entries": entries},
                                separators=(",", ":")).encode("utf-8"))
        os.replace(tmp, mdir / f"{cursor}.json.gz")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def manifests(mdir: Path) -> list[Path]:
    """The root's manifests, newest first."""
    found = []
    for p in mdir.glob("*.json.gz"):
        if CURSOR_RE.match(p.name[: -len(".json.gz")]):
            try:
                found.append((p.stat().st_mtime_ns, p))
            except OSError:
                pass
    return [p for _, p in sorted(found, reverse=True)]


def cmd_changes(registry: Path, root_id: str, since: str | None, excludes: list[str], text: bool) -> None:
    host = next((r["hostPath"] for r in load(registry) if r["id"] == root_id), None)
    if host is None:
        print(f"no synced coding root with ID {root_id}", file=sys.stderr)
        sys.exit(3)
    if not os.path.isdir(host):
        print(f"the folder of synced coding root {root_id} is missing: {host}", file=sys.stderr)
        sys.exit(4)
    if since is not None and not CURSOR_RE.match(since):
        print(f"not a cursor: {since}", file=sys.stderr)
        sys.exit(5)

    mdir = registry.parent / f"{root_id}{MANIFEST_SUFFIX}"
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / ".lock", "a") as lock:
        # One scan per root at a time, so pruning never drops a manifest
        # another call has just handed out.
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = manifests(mdir)
        base = None
        if since is not None:
            base = read_manifest(mdir / f"{since}.json.gz")
        # Hashes come from the base, else the newest manifest, so a full
        # listing after the first one hashes only what moved.
        cache = base
        if cache is None and known:
            cache = read_manifest(known[0])
        unreadable: list[str] = []
        entries = scan(host, set(excludes), cache or {}, unreadable)

        cursor = secrets.token_hex(12)
        write_manifest(mdir, cursor, entries, excludes)
        for old in manifests(mdir)[KEEP_MANIFESTS:]:
            try:
                old.unlink()
            except OSError:
                pass

    reset = since is not None and base is None
    if base is None:
        changed = sorted(entries)
        deleted: list[str] = []
    else:
        # An entry with no hash is reported every time until it has one.
        changed = sorted(p for p, e in entries.items() if base.get(p) != e or (len(e) == 5 and not e[4]))
        deleted = sorted(p for p in base if p not in entries)

    if text:
        for p in changed:
            print(f"{'changed' if base is not None and p in base else 'listed' if base is None else 'created'}\t{p}")
        for p in deleted:
            print(f"deleted\t{p}")
        for p in sorted(unreadable):
            print(f"unreadable\t{p}")
        print(f"cursor\t{cursor}" + ("\treset" if reset else ""))
        return
    out = {
        "phase": "done",
        "id": root_id,
        "cursor": cursor,
        "since": since,
        "reset": reset,
        "full": base is None,
        "files": [public(p, entries[p]) for p in changed],
        "deleted": [{"path": p} for p in deleted],
        "unreadable": sorted(unreadable),
    }
    sys.stdout.write(json.dumps(out, separators=(",", ":"), ensure_ascii=False) + "\n")


def parse_changes(args: list[str]) -> None:
    if len(args) < 2:
        sys.exit(__doc__)
    registry, root_id, rest = Path(args[0]), args[1], args[2:]
    since, excludes, text = None, [], False
    while rest:
        opt = rest.pop(0)
        if opt == "--text":
            text = True
        elif opt in ("--since", "--exclude-dir") and rest:
            val = rest.pop(0)
            if opt == "--since":
                since = val
            else:
                excludes.append(val)
        else:
            sys.exit(__doc__)
    cmd_changes(registry, root_id, since, excludes, text)


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
    elif cmd == "changes":
        parse_changes(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
