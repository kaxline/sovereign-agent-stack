#!/usr/bin/env python3
"""coding-root.sh add and context-root.sh add edit the user's override as before.

Runs both scripts in a scratch checkout built from the example override, so
the expected files below are the bytes a user gets on a first add. Also
checks that sync-terminal-backend.sh accepts what add wrote (no second edit),
and that re-adding a root leaves .env and the override alone.
"""

from __future__ import annotations

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
    "scripts/context-root.sh",
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
    env["SYNC_TERMINAL_ROOT"] = str(root)
    return subprocess.run(["bash", *args], cwd=root, env=env, capture_output=True, text=True)


def main() -> None:
    example = (ROOT / "docker-compose.override.yml.example").read_text()
    head, sep, tail = example.partition("  opencode:\n    volumes:\n")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ws = base / "ws"
        repo = base / "repo"
        notes = base / "My Notes #1"
        for d in (ws, repo, notes):
            d.mkdir()
        env = f"HERMES_TERMINAL_BACKEND=ssh\nOPENCODE_WORKSPACE_HOST={ws}\nCODING_EXTRA_ROOTS=\nCONTEXT_EXTRA_ROOTS=\n"

        # First coding root on a missing override: copied from the example,
        # opencode gets the mount first in its list, hermes-worker is appended.
        root = checkout(base, env)
        override = root / "docker-compose.override.yml"
        proc = run(root, "scripts/coding-root.sh", "add", str(repo))
        checks.expect(proc.returncode == 0, f"coding add failed: {proc.stderr}")
        want = (
            head + sep + f"      - {repo}:{repo}\n" + tail
            + f"\n  hermes-worker:\n    volumes:\n      - {repo}:{repo}\n"
        )
        checks.expect(override.read_text() == want, f"coding add wrote:\n{override.read_text()}")
        checks.expect(f"CODING_EXTRA_ROOTS=:{repo}\n" in (root / ".env").read_text(), "CODING_EXTRA_ROOTS not set")
        checks.expect("added service hermes-worker with mount" in proc.stdout, f"coding add said: {proc.stdout}")
        checks.expect("--force-recreate hermes-worker hermes opencode opencode-mcp" in proc.stdout, "recreate hint missing")

        # Re-adding the same root changes nothing.
        env_before, ov_before = (root / ".env").read_text(), override.read_text()
        proc = run(root, "scripts/coding-root.sh", "add", str(repo))
        checks.expect(proc.returncode == 0 and "Already covered" in proc.stdout, f"coding re-add: {proc.stdout}{proc.stderr}")
        checks.expect((root / ".env").read_text() == env_before, "coding re-add changed .env")
        checks.expect(override.read_text() == ov_before, "coding re-add changed the override")

        # A context root whose path has a space and a '#' is quoted, read-only,
        # on the terminal service only.
        proc = run(root, "scripts/context-root.sh", "add", str(notes))
        checks.expect(proc.returncode == 0, f"context add failed: {proc.stderr}")
        text = override.read_text()
        want = want.replace(
            f"  hermes-worker:\n    volumes:\n",
            f'  hermes-worker:\n    volumes:\n      - "{notes}:{notes}:ro"\n',
        )
        checks.expect(text == want, f"context add wrote:\n{text}")
        checks.expect(f"added mount for hermes-worker: {notes} (ro)" in proc.stdout, f"context add said: {proc.stdout}")

        env_before, ov_before = (root / ".env").read_text(), override.read_text()
        proc = run(root, "scripts/context-root.sh", "add", str(notes))
        checks.expect(proc.returncode == 0 and "Already a context root" in proc.stdout, f"context re-add: {proc.stdout}")
        checks.expect((root / ".env").read_text() == env_before, "context re-add changed .env")
        checks.expect(override.read_text() == ov_before, "context re-add changed the override")

        # make up runs the sync next; it must find nothing to move.
        proc = run(root, "scripts/sync-terminal-backend.sh")
        checks.expect(proc.returncode == 0, f"sync failed: {proc.stderr}")
        checks.expect("override mounts already match" in proc.stdout, f"sync re-edited add's output: {proc.stdout}")
        checks.expect(override.read_text() == ov_before, "sync changed add's override")

        # local backend: the terminal service is hermes, in an existing block.
        root = checkout(base, env.replace("=ssh", "=local"))
        override = root / "docker-compose.override.yml"
        override.write_text("services:\n  hermes:\n    environment:\n      FOO: bar\n")
        proc = run(root, "scripts/context-root.sh", "add", str(repo))
        checks.expect(proc.returncode == 0, f"local context add failed: {proc.stderr}")
        checks.expect(
            override.read_text()
            == f"services:\n  hermes:\n    volumes:\n      - {repo}:{repo}:ro\n    environment:\n      FOO: bar\n",
            f"local context add wrote:\n{override.read_text()}",
        )

        # local backend coding add: hermes once, no hermes-worker.
        notes2 = base / "local-repo"
        notes2.mkdir()
        proc = run(root, "scripts/coding-root.sh", "add", str(notes2))
        checks.expect(proc.returncode == 0, f"local coding add failed: {proc.stderr}")
        checks.expect("--force-recreate hermes opencode opencode-mcp\n" in proc.stdout, f"local recreate hint: {proc.stdout}")

        # remove undoes add: no bind left on either service, .env back to empty,
        # the repo folder untouched. A second remove and an unknown path change
        # nothing; the primary root is refused.
        root = checkout(base, env)
        override = root / "docker-compose.override.yml"
        run(root, "scripts/coding-root.sh", "add", str(repo))
        proc = run(root, "scripts/coding-root.sh", "remove", str(repo))
        checks.expect(proc.returncode == 0, f"coding remove failed: {proc.stderr}")
        text = override.read_text()
        checks.expect(f"{repo}:{repo}" not in text, f"coding remove left a bind:\n{text}")
        checks.expect("  hermes-worker:\n" not in text, f"coding remove left an empty hermes-worker block:\n{text}")
        checks.expect("  opencode:\n    volumes:\n      - ./compose/opencode/blank:" in text, f"coding remove dropped opencode's shadow mounts:\n{text}")
        checks.expect("CODING_EXTRA_ROOTS=\n" in (root / ".env").read_text(), "coding remove left CODING_EXTRA_ROOTS set")
        checks.expect("--force-recreate hermes-worker hermes opencode opencode-mcp" in proc.stdout, f"remove recreate hint: {proc.stdout}")
        checks.expect(repo.is_dir(), "coding remove deleted the folder")
        env_before, ov_before = (root / ".env").read_text(), override.read_text()
        for args in ((str(repo),), (str(base / "never-added"),)):
            proc = run(root, "scripts/coding-root.sh", "remove", *args)
            checks.expect(proc.returncode == 0 and "Not an extra coding root" in proc.stdout, f"remove {args}: {proc.stdout}{proc.stderr}")
        checks.expect((root / ".env").read_text() == env_before and override.read_text() == ov_before, "no-op remove changed a file")
        proc = run(root, "scripts/coding-root.sh", "remove", str(ws))
        checks.expect(proc.returncode != 0 and "primary coding root" in proc.stderr, f"primary remove: {proc.stdout}{proc.stderr}")

        # Removing one of two roots keeps the other, and on the local backend
        # keeps the rest of the hermes block.
        other = base / "other-repo"
        other.mkdir()
        root = checkout(base, env.replace("=ssh", "=local"))
        override = root / "docker-compose.override.yml"
        override.write_text("services:\n  hermes:\n    environment:\n      FOO: bar\n")
        run(root, "scripts/coding-root.sh", "add", str(repo))
        run(root, "scripts/coding-root.sh", "add", str(other))
        proc = run(root, "scripts/coding-root.sh", "remove", str(repo))
        checks.expect(proc.returncode == 0, f"local remove failed: {proc.stderr}")
        checks.expect(f"CODING_EXTRA_ROOTS=:{other}\n" in (root / ".env").read_text(), "local remove dropped the other root")
        checks.expect(
            override.read_text()
            == f"services:\n  hermes:\n    volumes:\n      - {other}:{other}\n    environment:\n      FOO: bar\n"
            f"\n  opencode:\n    volumes:\n      - {other}:{other}\n",
            f"local remove wrote:\n{override.read_text()}",
        )

        # A bad backend stops before .env or the override is touched.
        root = checkout(base, env.replace("=ssh", "=docker"))
        proc = run(root, "scripts/coding-root.sh", "add", str(repo))
        checks.expect(proc.returncode != 0, "coding add accepted HERMES_TERMINAL_BACKEND=docker")
        checks.expect("CODING_EXTRA_ROOTS=\n" in (root / ".env").read_text(), "bad backend still wrote .env")
        checks.expect(not (root / "docker-compose.override.yml").exists(), "bad backend still wrote the override")

    checks.finish("OK coding-root and context-root add")


if __name__ == "__main__":
    main()
