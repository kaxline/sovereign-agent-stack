#!/usr/bin/env python3
"""Every tmpfs uses options both Docker and Podman apply the same way.

Service tmpfs entries (CR 0041): Podman refuses uid= and gid= ("unknown mount
option"), and Docker refuses Podman's U, so one entry with either stops
`docker compose up` on that engine before the container exists.

Named volumes with driver_opts.type tmpfs (CR 0042): Podman takes uid= and gid=
out of a local volume's o and mounts the tmpfs root-owned, while Docker passes
o to mount whole, so the same volume has a different owner on each engine.
They get the same option list. Renders every profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import compose_config  # noqa: E402
from testkit import Checks  # noqa: E402

# Options in both Podman's processOptionsInternal (pkg/util/mount_opts.go) and
# Docker's tmpfs parser. size= and mode= take a value.
FLAGS = {"rw", "ro", "exec", "noexec", "suid", "nosuid", "dev", "nodev", "noatime"}
VALUED = {"size", "mode"}
checks = Checks()


def check_options(where: str, opts: str) -> None:
    for opt in filter(None, opts.split(",")):
        key = opt.split("=", 1)[0]
        if not (opt in FLAGS or ("=" in opt and key in VALUED)):
            checks.fail(f"{where} has option {opt!r}, which Docker and Podman do not apply the same way")


def main() -> None:
    cfg = compose_config.render("*")
    seen = 0
    for name, service in sorted((cfg.get("services") or {}).items()):
        for entry in service.get("tmpfs") or []:
            seen += 1
            path, _, opts = entry.partition(":")
            check_options(f"{name} tmpfs {path}", opts)
    if not seen:
        checks.fail("no service tmpfs found; the render is missing services")
    seen = 0
    for name, volume in sorted((cfg.get("volumes") or {}).items()):
        driver_opts = (volume or {}).get("driver_opts") or {}
        if driver_opts.get("type") == "tmpfs":
            seen += 1
            check_options(f"volume {name}", str(driver_opts.get("o", "")))
    if not seen:
        checks.fail("no tmpfs volume found; the render is missing volumes")
    checks.finish()


if __name__ == "__main__":
    main()
