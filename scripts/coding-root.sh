#!/usr/bin/env bash
# Manage shared coding roots for Hermes + OpenCode.
#
# Primary root: OPENCODE_WORKSPACE_HOST (mounted on the terminal backend and opencode).
# Extra roots: colon-prefixed entries in CODING_EXTRA_ROOTS (.env) plus matching
# bind mounts under the terminal service (hermes-worker, or hermes when
# HERMES_TERMINAL_BACKEND=local) and services.opencode in
# docker-compose.override.yml (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
OVERRIDE="${ROOT}/docker-compose.override.yml"
OVERRIDE_EXAMPLE="${ROOT}/docker-compose.override.yml.example"

die() { echo "error: $*" >&2; exit 1; }

env_get() {
  local key="$1" default="${2:-}"
  [[ -f "$ENV_FILE" ]] || { echo "$default"; return; }
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 || true)"
  if [[ -z "$line" ]]; then
    echo "$default"
    return
  fi
  local val="${line#*=}"
  val="${val%\"}"
  val="${val#\"}"
  val="${val%\'}"
  val="${val#\'}"
  echo "$val"
}

upsert_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      $0 ~ "^" k "=" { print k "=" v; done=1; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

validate_path() {
  local path="$1"
  [[ -n "$path" ]] || die "path is required"
  [[ "$path" == /* ]] || die "path must be absolute: $path"
  [[ "$path" != "/" ]] || die "path must not be /"
  [[ "$path" != */ ]] || die "path must not end with a slash: $path"
  local home="${HOME:-}"
  [[ -n "$home" && "$path" == "$home" ]] && die "path must not be \$HOME alone (too broad): $path"
  [[ -e "$path" ]] || die "path does not exist: $path"
  [[ -d "$path" ]] || die "path must be a directory: $path"
}

path_covered() {
  local candidate="$1"
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  [[ -n "$primary" ]] || return 1
  if [[ "$candidate" == "$primary" || "$candidate" == "$primary"/* ]]; then
    return 0
  fi
  local IFS=':'
  local root
  # shellcheck disable=SC2086
  for root in $extras; do
    [[ -z "$root" ]] && continue
    if [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]; then
      return 0
    fi
  done
  return 1
}

ensure_override() {
  if [[ ! -f "$OVERRIDE" ]]; then
    cp "$OVERRIDE_EXAMPLE" "$OVERRIDE"
    echo "Created $OVERRIDE from example"
  fi
}

# Ensure a volume line exists under services.<svc>.volumes in the override file.
ensure_service_mount() {
  local svc="$1" mount_path="$2"
  local tmp
  tmp="$(mktemp)"

  python3 - "$OVERRIDE" "$svc" "$mount_path" "$tmp" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1])
service = sys.argv[2]
mount_path = sys.argv[3]
out = Path(sys.argv[4])
entry = f"      - {mount_path}:{mount_path}"

text = src.read_text() if src.exists() else "services:\n"
if "services:" not in text:
    text = "services:\n" + text
lines = text.splitlines(keepends=True)

def find_service(lines, name):
    header = f"  {name}:"
    for i, line in enumerate(lines):
        if line.rstrip("\n") == header:
            return i
    return None

def service_end(lines, start):
    i = start + 1
    while i < len(lines):
        line = lines[i]
        # Next top-level service under services: (two-space indent, not empty)
        if line.startswith("  ") and not line.startswith("    ") and line.strip() and line.rstrip("\n").endswith(":"):
            return i
        i += 1
    return len(lines)

idx = find_service(lines, service)
if idx is None:
    if not text.rstrip().endswith("\n"):
        lines.append("\n")
    lines.append(f"  {service}:\n")
    lines.append("    volumes:\n")
    lines.append(entry + "\n")
    out.write_text("".join(lines))
    print(f"added service {service} with mount {mount_path}")
    raise SystemExit(0)

end = service_end(lines, idx)
block = lines[idx:end]
# already present?
for line in block:
    if mount_path + ":" + mount_path in line.replace(" ", ""):
        out.write_text("".join(lines))
        print(f"mount already present for {service}: {mount_path}")
        raise SystemExit(0)
    if f"{mount_path}:{mount_path}" in line:
        out.write_text("".join(lines))
        print(f"mount already present for {service}: {mount_path}")
        raise SystemExit(0)

vol_rel = None
for j, line in enumerate(block):
    if line.rstrip("\n") == "    volumes:":
        vol_rel = j
        break

if vol_rel is None:
    # Insert volumes after service header
    insert_at = idx + 1
    lines.insert(insert_at, "    volumes:\n")
    lines.insert(insert_at + 1, entry + "\n")
else:
    lines.insert(idx + vol_rel + 1, entry + "\n")

out.write_text("".join(lines))
print(f"added mount for {service}: {mount_path}")
PY
  mv "$tmp" "$OVERRIDE"
}

terminal_service() {
  local backend
  backend="$(env_get HERMES_TERMINAL_BACKEND ssh)"
  case "$backend" in
    ssh) printf '%s\n' hermes-worker ;;
    local) printf '%s\n' hermes ;;
    *) die "HERMES_TERMINAL_BACKEND must be ssh or local (got ${backend})" ;;
  esac
}

ensure_override_mount() {
  local path="$1"
  ensure_override
  ensure_service_mount "$(terminal_service)" "$path"
  ensure_service_mount opencode "$path"
}

cmd_list() {
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST '(unset)')"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  echo "Primary coding root (OPENCODE_WORKSPACE_HOST):"
  echo "  $primary"
  echo "Extra coding roots (CODING_EXTRA_ROOTS):"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    echo "  (none)"
  else
    local IFS=':'
    local root
    # shellcheck disable=SC2086
    for root in $extras; do
      [[ -z "$root" ]] && continue
      echo "  $root"
    done
  fi
  if [[ -f "$OVERRIDE" ]]; then
    echo "Override file: $OVERRIDE"
  else
    echo "Override file: (missing — will be created on add)"
  fi
}

cmd_add() {
  local path="${1:-}"
  validate_path "$path"
  [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — run ./scripts/setup.sh first"

  local primary
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  [[ -n "$primary" ]] || die "OPENCODE_WORKSPACE_HOST is unset"

  if path_covered "$path"; then
    echo "Already covered by an existing coding root: $path"
    cmd_list
    return 0
  fi

  local extras
  extras="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    extras=":$path"
  else
    case ":${extras#:}:" in
      *:"$path":*) echo "Already in CODING_EXTRA_ROOTS: $path"; cmd_list; return 0 ;;
    esac
    extras="${extras}:$path"
  fi
  [[ "$extras" == :* ]] || extras=":$extras"

  upsert_env CODING_EXTRA_ROOTS "$extras"
  ensure_override_mount "$path"

  echo
  echo "Added coding root: $path"
  echo "CODING_EXTRA_ROOTS=$extras"
  echo
  echo "Recreate containers so the new bind mounts apply:"
  echo "  docker compose up -d --force-recreate $(terminal_service) opencode opencode-mcp"
  echo "  # or: make down && make up"
  echo
  echo "If this tree has .env / credential files, shadow them (see docs/opencode.md):"
  echo "  find \"$path\" -name '.env' -not -path '*/node_modules/*'"
}

usage() {
  cat <<EOF
Usage: $0 <list|add DIR>

  list          Show primary and extra coding roots
  add DIR       Grant Hermes + OpenCode access to an absolute directory

EOF
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    list) cmd_list ;;
    add)
      shift
      cmd_add "${1:-}"
      ;;
    -h|--help|help) usage ;;
    "") usage; exit 1 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
