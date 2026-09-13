# Shared helpers for ASSISTANT_DATA_ROOT. Source from repo scripts after ROOT is set.
# shellcheck shell=bash

# Subdirectories that live under ASSISTANT_DATA_ROOT (not ./data/hermes).
USER_DATA_SUBDIRS=(projects voice memory inputs rag_storage corpora)

data_root_env_get() {
  local key="$1"
  local default="${2:-}"
  local val=""
  if [[ -f "${ROOT}/.env" ]]; then
    val="$(grep -E "^${key}=" "${ROOT}/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
  fi
  if [[ -z "$val" ]]; then
    echo "$default"
  else
    echo "$val"
  fi
}

# Expand ~ and relative paths to absolute. Empty → default ./data under ROOT.
resolve_data_root_path() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    raw="$(data_root_env_get ASSISTANT_DATA_ROOT ./data)"
  fi
  # Expand leading ~
  if [[ "$raw" == "~" ]]; then
    raw="$HOME"
  elif [[ "$raw" == "~/"* ]]; then
    raw="$HOME/${raw#~/}"
  fi
  if [[ "$raw" != /* ]]; then
    # Relative to repo root
    raw="${ROOT}/${raw#./}"
  fi
  # Normalize (resolve .. and symlinks when the path exists; else clean manually)
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    # Strip trailing slash for consistency; parent may not exist yet
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
