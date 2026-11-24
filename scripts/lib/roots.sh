# Shared by coding-root.sh and context-root.sh. The caller sets ROOT and
# OVERRIDE, defines die and busy (lock's failure: busy MESSAGE [JSON_MEMBERS]),
# and sources lib/env.sh first.

# shellcheck source=lib/python.sh
declare -F fast_python > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/python.sh"
# shellcheck source=lib/data-root.sh
declare -F resolve_data_root_path > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/data-root.sh"
# shellcheck source=lib/stack-lock.sh
declare -F stack_lock > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/stack-lock.sh"

# split_roots LIST...: each entry of the colon lists, one per line, skipping
# empty ones.
split_roots() {
  local IFS=':' root
  # shellcheck disable=SC2048,SC2086
  for root in $*; do
    [[ -z "$root" ]] || printf '%s\n' "$root"
  done
}

# path_under_roots CANDIDATE ROOT...: true if CANDIDATE is a ROOT or inside
# one. Each ROOT may be a colon list; empty entries are skipped.
path_under_roots() {
  local candidate="$1" root
  shift
  while IFS= read -r root; do
    [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]] && return 0
  done < <(split_roots "$@")
  return 1
}

# lock: takes the stack's config lock until exit, or ends with busy.
lock() {
  local lockfile rc=0 who
  lockfile="$(resolve_data_root_path)/addons/.lock"
  stack_lock "$lockfile" || rc=$?
  case "$rc" in
    0) trap stack_unlock EXIT ;;
    2) busy "${lockfile} is not a lock symlink; remove it if no change is running" ;;
    *)
      who="$(stack_lock_busy_text)"
      busy "another config change is running${who:+ (${who})}" ",\"holder\":$(stack_lock_holder_json)" ;;
  esac
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

# add_mount SERVICE SPEC [--ro]: one bind under services.SERVICE. SPEC is
# PATH for a same-path bind, or HOST:CONTAINER.
add_mount() {
  fast_python - add "$OVERRIDE" "$@" < "${ROOT}/scripts/lib/override_mounts.py"
}

# remove_mount SERVICE SPEC [--ro]: drop that bind from services.SERVICE.
remove_mount() {
  fast_python - remove "$OVERRIDE" "$@" < "${ROOT}/scripts/lib/override_mounts.py"
}
