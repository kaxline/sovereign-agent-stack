#!/usr/bin/env bash
# Project coding and context roots onto the active terminal service.
# Called by make ensure-local and scripts/setup.sh.
#
# HERMES_TERMINAL_BACKEND=ssh (default) uses hermes-worker.
# HERMES_TERMINAL_BACKEND=local uses hermes and drops an empty hermes-worker block.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests may point SYNC_TERMINAL_ROOT at a temp directory with its own .env.
ROOT="${SYNC_TERMINAL_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$ROOT"

# shellcheck source=lib/env.sh
source "$SCRIPT_DIR/lib/env.sh"
# shellcheck source=lib/python.sh
source "$SCRIPT_DIR/lib/python.sh"

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ ! -f .env ]]; then
  die "missing .env — run ./scripts/setup.sh"
fi

backend="$(env_get HERMES_TERMINAL_BACKEND)"
if [[ -z "$backend" ]]; then
  upsert_env .env HERMES_TERMINAL_BACKEND ssh
  backend=ssh
  log "Set HERMES_TERMINAL_BACKEND=ssh"
fi

case "$backend" in
  ssh|local) ;;
  *) die "HERMES_TERMINAL_BACKEND must be ssh or local (got ${backend})" ;;
esac

coding="$(env_get CODING_EXTRA_ROOTS)"
context="$(env_get CONTEXT_EXTRA_ROOTS)"
override="$ROOT/docker-compose.override.yml"

require_python3

python3 - "$override" "$backend" "$coding" "$context" <<'PY'
import sys
from pathlib import Path

override = Path(sys.argv[1])
backend = sys.argv[2]
coding_raw = sys.argv[3]
context_raw = sys.argv[4]

active = "hermes" if backend == "local" else "hermes-worker"
inactive = "hermes-worker" if backend == "local" else "hermes"


def split_roots(raw: str) -> list[str]:
    return [part for part in raw.split(":") if part]


coding = split_roots(coding_raw)
context = [path for path in split_roots(context_raw) if path not in coding]


def load_lines() -> list[str]:
    if not override.exists():
        return ["services:\n"]
    text = override.read_text()
    if "services:" not in text:
        text = "services:\n" + text
    if text and not text.endswith("\n"):
        text += "\n"
    return text.splitlines(keepends=True)


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


def ensure_mount(lines: list[str], service: str, path: str, readonly: bool) -> list[str]:
    spec = mount_spec(path, readonly)
    entry = format_entry(path, readonly)
    idx = find_service(lines, service)
    if idx is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.extend([f"  {service}:\n", "    volumes:\n", entry])
        print(f"added service {service} with mount {path}")
        return lines
    end = service_end(lines, idx)
    block = lines[idx:end]
    if any(spec_of(line) == spec for line in block):
        return lines
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
    print(f"added mount for {service}: {path}")
    return lines


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


lines = load_lines()
changed = False
before = "".join(lines)

for path in coding:
    lines = remove_mount(lines, inactive, path, False)
    lines = ensure_mount(lines, active, path, False)
for path in context:
    lines = remove_mount(lines, inactive, path, True)
    lines = ensure_mount(lines, active, path, True)

for service in (active, inactive):
    lines = strip_empty_volumes(lines, service)

if backend == "local":
    lines = drop_service_if_no_volumes(lines, "hermes-worker")
else:
    # An override hermes block that only existed for roots we just moved.
    lines = drop_service_if_no_volumes(lines, "hermes")

text = "".join(lines)
if text != before:
    changed = True

if not changed:
    print(f"terminal backend {backend}: override mounts already match")
    raise SystemExit(0)

# Do not create an override that only contains "services:".
body = text.strip()
if body in ("", "services:"):
    if override.exists():
        override.unlink()
        print(f"removed empty {override.name}")
    raise SystemExit(0)

override.write_text(text if text.endswith("\n") else text + "\n")
print(f"updated {override}")
PY
