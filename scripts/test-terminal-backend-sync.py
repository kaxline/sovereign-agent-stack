#!/usr/bin/env python3
"""Move extra roots between hermes and hermes-worker without touching other binds."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync-terminal-backend.sh"

NOTES = "/Users/you/Notes/Factland Foundation"
REPO = "/Users/you/other-repo"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)


def write_env(directory: Path, backend: str | None, coding: str, context: str) -> None:
    lines = [
        f"CODING_EXTRA_ROOTS={coding}",
        f"CONTEXT_EXTRA_ROOTS={context}",
    ]
    if backend is not None:
        lines.insert(0, f"HERMES_TERMINAL_BACKEND={backend}")
    (directory / ".env").write_text("\n".join(lines) + "\n")


def run_sync(directory: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SYNC_TERMINAL_ROOT"] = str(directory)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
    )


def service_block(text: str, name: str) -> str:
    lines = text.splitlines()
    header = f"  {name}:"
    start = None
    for i, line in enumerate(lines):
        if line == header:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            end = i
            break
    return "\n".join(lines[start:end])


def main() -> None:
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        override = root / "docker-compose.override.yml"
        override.write_text(
            "services:\n"
            "  opencode:\n"
            "    volumes:\n"
            f"      - ./compose/opencode/blank:{REPO}/.env:ro\n"
            "  hermes:\n"
            "    volumes:\n"
            f'      - "{NOTES}:{NOTES}:ro"\n'
        )
        write_env(root, "ssh", f":{REPO}", f":{NOTES}")
        proc = run_sync(root)
        if proc.returncode != 0:
            errors.append(f"ssh sync failed: {proc.stderr or proc.stdout}")
        else:
            text = override.read_text()
            opencode = service_block(text, "opencode")
            worker = service_block(text, "hermes-worker")
            hermes = service_block(text, "hermes")
            if f"{REPO}/.env:ro" not in opencode:
                errors.append("ssh sync dropped the opencode secret shadow")
            worker_text = worker.replace('"', "")
            if f"{NOTES}:{NOTES}:ro" not in worker_text:
                errors.append("ssh sync did not put the context root on hermes-worker")
            if hermes:
                errors.append("ssh sync left a hermes service after moving its only mount")
            coding_lines = [
                line.strip()
                for line in worker_text.splitlines()
                if REPO in line and NOTES not in line
            ]
            if not any(line.endswith(f"{REPO}:{REPO}") for line in coding_lines):
                errors.append("ssh sync did not put the coding root on hermes-worker")

        override.write_text(
            "services:\n"
            "  hermes-worker:\n"
            "    volumes:\n"
            f"      - {REPO}:{REPO}\n"
            f"      - {NOTES}:{NOTES}:ro\n"
            "  opencode:\n"
            "    volumes:\n"
            f"      - {REPO}:{REPO}\n"
        )
        write_env(root, "local", f":{REPO}", f":{NOTES}")
        proc = run_sync(root)
        if proc.returncode != 0:
            errors.append(f"local sync failed: {proc.stderr or proc.stdout}")
        else:
            text = override.read_text()
            worker = service_block(text, "hermes-worker")
            hermes = service_block(text, "hermes")
            opencode = service_block(text, "opencode")
            if worker:
                errors.append("local sync left a hermes-worker service")
            if f"{REPO}:{REPO}" not in hermes.replace('"', ""):
                errors.append("local sync did not put the coding root on hermes")
            if f"{NOTES}:{NOTES}:ro" not in hermes.replace('"', ""):
                errors.append("local sync did not put the context root on hermes")
            if f"{REPO}:{REPO}" not in opencode:
                errors.append("local sync dropped the opencode coding root")

        write_env(root, "docker", "", "")
        proc = run_sync(root)
        if proc.returncode == 0:
            errors.append("invalid backend was accepted")

        write_env(root, None, "", "")
        proc = run_sync(root)
        if proc.returncode != 0:
            errors.append(f"defaulting backend failed: {proc.stderr or proc.stdout}")
        elif "HERMES_TERMINAL_BACKEND=ssh" not in (root / ".env").read_text():
            errors.append("missing backend was not defaulted to ssh")

    if errors:
        for err in errors:
            fail(err)
        raise SystemExit(1)
    print("OK terminal backend override sync")


if __name__ == "__main__":
    main()
