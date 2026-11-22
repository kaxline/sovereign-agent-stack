# Helpers for the projects folder (ASSISTANT_DATA_ROOT/projects).
# Needs lib/env.sh and lib/data-root.sh.
# shellcheck shell=bash

# project_source_mounts PROJECTS_DIR: sets PROJECT_SOURCE_MOUNTS to the
# stack_container --mount options a container needs to see what the projects'
# sources.yaml files may point into: the projects folder and every existing
# coding and context root. stack_python ignores them on the host.
project_source_mounts() {
  local src srcs
  PROJECT_SOURCE_MOUNTS=(--mount "$1")
  IFS=':' read -r -a srcs <<< "$(env_get OPENCODE_WORKSPACE_HOST):$(env_get CODING_EXTRA_ROOTS):$(env_get CONTEXT_EXTRA_ROOTS)"
  for src in ${srcs[@]+"${srcs[@]}"}; do
    [[ -n "$src" ]] || continue
    src="$(expand_user_path "$src")"
    if [[ -d "$src" ]]; then
      PROJECT_SOURCE_MOUNTS+=(--mount "$src")
    fi
  done
}
