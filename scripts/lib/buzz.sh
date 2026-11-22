# Buzz checkout helpers. Source from repo-root scripts after ROOT is set and
# die is defined:  source "${ROOT}/scripts/lib/buzz.sh"
# shellcheck shell=bash

# shellcheck source=lib/data-root.sh
declare -F resolve_dir_path > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/data-root.sh"

# buzz_local_dir: BUZZ_LOCAL_DIR_PATH from .env, expanded and resolved. Calls
# die when it is unset or not an existing path.
buzz_local_dir() {
  local raw dir
  raw="$(env_get BUZZ_LOCAL_DIR_PATH)"
  [[ -n "$raw" ]] || die "BUZZ_LOCAL_DIR_PATH is unset — add it to .env (path to your Buzz checkout)"
  dir="$(resolve_dir_path "$raw")"
  [[ -d "$dir" ]] || die "BUZZ_LOCAL_DIR_PATH does not exist: ${dir}"
  printf '%s\n' "$dir"
}
