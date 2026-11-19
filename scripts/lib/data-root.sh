# Shared helpers for ASSISTANT_DATA_ROOT. Source from repo scripts after ROOT is set.
# shellcheck shell=bash

# Subdirectories that live under ASSISTANT_DATA_ROOT (not ./data/hermes).
USER_DATA_SUBDIRS=(projects voice memory inputs rag_storage corpora)

# shellcheck source=env.sh
declare -F env_get_file > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# data_root_env_get KEY [DEFAULT]: env_get on ${ROOT}/.env, whatever the
# current directory or ENV_FILE.
data_root_env_get() {
  env_get_file "${ROOT}/.env" "$@"
}

# expand_user_path PATH: PATH as an absolute path, without touching the disk.
# A leading ~ or ~/ becomes $HOME, a relative path is taken from ROOT (a leading
# ./ dropped) and a trailing slash is dropped. Symlinks and .. are left alone.
expand_user_path() {
  local raw="$1"
  if [[ "$raw" == "~" ]]; then
    raw="$HOME"
  elif [[ "$raw" == \~/* ]]; then
    raw="$HOME/${raw#\~/}"
  fi
  [[ "$raw" == /* ]] || raw="${ROOT}/${raw#./}"
  [[ "$raw" == / ]] || raw="${raw%/}"
  printf '%s\n' "$raw"
}

# resolve_data_root_path [PATH]: expand_user_path, then resolve symlinks and ..
# in PATH (or in its parent when PATH does not exist yet). Empty PATH means
# ASSISTANT_DATA_ROOT from ${ROOT}/.env, default ./data.
resolve_data_root_path() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    raw="$(data_root_env_get ASSISTANT_DATA_ROOT ./data)"
  fi
  raw="$(expand_user_path "$raw")"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    local parent base
    parent="$(dirname "$raw")"
    base="$(basename "$raw")"
    if [[ -d "$parent" ]]; then
      echo "$(cd "$parent" && pwd -P)/${base}"
    else
      echo "$raw"
    fi
  fi
}

validate_data_root_path() {
  local path="$1"
  [[ -n "$path" ]] || { echo "ASSISTANT_DATA_ROOT must not be empty" >&2; return 1; }
  [[ "$path" == /* ]] || { echo "ASSISTANT_DATA_ROOT must be an absolute path: $path" >&2; return 1; }
  [[ "$path" != "/" ]] || { echo "ASSISTANT_DATA_ROOT must not be /" >&2; return 1; }
  [[ "$path" != */ ]] || { echo "ASSISTANT_DATA_ROOT must not end with a slash: $path" >&2; return 1; }
  local hermes="${ROOT}/data/hermes"
  case "$path" in
    "$hermes"|"$hermes"/*)
      echo "ASSISTANT_DATA_ROOT must not be ./data/hermes (Hermes state stays there)" >&2
      return 1
      ;;
  esac
  return 0
}

path_is_inside() {
  # True if $1 is inside $2 (or equal).
  local inner="$1"
  local outer="$2"
  [[ "$inner" == "$outer" || "$inner" == "$outer"/* ]]
}
