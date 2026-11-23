#!/usr/bin/env python3
"""coding-root.sh changes (CR 0046 R4).

Runs in a scratch checkout with STACK_PYTHON=host. Covers the first full
listing, create, a same-size rewrite within one second, rename, delete, a
symbolic link, excluded directories, an unknown cursor, keeping the two
newest manifests, a file written while it is hashed, an unknown ID, text
mode, and that remove --delete-folder drops the manifests.

By hand, for the CR's ten-second target (not in ci):

    python3 scripts/test-coding-root-changes.py --timing [--files N]
    python3 scripts/test-coding-root-changes.py --timing --tree DIR [--exclude-dir NAME]...

The first generates N small files (default 50,000) and times a cold listing
(no manifest), a warm one (nothing moved) and one after 100 rewrites. The
second times the same on an existing tree, read-only; its manifests go to a
temporary folder.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "scripts/coding-root.sh",
    "scripts/lib/env.sh",
    "scripts/lib/python.sh",
    "scripts/lib/roots.sh",
    "scripts/lib/override_mounts.py",
    "scripts/lib/json-lines.sh",
    "scripts/lib/data-root.sh",
    "scripts/lib/stack-lock.sh",
    "scripts/lib/coding-roots.sh",
    "scripts/lib/coding_roots.py",
    "docker-compose.override.yml.example",
)
RID = "3f2b8c1e-7d4a-4e9b-9c2f-1a2b3c4d5e6f"
CPATH = "/Users/you/code/app"
EXCLUDES = ("--exclude-dir", "node_modules", "--exclude-dir", ".git")

checks = Checks()


def checkout(base: Path, env: str) -> Path:
    root = Path(tempfile.mkdtemp(dir=base))
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    (root / ".env").write_text(env)
    return root


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["STACK_PYTHON"] = "host"
    env.pop("ASSISTANT_DATA_ROOT", None)
    return subprocess.run(["bash", "scripts/coding-root.sh", *args], cwd=root, env=env,
                          capture_output=True, text=True)


def last_json(proc: subprocess.CompletedProcess[str]) -> dict:
    lines = proc.stdout.splitlines()
    ok = len(lines) == 1 and lines[0].startswith("{")
    checks.expect(ok, f"stdout is not one JSON line: {proc.stdout[:500]!r} {proc.stderr!r}")
    return json.loads(lines[0]) if ok else {}


def changes(root: Path, *args: str) -> dict:
    proc = run(root, "changes", "--id", RID, *args, "--json")
    checks.expect(proc.returncode == 0, f"changes {args}: rc={proc.returncode} {proc.stderr}")
    return last_json(proc)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def paths(out: dict) -> list[str]:
    return [f["path"] for f in out.get("files", [])]


def deleted(out: dict) -> list[str]:
    return [f["path"] for f in out.get("deleted", [])]


def load_module():
    spec = importlib.util.spec_from_file_location("coding_roots", ROOT / "scripts/lib/coding_roots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        ws = base / "ws"
        ws.mkdir()
        env = (
            f"HERMES_TERMINAL_BACKEND=ssh\nOPENCODE_WORKSPACE_HOST={ws}\n"
            f"HERMES_UID={os.getuid()}\nHERMES_GID={os.getgid()}\n"
        )
        root = checkout(base, env)
        shutil.copy(ROOT / "docker-compose.override.yml.example", root / "docker-compose.override.yml")
        proc = run(root, "add", "--container-path", CPATH, "--id", RID, "--json")
        checks.expect(proc.returncode == 0, f"add failed: {proc.stdout}{proc.stderr}")
        host = root / "data" / "coding-roots" / RID
        mdir = root / "data" / "coding-roots" / f"{RID}.manifest"

        # An empty root: a full listing with nothing in it.
        out = changes(root, *EXCLUDES)
        checks.expect(
            out.get("phase") == "done" and out.get("id") == RID and out.get("since") is None
            and out.get("reset") is False and out.get("full") is True and out.get("files") == []
            and out.get("deleted") == [] and out.get("unreadable") == [],
            f"empty listing: {out}",
        )
        c0 = out.get("cursor", "")

        # Create: files, a nested folder, a link, and excluded folders.
        (host / "src").mkdir()
        (host / "a.txt").write_text("aaaa")
        (host / "src" / "b.txt").write_text("bee")
        (host / "link").symlink_to("a.txt")
        for skip in ("node_modules/pkg", ".git", "src/node_modules"):
            (host / skip).mkdir(parents=True)
            (host / skip / "f").write_text("skip me")
        out = changes(root, "--since", c0, *EXCLUDES)
        checks.expect(out.get("reset") is False and out.get("full") is False and out.get("since") == c0, f"create: {out}")
        checks.expect(paths(out) == ["a.txt", "link", "src/b.txt"], f"create listed {paths(out)}")
        files = {f["path"]: f for f in out.get("files", [])}
        a = files.get("a.txt", {})
        checks.expect(a.get("sha256") == sha("aaaa") and a.get("size") == 4
                      and a.get("mtimeNs") == (host / "a.txt").stat().st_mtime_ns, f"a.txt entry {a}")
        checks.expect(files.get("src/b.txt", {}).get("sha256") == sha("bee"), f"b.txt entry {files.get('src/b.txt')}")
        link = files.get("link", {})
        checks.expect(link.get("link") is True and link.get("target") == "a.txt" and "sha256" not in link,
                      f"link entry {link}")
        c1 = out.get("cursor", "")
        checks.expect(c1 and c1 != c0, f"cursor did not move: {c0} {c1}")

        # Nothing moved: nothing listed.
        out = changes(root, "--since", c1, *EXCLUDES)
        checks.expect(paths(out) == [] and deleted(out) == [], f"no change listed {out}")
        c2 = out.get("cursor", "")

        # A same-size rewrite within one second: the mtime is set back to
        # the old value, so only ctime (and the hash) tell it apart.
        before = (host / "a.txt").stat()
        (host / "a.txt").write_text("bbbb")
        os.utime(host / "a.txt", ns=(before.st_atime_ns, before.st_mtime_ns))
        checks.expect((host / "a.txt").stat().st_mtime_ns == before.st_mtime_ns, "utime did not restore the mtime")
        out = changes(root, "--since", c2, *EXCLUDES)
        checks.expect(paths(out) == ["a.txt"] and out["files"][0].get("sha256") == sha("bbbb"),
                      f"same-size rewrite: {out}")
        c3 = out.get("cursor", "")

        # Rename: a delete and a create with the same hash.
        (host / "src" / "b.txt").rename(host / "src" / "c.txt")
        out = changes(root, "--since", c3, *EXCLUDES)
        checks.expect(paths(out) == ["src/c.txt"] and deleted(out) == ["src/b.txt"]
                      and out["files"][0].get("sha256") == sha("bee"), f"rename: {out}")
        c4 = out.get("cursor", "")

        # Delete: the link to it stays and isn't listed again.
        (host / "a.txt").unlink()
        out = changes(root, "--since", c4, *EXCLUDES)
        checks.expect(paths(out) == [] and deleted(out) == ["a.txt"], f"delete: {out}")
        c5 = out.get("cursor", "")

        # Writing in an excluded folder lists nothing; without the excludes
        # the folders' files appear.
        (host / "node_modules" / "pkg" / "g").write_text("new")
        out = changes(root, "--since", c5, *EXCLUDES)
        checks.expect(paths(out) == [] and deleted(out) == [], f"excluded write listed: {out}")
        c6 = out.get("cursor", "")
        out = changes(root, "--since", c6)
        checks.expect(
            paths(out) == [".git/f", "node_modules/pkg/f", "node_modules/pkg/g", "src/node_modules/f"],
            f"without excludes: {paths(out)}",
        )
        c7 = out.get("cursor", "")

        # Two manifests are kept: the newest and the one before, so a reply
        # that was lost can be asked for again.
        stored = sorted(p.name for p in mdir.glob("*.json.gz"))
        checks.expect(stored == sorted([f"{c6}.json.gz", f"{c7}.json.gz"]), f"manifests kept: {stored}")
        (host / "src" / "d.txt").write_text("dee")
        first = changes(root, "--since", c7)
        again = changes(root, "--since", c7)
        checks.expect(paths(first) == paths(again) == ["src/d.txt"] and again.get("reset") is False,
                      f"repeat from the previous cursor: {first} {again}")
        out = changes(root, "--since", c6)
        checks.expect(out.get("reset") is True, f"a pruned cursor did not reset: {out}")
        with gzip.open(mdir / f"{again['cursor']}.json.gz", "rt") as f:
            manifest = json.load(f)
        checks.expect(manifest.get("version") == 1 and "src/d.txt" in manifest.get("entries", {}),
                      f"manifest shape: {list(manifest)}")

        # Unknown cursor: reset with the full listing.
        out = changes(root, "--since", "0123456789abcdef", *EXCLUDES)
        checks.expect(out.get("reset") is True and out.get("full") is True and deleted(out) == []
                      and paths(out) == ["link", "src/c.txt", "src/d.txt"], f"unknown cursor: {out}")
        # Without --since: the full listing, not a reset.
        out = changes(root, *EXCLUDES)
        checks.expect(out.get("reset") is False and out.get("full") is True
                      and paths(out) == ["link", "src/c.txt", "src/d.txt"], f"full listing: {out}")

        # Text mode.
        cur = out.get("cursor", "")
        (host / "src" / "e.txt").write_text("e")
        proc = run(root, "changes", "--id", RID, "--since", cur, *EXCLUDES)
        lines = proc.stdout.splitlines()
        checks.expect(proc.returncode == 0 and lines[:1] == ["created\tsrc/e.txt"]
                      and len(lines) == 2 and lines[1].startswith("cursor\t"), f"text mode: {proc.stdout!r}")

        # A folder that can't be listed keeps its files: no deletes.
        cur = proc.stdout.splitlines()[-1].split("\t")[1] if proc.stdout else ""
        (host / "src").chmod(0)
        try:
            if os.access(host / "src", os.R_OK):
                print("SKIP unreadable folder check: running as root")
            else:
                out = changes(root, "--since", cur, *EXCLUDES)
                checks.expect(deleted(out) == [] and paths(out) == [] and out.get("unreadable") == ["src"],
                              f"unreadable folder: {out}")
                cur = out.get("cursor", "")
        finally:
            (host / "src").chmod(0o755)
        out = changes(root, "--since", cur, *EXCLUDES)
        checks.expect(paths(out) == [] and deleted(out) == [] and out.get("unreadable") == [],
                      f"after the folder is readable again: {out}")

        # Refusals.
        for args, rc, code, want in (
            (("--id", "nope"), 2, "bad_input", "no synced coding root"),
            (("--id", RID, "--since", "../x"), 2, "bad_input", "not a cursor"),
            (("--id", RID, "--exclude-dir", "a/b"), 2, "bad_input", "without a slash"),
            (("--since", "x"), 2, "bad_input", "needs --id"),
        ):
            proc = run(root, "changes", *args, "--json")
            out = last_json(proc)
            checks.expect(proc.returncode == rc and out.get("error") == code and want in out.get("message", ""),
                          f"changes {args}: rc={proc.returncode} {out}")
        proc = run(root, "add", "--container-path", "/Users/you/code/x", "--id", "x.manifest", "--json")
        checks.expect(proc.returncode == 2 and "must not end in .manifest" in proc.stdout,
                      f"add x.manifest: {proc.stdout}")

        # A file written while it is hashed: reported with its new stat and no
        # hash, then hashed and reported again on the next scan.
        mod = load_module()
        scratch = base / "race"
        scratch.mkdir()
        (scratch / "f").write_text("one")
        real = mod.sha256_of

        def racing(path: str) -> str:
            digest = real(path)
            with open(path, "a") as f:
                f.write("two")
            return digest

        mod.sha256_of = racing
        first = mod.scan(str(scratch), set(), {}, [])
        mod.sha256_of = real
        entry = first.get("f", [])
        checks.expect(len(entry) == 5 and entry[0] == 6 and entry[4] is None, f"racing entry {entry}")
        second = mod.scan(str(scratch), set(), first, [])
        checks.expect(second.get("f", [None] * 5)[4] == sha("onetwo"), f"rehash {second}")

        # The folder missing: command_failed. remove --delete-folder drops the
        # manifests with it.
        shutil.move(str(host), str(base / "moved"))
        proc = run(root, "changes", "--id", RID, "--json")
        out = last_json(proc)
        checks.expect(proc.returncode == 4 and out.get("error") == "command_failed", f"missing folder: {out}")
        shutil.move(str(base / "moved"), str(host))
        proc = run(root, "remove", "--id", RID, "--delete-folder", "--json")
        checks.expect(proc.returncode == 0 and not host.exists() and not mdir.exists(),
                      f"remove --delete-folder left manifests: {proc.stdout}{proc.stderr}")

    checks.finish("OK coding-root changes")


def timed(mod, top: Path, excludes: set[str], cache: dict) -> tuple[float, dict]:
    start = time.perf_counter()
    entries = mod.scan(str(top), excludes, cache, [])
    return time.perf_counter() - start, entries


def timing(argv: list[str]) -> None:
    files, tree, excludes = 50_000, None, set()
    while argv:
        opt = argv.pop(0)
        if opt == "--files":
            files = int(argv.pop(0))
        elif opt == "--tree":
            tree = Path(argv.pop(0)).resolve()
        elif opt == "--exclude-dir":
            excludes.add(argv.pop(0))
        else:
            sys.exit(__doc__)
    mod = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        if tree is None:
            tree = Path(tmp) / "tree"
            start = time.perf_counter()
            for i in range(files):
                d = tree / f"d{i // 1000:03d}" / f"s{i // 100 % 10}"
                if i % 100 == 0:
                    d.mkdir(parents=True)
                (d / f"f{i:05d}.txt").write_text(f"file {i}\n" * (1 + i % 20))
            print(f"generated {files} files in {time.perf_counter() - start:.1f}s")
        cold, entries = timed(mod, tree, excludes, {})
        size = sum(e[0] for e in entries.values())
        print(f"{len(entries)} entries, {size / 1e6:.1f} MB")
        print(f"cold listing (no manifest, hashes all): {cold:.2f}s")
        warm, entries = timed(mod, tree, excludes, entries)
        print(f"warm listing (nothing moved): {warm:.2f}s")
        mdir = Path(tmp) / "m"
        mdir.mkdir()
        start = time.perf_counter()
        mod.write_manifest(mdir, "t", entries, sorted(excludes))
        mod.read_manifest(mdir / "t.json.gz")
        print(f"manifest write + read: {time.perf_counter() - start:.2f}s, "
              f"{(mdir / 't.json.gz').stat().st_size / 1e6:.1f} MB")
        if tree.parent == Path(tmp):
            for p in sorted(entries)[:100]:
                (tree / p).write_text("rewritten\n")
            moved, _ = timed(mod, tree, excludes, entries)
            print(f"after 100 rewrites: {moved:.2f}s")
        print(f"python {sys.version.split()[0]} on {sys.platform}, {os.cpu_count()} CPUs")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--timing"]:
        timing(sys.argv[2:])
    else:
        main()
