# shellcheck shell=bash
# Coding roots as host/container pairs, for whatever turns them into binds or
# host paths: sync-terminal-backend.sh, coding-root.sh, doctor.sh and
# lib/projects.sh. See scripts/lib/coding_roots.py for the synced root
# registry. Needs ROOT set, and lib/env.sh, lib/data-root.sh and lib/python.sh.
#
# bash 3.2 compatible.

CODING_ROOTS_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/coding_roots.py"

# coding_roots_dir: where synced roots' folders and the registry live.
coding_roots_dir() {
  printf '%s/coding-roots\n' "$(resolve_data_root_path)"
}

coding_roots_registry() {
  printf '%s/roots.json\n' "$(coding_roots_dir)"
}

# coding_roots_py [stack_container options] ARGS...: coding_roots.py through
# fast_python, the script read from stdin.
coding_roots_py() {
  local opts=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mount | --env | --network-of) opts+=("$1" "$2"); shift 2 ;;
      *) break ;;
    esac
  done
  fast_python ${opts[@]+"${opts[@]}"} - "$@" < "$CODING_ROOTS_PY"
}

# coding_root_pairs: host<TAB>container per CODING_EXTRA_ROOTS entry. A plain
# entry is bound at its own path; a synced root's host folder comes from the
# registry. Without a registry no python runs. Returns 1 when the registry
# can't be read (no python3 on the host and no Hermes image).
coding_root_pairs() {
  local extras reg p
  extras="$(env_get CODING_EXTRA_ROOTS)"
  reg="$(coding_roots_registry)"
  if [[ -s "$reg" ]]; then
    coding_roots_py pairs "$extras" "$(cat "$reg")"
    return
  fi
  local IFS=':'
  for p in $extras; do
    [[ -n "$p" ]] && printf '%s\t%s\n' "$p" "$p"
  done
  return 0
}
