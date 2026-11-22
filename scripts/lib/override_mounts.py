#!/usr/bin/env python3
"""Edit same-path bind mounts in a user's docker-compose.override.yml.

The file is the user's own, so it is edited as lines rather than parsed and
re-dumped: comments, order and spacing outside the touched service stay as
they are. A mount is a ``      - HOST:HOST`` (or ``:ro``) entry under
``services.<name>.volumes``.

    override_mounts.py add OVERRIDE SERVICE PATH [--ro]
        One mount on one service, for coding-root.sh and context-root.sh.
    override_mounts.py remove OVERRIDE SERVICE PATH [--ro]
        Drop that mount from one service, for coding-root.sh remove. A
        service block left with nothing else in it goes too.
    override_mounts.py sync OVERRIDE BACKEND CODING CONTEXT
        Project CODING_EXTRA_ROOTS and CONTEXT_EXTRA_ROOTS (colon lists) onto
        the active terminal service, for sync-terminal-backend.sh.

sync-terminal-backend.sh pipes this file to python3 on stdin, since in the
Hermes image only its ROOT is mounted, so it imports nothing from scripts/lib.
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_service(lines: list[str], name: str):
    header = f"  {name}:"
    for i, line in enumerate(lines):
        if line.rstrip("\n") == header:
            return i
    return None


def service_end(lines: list[str], start: int) -> int:
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if (
            line.startswith("  ")
            and not line.startswith("    ")
            and line.strip()
            and line.rstrip("\n").endswith(":")
        ):
            return i
        i += 1
    return len(lines)


def spec_of(line: str):
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None
    spec = stripped[2:].strip()
    if len(spec) >= 2 and spec[0] == spec[-1] and spec[0] in "\"'":
        spec = spec[1:-1]
    return spec


def mount_spec(path: str, readonly: bool) -> str:
    spec = f"{path}:{path}"
    if readonly:
        spec += ":ro"
    return spec


def format_entry(path: str, readonly: bool) -> str:
    spec = mount_spec(path, readonly)
    if any(ch in spec for ch in " #\t"):
        escaped = spec.replace("\\", "\\\\").replace('"', '\\"')
        spec = f'"{escaped}"'
    return f"      - {spec}\n"


def ensure_mount(
    lines: list[str], service: str, path: str, readonly: bool, for_add: bool = False
) -> tuple[list[str], str]:
    """Add the mount unless the service already has it.

    Returns the lines and what happened: "service" (appended a new service
    block), "added" or "present". for_add keeps two rules coding-root and
    context-root add have always had and sync never has: the path mounted in
    the other mode (rw for :ro, or :ro for rw) counts as present, so add never
    writes a second mount at the same target, which Compose rejects; and an
    appended service block gets a blank line before it.
    """
    spec = mount_spec(path, readonly)
    present = {spec, mount_spec(path, not readonly)} if for_add else {spec}
    entry = format_entry(path, readonly)
    idx = find_service(lines, service)
    if idx is None:
        if for_add:
            lines.append("\n")
        elif lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.extend([f"  {service}:\n", "    volumes:\n", entry])
        return lines, "service"
    end = service_end(lines, idx)
    block = lines[idx:end]
    if any(spec_of(line) in present for line in block):
        return lines, "present"
    vol_rel = None
    for j, line in enumerate(block):
        if line.rstrip("\n") == "    volumes:":
            vol_rel = j
            break
    if vol_rel is None:
        lines.insert(idx + 1, "    volumes:\n")
        lines.insert(idx + 2, entry)
    else:
        lines.insert(idx + vol_rel + 1, entry)
    return lines, "added"


def remove_mount(lines: list[str], service: str, path: str, readonly: bool) -> list[str]:
    spec = mount_spec(path, readonly)
    idx = find_service(lines, service)
    if idx is None:
        return lines
    end = service_end(lines, idx)
    kept = []
    removed = False
    for line in lines[idx:end]:
        if spec_of(line) == spec:
            removed = True
            continue
        kept.append(line)
    if removed:
        lines = lines[:idx] + kept + lines[end:]
        print(f"removed mount from {service}: {path}")
    return lines


def strip_empty_volumes(lines: list[str], service: str) -> list[str]:
    idx = find_service(lines, service)
    if idx is None:
        return lines
    end = service_end(lines, idx)
    block = lines[idx:end]
    has_mount = any(spec_of(line) is not None for line in block)
    if has_mount:
        return lines
    kept = [line for line in block if line.rstrip("\n") != "    volumes:"]
    return lines[:idx] + kept + lines[end:]


def drop_service_if_no_volumes(lines: list[str], service: str) -> list[str]:
    idx = find_service(lines, service)
    if idx is None:
        return lines
    end = service_end(lines, idx)
    block = lines[idx:end]
    if any(spec_of(line) is not None for line in block):
        return lines
    # Keep a service that still has other keys (environment, command, ...).
    for line in block[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("    ") and not line.startswith("      ") and stripped.endswith(":"):
            if stripped != "volumes:":
                return lines
    lines = lines[:idx] + lines[end:]
    print(f"removed empty {service} service from override")
    return lines


def read_text(override: Path) -> str:
    text = override.read_text() if override.exists() else "services:\n"
    if "services:" not in text:
        text = "services:\n" + text
    return text


def cmd_add(override: Path, service: str, path: str, readonly: bool) -> None:
    lines = read_text(override).splitlines(keepends=True)
    lines, result = ensure_mount(lines, service, path, readonly, for_add=True)
    ro = " (ro)" if readonly else ""
    if result == "present":
        print(f"mount already present for {service}: {path}")
        return
    override.write_text("".join(lines))
    if result == "service":
        print(f"added service {service} with mount {path}{ro}")
    else:
        print(f"added mount for {service}: {path}{ro}")


def cmd_remove(override: Path, service: str, path: str, readonly: bool) -> None:
    if not override.exists():
        return
    before = override.read_text()
    lines = remove_mount(before.splitlines(keepends=True), service, path, readonly)
    lines = strip_empty_volumes(lines, service)
    lines = drop_service_if_no_volumes(lines, service)
    text = "".join(lines)
    if text != before:
        override.write_text(text)


def split_roots(raw: str) -> list[str]:
    return [part for part in raw.split(":") if part]


def cmd_sync(override: Path, backend: str, coding_raw: str, context_raw: str) -> None:
    active = "hermes" if backend == "local" else "hermes-worker"
    inactive = "hermes-worker" if backend == "local" else "hermes"
    coding = split_roots(coding_raw)
    context = [path for path in split_roots(context_raw) if path not in coding]

    if override.exists():
        text = read_text(override)
        if text and not text.endswith("\n"):
            text += "\n"
        lines = text.splitlines(keepends=True)
    else:
        lines = ["services:\n"]
    before = "".join(lines)

    for roots, readonly in ((coding, False), (context, True)):
        for path in roots:
            lines = remove_mount(lines, inactive, path, readonly)
            lines, result = ensure_mount(lines, active, path, readonly)
            if result == "service":
                print(f"added service {active} with mount {path}")
            elif result == "added":
                print(f"added mount for {active}: {path}")

    for service in (active, inactive):
        lines = strip_empty_volumes(lines, service)

    if backend == "local":
        lines = drop_service_if_no_volumes(lines, "hermes-worker")
    else:
        # An override hermes block that only existed for roots we just moved.
        lines = drop_service_if_no_volumes(lines, "hermes")

    text = "".join(lines)
    if text == before:
        print(f"terminal backend {backend}: override mounts already match")
        return

    # Do not create an override that only contains "services:".
    body = text.strip()
    if body in ("", "services:"):
        if override.exists():
            override.unlink()
            print(f"removed empty {override.name}")
        return

    override.write_text(text if text.endswith("\n") else text + "\n")
    print(f"updated {override}")


def main(argv: list[str]) -> None:
    if len(argv) >= 4 and argv[0] == "add" and argv[4:] in ([], ["--ro"]):
        cmd_add(Path(argv[1]), argv[2], argv[3], argv[4:] == ["--ro"])
    elif len(argv) >= 4 and argv[0] == "remove" and argv[4:] in ([], ["--ro"]):
        cmd_remove(Path(argv[1]), argv[2], argv[3], argv[4:] == ["--ro"])
    elif len(argv) == 5 and argv[0] == "sync":
        cmd_sync(Path(argv[1]), argv[2], argv[3], argv[4])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
