#!/usr/bin/env python3
"""compose/hermes/09-install-worker-key.sh on the host, twice, with a stub
ssh-keyscan: the worker's entries are replaced, not appended on every start,
and entries for other hosts stay. The script's absolute paths are rewritten
into a scratch root; `id` is stubbed so `id -u hermes` resolves to this user."""

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
SCRIPT = ROOT / "compose" / "hermes" / "09-install-worker-key.sh"

KEY_A = "hermes-worker ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKeyAKeyAKeyAKeyAKeyAKeyAKeyAKeyAKeyAKeyA"
KEY_A_RSA = "hermes-worker ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQRsaKeyRsaKeyRsaKeyRsaKey"
KEY_B = "hermes-worker ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKeyBKeyBKeyBKeyBKeyBKeyBKeyBKeyBKeyBKeyB"
OTHER = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def main() -> None:
    if shutil.which("ssh-keygen") is None:
        print("SKIP ssh-keygen not on PATH")
        sys.exit(77)
    checks = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (root / "opt" / "hermes-ssh").mkdir(parents=True)
        (root / "opt" / "hermes-ssh" / "id_ed25519").write_text("fake key\n")
        scan_out = root / "scan.txt"
        scan_rc = root / "scan.rc"

        script = SCRIPT.read_text().replace("/opt/", f"{root}/opt/").replace("/var/lib/", f"{root}/var/lib/")
        script_path = root / "09-install-worker-key.sh"
        script_path.write_text(script)

        uid, gid = os.getuid(), os.getgid()
        (bin_dir / "id").write_text(
            f"#!/bin/sh\ncase \"$1\" in -u) echo {uid};; -g) echo {gid};; *) exec /usr/bin/id \"$@\";; esac\n")
        (bin_dir / "ssh-keyscan").write_text(
            f"#!/bin/sh\ncat '{scan_out}'\nexit \"$(cat '{scan_rc}')\"\n")
        for f in ("id", "ssh-keyscan"):
            (bin_dir / f).chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")

        known = root / "opt" / "data" / ".ssh" / "known_hosts"

        def run(keys: list[str], rc: int = 0) -> list[str]:
            scan_out.write_text("".join(k + "\n" for k in keys))
            scan_rc.write_text(f"{rc}\n")
            proc = subprocess.run(["sh", str(script_path)], env=env, capture_output=True, text=True)
            checks.expect(proc.returncode == 0, f"script exited {proc.returncode}: {proc.stderr}")
            return known.read_text().splitlines() if known.exists() else []

        known.parent.mkdir(parents=True)
        known.write_text(OTHER + "\n")

        first = run([KEY_A, KEY_A_RSA])
        second = run([KEY_A, KEY_A_RSA])
        checks.expect(len(second) == len(set(second)), f"duplicate lines after two starts: {second}")
        checks.expect(sorted(second) == sorted([OTHER, KEY_A, KEY_A_RSA]),
                      f"two starts leave one entry per worker key plus the other host: {second}")
        checks.expect(first == second, f"second start changed the file: {first} -> {second}")

        rebuilt = run([KEY_B])
        checks.expect(sorted(rebuilt) == sorted([OTHER, KEY_B]),
                      f"a rebuilt worker's key replaces the old ones: {rebuilt}")

        failed = run([], rc=1)
        checks.expect(sorted(failed) == sorted([OTHER, KEY_B]),
                      f"a failed scan leaves existing entries alone: {failed}")
        checks.expect(not Path(f"{known}.old").exists(), "no known_hosts.old left behind")
        checks.expect((known.stat().st_mode & 0o777) == 0o600, "known_hosts is mode 600")

    checks.finish("OK worker known_hosts stays one entry per key")


if __name__ == "__main__":
    main()
