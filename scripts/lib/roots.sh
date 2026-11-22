# Shared by coding-root.sh and context-root.sh. The caller sets ROOT and
# OVERRIDE, defines die, and sources lib/env.sh first.

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

terminal_service() {
  local backend
  backend="$(env_get HERMES_TERMINAL_BACKEND ssh)"
  case "$backend" in
    ssh) printf '%s\n' hermes-worker ;;
    local) printf '%s\n' hermes ;;
    *) die "HERMES_TERMINAL_BACKEND must be ssh or local (got ${backend})" ;;
  esac
}

ensure_override() {
  if [[ ! -f "$OVERRIDE" ]]; then
    cp "${ROOT}/docker-compose.override.yml.example" "$OVERRIDE"
    echo "Created $OVERRIDE from example"
  fi
}

# add_mount SERVICE PATH [--ro]: one same-path bind under services.SERVICE.
add_mount() {
  python3 "${ROOT}/scripts/lib/override_mounts.py" add "$OVERRIDE" "$@"
}

# remove_mount SERVICE PATH [--ro]: drop that bind from services.SERVICE.
remove_mount() {
  python3 "${ROOT}/scripts/lib/override_mounts.py" remove "$OVERRIDE" "$@"
}
