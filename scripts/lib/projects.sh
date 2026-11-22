# Helpers for the projects folder (ASSISTANT_DATA_ROOT/projects).
# Needs ROOT, lib/env.sh, lib/data-root.sh and lib/python.sh.
# shellcheck shell=bash

# shellcheck source=lib/coding-roots.sh
declare -F coding_root_pairs > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/coding-roots.sh"

# project_source_mounts PROJECTS_DIR: sets PROJECT_SOURCE_MOUNTS to the
# stack_container --mount options a container needs to see what the projects'
# sources.yaml files may point into: the projects folder and every existing
# coding and context root. A synced coding root is its host folder.
# stack_python ignores them on the host.
project_source_mounts() {
  local src srcs coding
  PROJECT_SOURCE_MOUNTS=(--mount "$1")
  coding="$(coding_root_pairs 2> /dev/null | cut -f1 | tr '\n' ':')" || coding=""
  IFS=':' read -r -a srcs <<< "$(env_get OPENCODE_WORKSPACE_HOST):${coding}:$(env_get CONTEXT_EXTRA_ROOTS)"
  for src in ${srcs[@]+"${srcs[@]}"}; do
    [[ -n "$src" ]] || continue
    src="$(expand_user_path "$src")"
    if [[ -d "$src" ]]; then
      PROJECT_SOURCE_MOUNTS+=(--mount "$src")
    fi
  done
}
