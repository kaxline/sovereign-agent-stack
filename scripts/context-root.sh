#!/usr/bin/env bash
# Manage read-only context roots for Hermes file context (not OpenCode).
#
# Extra roots: colon-prefixed entries in CONTEXT_EXTRA_ROOTS (.env) plus matching
# read-only bind mounts under the terminal service (hermes-worker, or hermes when
# HERMES_TERMINAL_BACKEND=local) in docker-compose.override.yml
# (gitignored). Paths already covered by OPENCODE_WORKSPACE_HOST or
# CODING_EXTRA_ROOTS are treated as visible (writable — coding-root wins).
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

# True if candidate is under any already-mounted coding or context root.
path_visible() {
  local candidate="$1"
  local primary extras context
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  context="$(env_get CONTEXT_EXTRA_ROOTS)"
  if [[ -n "$primary" && ( "$candidate" == "$primary" || "$candidate" == "$primary"/* ) ]]; then
    return 0
  fi
  local IFS=':'
  local root
  # shellcheck disable=SC2086
  for root in $extras $context; do
    [[ -z "$root" ]] && continue
    if [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]; then
      return 0
    fi
  done
  return 1
}

path_covered_by_coding() {
  local candidate="$1"
  local primary extras
  primary="$(env_get OPENCODE_WORKSPACE_HOST)"
  extras="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -n "$primary" && ( "$candidate" == "$primary" || "$candidate" == "$primary"/* ) ]]; then
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

terminal_service() {
  local backend
  backend="$(env_get HERMES_TERMINAL_BACKEND ssh)"
  case "$backend" in
    ssh) printf '%s\n' hermes-worker ;;
    local) printf '%s\n' hermes ;;
    *) die "HERMES_TERMINAL_BACKEND must be ssh or local (got ${backend})" ;;
  esac
}

# Ensure a read-only volume line exists under the terminal service.
ensure_hermes_ro_mount() {
  local mount_path="$1"
  local service tmp
  service="$(terminal_service)"
  tmp="$(mktemp)"

  python3 - "$OVERRIDE" "$service" "$mount_path" "$tmp" <<'PY'
import sys
from pathlib import Path

src = Path(sys.argv[1])
service = sys.argv[2]
mount_path = sys.argv[3]
out = Path(sys.argv[4])
entry = f"      - {mount_path}:{mount_path}:ro"

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
        if line.startswith("  ") and not line.startswith("    ") and line.strip() and line.rstrip("\n").endswith(":"):
            return i
        i += 1
    return len(lines)

def mount_present(block, path):
    compact = path + ":" + path
    for line in block:
        stripped = line.replace(" ", "")
        if compact in stripped:
            return True
        if f"{path}:{path}" in line:
            return True
    return False

idx = find_service(lines, service)
if idx is None:
    if not text.rstrip().endswith("\n"):
        lines.append("\n")
    lines.append(f"  {service}:\n")
    lines.append("    volumes:\n")
    lines.append(entry + "\n")
    out.write_text("".join(lines))
    print(f"added service {service} with mount {mount_path} (ro)")
    raise SystemExit(0)

end = service_end(lines, idx)
block = lines[idx:end]
if mount_present(block, mount_path):
    out.write_text("".join(lines))
    print(f"mount already present for {service}: {mount_path}")
    raise SystemExit(0)

vol_rel = None
for j, line in enumerate(block):
    if line.rstrip("\n") == "    volumes:":
        vol_rel = j
        break

if vol_rel is None:
    insert_at = idx + 1
    lines.insert(insert_at, "    volumes:\n")
    lines.insert(insert_at + 1, entry + "\n")
else:
    lines.insert(idx + vol_rel + 1, entry + "\n")

out.write_text("".join(lines))
print(f"added mount for {service}: {mount_path} (ro)")
PY
  mv "$tmp" "$OVERRIDE"
}

cmd_list() {
  local extras
  extras="$(env_get CONTEXT_EXTRA_ROOTS)"
  echo "Context roots (CONTEXT_EXTRA_ROOTS, Hermes read-only):"
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
  echo "Also visible via coding roots (writable — coding-root wins):"
  echo "  primary: $(env_get OPENCODE_WORKSPACE_HOST '(unset)')"
  local coding
  coding="$(env_get CODING_EXTRA_ROOTS)"
  if [[ -z "$coding" || "$coding" == ":" ]]; then
    echo "  extras: (none)"
  else
    echo "  extras: $coding"
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

  if path_covered_by_coding "$path"; then
    echo "Already visible via a coding root (writable): $path"
    echo "Prefer a dedicated context root for notes vaults you do not want the coding agent to edit."
    cmd_list
    return 0
  fi

  if path_visible "$path"; then
    echo "Already a context root: $path"
    cmd_list
    return 0
  fi

  local extras
  extras="$(env_get CONTEXT_EXTRA_ROOTS)"
  if [[ -z "$extras" || "$extras" == ":" ]]; then
    extras=":$path"
  else
    case ":${extras#:}:" in
      *:"$path":*) echo "Already in CONTEXT_EXTRA_ROOTS: $path"; cmd_list; return 0 ;;
    esac
    extras="${extras}:$path"
  fi
  [[ "$extras" == :* ]] || extras=":$extras"

  upsert_env CONTEXT_EXTRA_ROOTS "$extras"
  ensure_override
  ensure_hermes_ro_mount "$path"

  echo
  echo "Added context root: $path"
  echo "CONTEXT_EXTRA_ROOTS=$extras"
  echo
  echo "Recreate the terminal backend so the new read-only bind applies:"
  echo "  docker compose up -d --force-recreate $(terminal_service)"
  echo "  # or: make down && make up"
  echo
  echo "Then declare the path in the project's sources.yaml and run:"
  echo "  make project-index PROJECT=<slug>"
}

usage() {
  cat <<EOF
Usage: $0 <list|add DIR>

  list          Show Hermes context roots (read-only notes mounts)
  add DIR       Mount an absolute directory into the terminal backend read-only

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
