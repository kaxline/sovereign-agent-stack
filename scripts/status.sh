#!/usr/bin/env bash
# The stack's version, image pins, services and published ports, as JSON.
#
#   scripts/status.sh --json
#
# Read-only, and it works with the stack stopped. It needs the compose CLI;
# with the container engine down, every service's state is "unknown" and
# "engine" is false. Services are the ones COMPOSE_PROFILES enables.
#
# A running service's ports are what the engine published; a stopped one's come
# from the compose config.
#
# The version is the .stack-version file a release tarball carries, else
# `git describe --tags` in a checkout, else "unknown". The JSON is built by
# scripts/lib/stack_status.py, run by stack_python (the pinned Hermes image,
# or the host's python3 with STACK_PYTHON=host). No secrets are read.
#
# bash 3.2 compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"

JSON=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    -h | --help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'error: unknown option %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ "$JSON" == 1 ]] || { printf 'error: status.sh prints JSON only; pass --json\n' >&2; exit 2; }

stack_version() {
  local v=""
  if [[ -f .stack-version ]]; then
    v="$(head -1 .stack-version | tr -d '[:space:]')"
  elif [[ -e .git ]] && command -v git > /dev/null 2>&1; then
    v="$(git describe --tags --always 2> /dev/null || true)"
  fi
  printf '%s\n' "${v:-unknown}"
}

errlog="$(mktemp)"
trap 'rm -f "$errlog"' EXIT
if ! config="$(docker compose config --format json 2> "$errlog")"; then
  cat "$errlog" >&2
  printf 'error: docker compose config failed\n' >&2
  exit 1
fi

engine_args=()
if docker info > /dev/null 2>&1 && ps_out="$(docker compose ps --all --format json 2> /dev/null)"; then
  :
else
  ps_out=""
  engine_args=(--engine-down)
fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
agent="$(env_get HERMES_AGENT_IMAGE_TAG "$(compose_image_default HERMES_AGENT_IMAGE_TAG)")"
webui="$(env_get HERMES_WEBUI_IMAGE_TAG "$(compose_image_default HERMES_WEBUI_IMAGE_TAG)")"

printf '%s\n%s\n%s\n' "$config" "---compose-ps---" "$ps_out" \
  | stack_python scripts/lib/stack_status.py --version "$(stack_version)" \
    --hermes-agent "$agent" --hermes-webui "$webui" --os "$os" --arch "$arch" \
    ${engine_args[@]+"${engine_args[@]}"}
