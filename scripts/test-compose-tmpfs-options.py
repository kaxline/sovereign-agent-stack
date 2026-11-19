#!/usr/bin/env python3
"""Every service tmpfs uses options both Docker and Podman accept (CR 0041).

Podman refuses uid= and gid= on a service tmpfs ("unknown mount option"), and
Docker refuses Podman's U, so one entry with either stops `docker compose up`
on that engine before the container exists. Renders every profile.
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


def main() -> None:
    services = compose_config.render("*").get("services") or {}
    seen = 0
    for name, service in sorted(services.items()):
        for entry in service.get("tmpfs") or []:
            seen += 1
            path, _, opts = entry.partition(":")
            for opt in filter(None, opts.split(",")):
                key = opt.split("=", 1)[0]
                if not (opt in FLAGS or ("=" in opt and key in VALUED)):
                    checks.fail(f"{name} tmpfs {path} has option {opt!r}, which Docker or Podman refuses")
    if not seen:
        checks.fail("no service tmpfs found; the render is missing services")
    checks.finish()


if __name__ == "__main__":
    main()
