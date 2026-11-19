# Edit Hermes profile YAML with the Python and YAML library in the Hermes image.
# Source from repo-root scripts (cwd = repo root):
#   source "${ROOT}/scripts/lib/hermes-yaml.sh"
#
# hermes_python ARGS... runs python3 from the pinned Hermes image with
# data/hermes mounted at /opt/data, reading the script from stdin. It needs no
# running container and no compose project name, so it behaves the same under
# a custom -p / COMPOSE_PROJECT_NAME and before the stack's first start.

# shellcheck source=python.sh
declare -F stack_hermes_image > /dev/null || source "$(dirname "${BASH_SOURCE[0]}")/python.sh"

hermes_python() {
  local image
  image="$(ROOT="${ROOT:-$PWD}" stack_hermes_image)"
  docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    -v "$(pwd)/data/hermes:/opt/data" \
    --entrypoint python3 \
    "$image" - "$@"
}

# hermes_set_platform_disabled PROFILE PLATFORM
# Pins platforms.<PLATFORM>.enabled=false in profiles/<PROFILE>/config.yaml
# through compose/hermes/profile_config.py, which merges a duplicate
# platforms: block that older setup runs appended. No-op when the file is
# missing.
hermes_set_platform_disabled() {
  local image
  image="$(ROOT="${ROOT:-$PWD}" stack_hermes_image)"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$(pwd)/data/hermes:/opt/data" \
    -v "$(pwd)/compose/hermes/profile_config.py:/bootstrap/profile_config.py:ro" \
    --entrypoint python3 \
    "$image" /bootstrap/profile_config.py platform-disabled \
    "/opt/data/profiles/$1/config.yaml" "$2" --name "$1"
}
