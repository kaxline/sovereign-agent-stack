#!/usr/bin/env python3
"""coding-root.sh add/list/remove for synced roots (CR 0046 R1, R2).

Runs in a scratch checkout with STACK_PYTHON=host. Covers a first and second
add, the container path refusals, list --json, remove with and without
--delete-folder, an unknown ID, the owner check on Linux, and that
sync-terminal-backend.sh (which up.sh runs on every start) keeps the synced
bind and writes no same-path bind for the container path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "scripts/coding-root.sh",
    "scripts/sync-terminal-backend.sh",
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
UUID = "3f2b8c1e-7d4a-4e9b-9c2f-1a2b3c4d5e6f"
CPATH = "/Users/you/code/app"

checks = Checks()


def checkout(base: Path, env: str) -> Path:
    root = Path(tempfile.mkdtemp(dir=base))
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    (root / ".env").write_text(env)
    return root


def run(root: Path, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["STACK_PYTHON"] = "host"
    env["SYNC_TERMINAL_ROOT"] = str(root)
    env.pop("ASSISTANT_DATA_ROOT", None)
    env.update(extra or {})
    return subprocess.run(["bash", *args], cwd=root, env=env, capture_output=True, text=True)


def last_json(proc: subprocess.CompletedProcess[str]) -> dict:
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    checks.expect(len(lines) == 1 and proc.stdout.strip() == lines[-1] if lines else False,
                  f"stdout is not one JSON line: {proc.stdout!r} {proc.stderr!r}")
    return json.loads(lines[-1]) if lines else {}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        ws = base / "ws"
        plain = base / "plain"
        notes = base / "notes"
        for d in (ws, plain, notes):
            d.mkdir()
        env = (
            f"HERMES_TERMINAL_BACKEND=ssh\nOPENCODE_WORKSPACE_HOST={ws}\n"
            f"CODING_EXTRA_ROOTS=:{plain}\nCONTEXT_EXTRA_ROOTS=:{notes}\n"
            f"HERMES_UID={os.getuid()}\nHERMES_GID={os.getgid()}\n"
        )
        root = checkout(base, env)
        override = root / "docker-compose.override.yml"
        script = "scripts/coding-root.sh"
        host = root / "data" / "coding-roots" / UUID
        spec = f"{host}:{CPATH}"
        recreate = ["hermes-worker", "hermes", "opencode", "opencode-mcp"]
        # make up's first sync binds the plain and context roots .env names.
        shutil.copy(ROOT / "docker-compose.override.yml.example", override)
        proc = run(root, "scripts/sync-terminal-backend.sh")
        checks.expect(proc.returncode == 0, f"first sync failed: {proc.stderr}")

        # First add: folder, registry, env, binds on both services.
        proc = run(root, script, "add", "--container-path", CPATH, "--id", UUID, "--json")
        checks.expect(proc.returncode == 0, f"add failed: {proc.stdout}{proc.stderr}")
        out = last_json(proc)
        checks.expect(
            out == {"phase": "done", "id": UUID, "hostPath": str(host), "containerPath": CPATH,
                    "created": True, "recreate": recreate},
            f"add printed {out}",
        )
        checks.expect(host.is_dir(), "add made no folder")
        checks.expect(oct(host.stat().st_mode & 0o7777) == "0o2775", f"folder mode {oct(host.stat().st_mode)}")
        reg = json.loads((root / "data" / "coding-roots" / "roots.json").read_text())
        checks.expect(
            [(r["id"], r["containerPath"], r["hostPath"]) for r in reg["roots"]] == [(UUID, CPATH, str(host))],
            f"registry: {reg}",
        )
        checks.expect(f"CODING_EXTRA_ROOTS=:{plain}:{CPATH}\n" in (root / ".env").read_text(), "env not appended")
        text = override.read_text()
        checks.expect(text.count(f"      - {spec}\n") == 2, f"binds missing:\n{text}")
        checks.expect(f"{CPATH}:{CPATH}" not in text, f"same-path bind written:\n{text}")

        # Second add with the same ID and path changes nothing.
        before = ((root / ".env").read_text(), text, (root / "data/coding-roots/roots.json").read_text())
        proc = run(root, script, "add", "--container-path", CPATH, "--id", UUID, "--json")
        out = last_json(proc)
        checks.expect(proc.returncode == 0 and out.get("created") is False and out.get("hostPath") == str(host),
                      f"second add: {proc.stdout}{proc.stderr}")
        after = ((root / ".env").read_text(), override.read_text(), (root / "data/coding-roots/roots.json").read_text())
        checks.expect(after == before, "second add changed a file")

        # Refusals: bad_input, exit 2, nothing written.
        refusals = [
            (("--container-path", "/Users/you/code/other", "--id", UUID), "already bound at"),
            (("--container-path", CPATH + "/sub", "--id", "b"), "overlaps coding root"),
            (("--container-path", "/Users/you/code", "--id", "b"), "overlaps coding root"),
            (("--container-path", str(plain / "x"), "--id", "b"), "overlaps coding root"),
            (("--container-path", str(ws), "--id", "b"), "overlaps the primary"),
            (("--container-path", str(notes), "--id", "b"), "overlaps context root"),
            (("--container-path", str(root / "x"), "--id", "b"), "inside the stack folder"),
            (("--container-path", "/opt/data/x", "--id", "b"), "under /opt"),
            (("--container-path", "/home/opencode", "--id", "b"), "under /home/opencode"),
            (("--container-path", "relative/path", "--id", "b"), "absolute"),
            (("--container-path", "/a/b/", "--id", "b"), "slash"),
            (("--container-path", "/a/../b", "--id", "b"), ".."),
            (("--container-path", "/a:b", "--id", "b"), "colon"),
            (("--container-path", "/a/b", "--id", "../x"), "ID must be"),
            (("--container-path", "/a/b"), "needs both"),
        ]
        for args, want in refusals:
            proc = run(root, script, "add", *args, "--json")
            out = last_json(proc)
            checks.expect(
                proc.returncode == 2 and out.get("error") == "bad_input" and want in out.get("message", ""),
                f"add {args}: rc={proc.returncode} {out}",
            )
        after = ((root / ".env").read_text(), override.read_text(), (root / "data/coding-roots/roots.json").read_text())
        checks.expect(after == before, "a refused add changed a file")
        checks.expect(not (root / "data/coding-roots/b").exists(), "a refused add made a folder")

        # The plain remove refuses a synced root's path.
        proc = run(root, script, "remove", CPATH)
        checks.expect(proc.returncode == 2 and f"remove --id {UUID}" in proc.stderr, f"plain remove: {proc.stderr}")

        # up.sh's sync keeps the synced bind and adds no same-path one, on
        # either backend.
        proc = run(root, "scripts/sync-terminal-backend.sh")
        checks.expect(proc.returncode == 0, f"sync failed: {proc.stderr}")
        checks.expect("override mounts already match" in proc.stdout, f"sync re-edited add's output: {proc.stdout}")
        env_text = (root / ".env").read_text()
        (root / ".env").write_text(env_text.replace("BACKEND=ssh", "BACKEND=local"))
        proc = run(root, "scripts/sync-terminal-backend.sh")
        checks.expect(proc.returncode == 0, f"local sync failed: {proc.stderr}")
        text = override.read_text()
        checks.expect(f"      - {spec}\n" in text.partition("  hermes:\n")[2], f"local sync moved no synced bind:\n{text}")
        checks.expect("  hermes-worker:" not in text and f"{CPATH}:{CPATH}" not in text, f"local sync wrote:\n{text}")
        (root / ".env").write_text(env_text)
        run(root, "scripts/sync-terminal-backend.sh")
        checks.expect(override.read_text().count(f"      - {spec}\n") == 2, "sync back to ssh lost a bind")

        # list --json: primary, plain extra, synced with id and size.
        (host / "f.txt").write_text("x" * 5000)
        proc = run(root, script, "list", "--json")
        out = last_json(proc)
        roots = out.get("roots", [])
        checks.expect([r["kind"] for r in roots] == ["primary", "extra", "synced"], f"list: {out}")
        if len(roots) == 3:
            checks.expect(roots[1]["hostPath"] == roots[1]["containerPath"] == str(plain) and roots[1]["id"] is None, f"plain row {roots[1]}")
            checks.expect(roots[2]["id"] == UUID and roots[2]["hostPath"] == str(host) and roots[2]["containerPath"] == CPATH, f"synced row {roots[2]}")
            checks.expect(isinstance(roots[2]["bytes"], int) and roots[2]["bytes"] >= 4096, f"synced bytes {roots[2]}")
        proc = run(root, script, "list", "--json", "--no-size")
        checks.expect(all(r["bytes"] is None for r in last_json(proc).get("roots", [])), "--no-size still sized")
        proc = run(root, script, "list")
        checks.expect(proc.returncode == 0 and f"{UUID}: {CPATH} <- {host}" in proc.stdout, f"list text: {proc.stdout}")

        # A second synced root, then remove the first without --delete-folder.
        proc = run(root, script, "add", "--container-path", "/Users/you/code/lib", "--id", "second", "--json")
        checks.expect(proc.returncode == 0, f"second root: {proc.stdout}{proc.stderr}")
        proc = run(root, script, "remove", "--id", UUID, "--json")
        out = last_json(proc)
        checks.expect(
            out == {"phase": "done", "id": UUID, "removed": True, "hostPath": str(host), "containerPath": CPATH,
                    "folderDeleted": False, "recreate": recreate},
            f"remove printed {out}",
        )
        text = override.read_text()
        checks.expect(spec not in text, f"remove left a bind:\n{text}")
        checks.expect(text.count(f"{root}/data/coding-roots/second:/Users/you/code/lib") == 2, f"remove dropped the other root:\n{text}")
        checks.expect(f"CODING_EXTRA_ROOTS=:{plain}:/Users/you/code/lib\n" in (root / ".env").read_text(), "remove left env")
        checks.expect((host / "f.txt").exists(), "remove without --delete-folder deleted the folder")

        # Unknown (and already removed) ID: exit 0, removed false.
        for rid in (UUID, "never"):
            proc = run(root, script, "remove", "--id", rid, "--json")
            out = last_json(proc)
            checks.expect(proc.returncode == 0 and out.get("removed") is False and out.get("recreate") == [],
                          f"remove unknown {rid}: {proc.stdout}{proc.stderr}")

        # Re-adding the removed ID reuses its folder; --delete-folder removes it.
        proc = run(root, script, "add", "--container-path", CPATH, "--id", UUID, "--json")
        checks.expect(proc.returncode == 0 and (host / "f.txt").exists(), f"re-add: {proc.stdout}{proc.stderr}")
        proc = run(root, script, "remove", "--id", UUID, "--delete-folder", "--json")
        out = last_json(proc)
        checks.expect(proc.returncode == 0 and out.get("folderDeleted") is True, f"remove --delete-folder: {out}")
        checks.expect(not host.exists(), "--delete-folder left the folder")
        checks.expect((root / "data/coding-roots/second").is_dir(), "--delete-folder touched another root")

        # Linux: a caller who isn't HERMES_UID is refused before anything is written.
        root2 = checkout(base, env.replace(f"HERMES_UID={os.getuid()}", f"HERMES_UID={os.getuid() + 1}"))
        proc = run(root2, script, "add", "--container-path", CPATH, "--id", UUID, "--json",
                   extra={"CODING_ROOT_UNAME": "Linux"})
        out = last_json(proc)
        checks.expect(proc.returncode == 1 and out.get("error") == "owner_mismatch", f"owner check: {out}")
        checks.expect(not (root2 / "data/coding-roots/roots.json").exists(), "owner refusal wrote the registry")
        checks.expect(f"{CPATH}" not in (root2 / ".env").read_text(), "owner refusal wrote .env")

        # Text mode prints the folder and the recreate command.
        root3 = checkout(base, env)
        proc = run(root3, script, "add", "--container-path", CPATH, "--id", UUID)
        checks.expect(proc.returncode == 0 and "--force-recreate hermes-worker hermes opencode opencode-mcp" in proc.stdout,
                      f"text add: {proc.stdout}{proc.stderr}")

    checks.finish("OK synced coding roots")


if __name__ == "__main__":
    main()
