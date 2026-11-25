# Shared helpers for ASSISTANT_DATA_ROOT. Source from repo scripts after ROOT is set.
# shellcheck shell=bash

# Subdirectories that live under ASSISTANT_DATA_ROOT (not ./data/hermes).
USER_DATA_SUBDIRS=(projects voice memory inputs rag_storage corpora)

# shellcheck source=lib/env.sh
declare -F env_get_file > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# data_root_env_get KEY [DEFAULT]: env_get on ${ROOT}/.env, whatever the
# current directory or ENV_FILE.
data_root_env_get() {
  env_get_file "${ROOT}/.env" "$@"
}

# data_root_raw: ASSISTANT_DATA_ROOT as written. The environment wins over
# ${ROOT}/.env, as in compose; default ./data.
data_root_raw() {
  printf '%s\n' "${ASSISTANT_DATA_ROOT:-$(data_root_env_get ASSISTANT_DATA_ROOT ./data)}"
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

# resolve_dir_path PATH: expand_user_path, then resolve symlinks and .. when
# PATH is an existing directory. Returns 1 on an empty PATH.
resolve_dir_path() {
  [[ -n "${1:-}" ]] || return 1
  local raw
  raw="$(expand_user_path "$1")"
  if [[ -d "$raw" ]]; then
    (cd "$raw" && pwd -P)
  else
    printf '%s\n' "$raw"
  fi
}

# resolve_data_root_path [PATH]: expand_user_path, then resolve symlinks and ..
# in PATH (or in its parent when PATH does not exist yet). Empty PATH means
# the data root (data_root_raw).
resolve_data_root_path() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    raw="$(data_root_raw)"
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

# Templates for the files setup.sh, data-dir.sh and corpus.sh put in a new data
# root: scripts/seed/data-root/ mirrors the root's layout, and the files beside
# it are seeded one at a time with seed_file.
SEED_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/seed"

# seed_file TEMPLATE DEST [NAME=VALUE]...: write DEST from TEMPLATE unless DEST
# exists, with each ${NAME} in the template replaced by VALUE. Logs "Wrote
# DEST" through the caller's log when it has one.
seed_file() {
  local template="$1" dest="$2" names="" pair tmp
  shift 2
  [[ -f "$dest" ]] && return 0
  if [[ ! -f "$template" ]]; then
    printf 'error: no seed template %s\n' "$template" >&2
    return 1
  fi
  local envs=()
  for pair in "$@"; do
    names="${names} ${pair%%=*}"
    envs+=("SEED_VAL_${pair%%=*}=${pair#*=}")
  done
  mkdir -p "$(dirname "$dest")"
  tmp="$(mktemp "$(dirname "$dest")/.$(basename "$dest").XXXXXX")"
  # awk's ENVIRON, not -v, so backslashes in a value stay as written.
  if ! env ${envs[@]+"${envs[@]}"} awk -v names="$names" '
    function replace_all(s, pat, rep,    out, i) {
      out = ""
      while ((i = index(s, pat)) > 0) {
        out = out substr(s, 1, i - 1) rep
        s = substr(s, i + length(pat))
      }
      return out s
    }
    BEGIN { n = split(names, keys, " ") }
    {
      line = $0
      for (k = 1; k <= n; k++) line = replace_all(line, "${" keys[k] "}", ENVIRON["SEED_VAL_" keys[k]])
      print line
    }' "$template" > "$tmp" || ! mv "$tmp" "$dest"; then
    rm -f "$tmp"
    return 1
  fi
  if declare -F log > /dev/null; then log "Wrote $dest"; else printf 'Wrote %s\n' "$dest"; fi
}

# seed_data_root ROOT: the USER_DATA_SUBDIRS folders under ROOT, plus every
# file in scripts/seed/data-root/ that ROOT doesn't have yet. ${DATA_ROOT} in a
# template becomes ROOT. Existing files are never touched.
seed_data_root() {
  local root="$1" name rel
  if [[ ! -d "${SEED_DIR}/data-root" ]]; then
    printf 'error: no seed templates at %s\n' "${SEED_DIR}/data-root" >&2
    return 1
  fi
  for name in "${USER_DATA_SUBDIRS[@]}"; do
    mkdir -p "${root}/${name}"
  done
  mkdir -p "${root}/memory/notes"
  while IFS= read -r rel; do
    seed_file "${SEED_DIR}/data-root/${rel}" "${root}/${rel}" "DATA_ROOT=${root}" || return 1
  done < <(cd "${SEED_DIR}/data-root" && find . -type f ! -name '.*' | sed 's|^\./||' | LC_ALL=C sort)
}
