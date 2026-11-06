# Python for stack scripts: in the Hermes image (stack_python), or a host python3 check.
# Source from repo-root scripts:  source "${ROOT}/scripts/lib/python.sh"

# Fails unless a working python3 >= 3.9 is on PATH. Runs the interpreter
# instead of trusting command -v: on a Mac without the Command Line Tools,
# /usr/bin/python3 is a stub that opens an install dialog and exits non-zero.
require_python3() {
  local hint="Install Python 3.9+ (macOS: xcode-select --install, or brew install python)"
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'error: python3 not found. %s\n' "$hint" >&2
    exit 1
  fi
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    printf 'error: python3 is missing, a stub, or older than 3.9 (%s). %s\n' \
      "$(command -v python3)" "$hint" >&2
    exit 1
  fi
}

# This checkout, whatever ROOT a caller points at (tests use a temp ROOT).
STACK_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The default tag of a compose image variable, read from docker-compose.yml so
# it has one source: compose_image_default HERMES_AGENT_IMAGE_TAG
compose_image_default() {
  sed -n "s/.*\${$1:-\([^}]*\)}.*/\1/p" "${STACK_REPO}/docker-compose.yml" | head -1
}

# The pinned Hermes image: HERMES_AGENT_IMAGE_TAG from the environment or .env,
# else compose's default.
stack_hermes_image() {
  local tag="${HERMES_AGENT_IMAGE_TAG:-}"
  if [[ -z "$tag" && -f "${ROOT:?}/.env" ]]; then
    tag="$(grep -E '^HERMES_AGENT_IMAGE_TAG=' "${ROOT}/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  fi
  printf 'nousresearch/hermes-agent:%s\n' "${tag:-$(compose_image_default HERMES_AGENT_IMAGE_TAG)}"
}

# A network the running compose SERVICE is on, so a one-shot container can
# reach it by service name.
stack_network_of() {
  local cid net
  cid="$(docker compose ps -q "$1" 2>/dev/null | head -1)"
  [[ -n "$cid" ]] || { printf 'error: %s is not running\n' "$1" >&2; return 1; }
  net="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$cid" | sed '/^$/d' | head -1)"
  [[ -n "$net" ]] || { printf 'error: %s is on no network\n' "$1" >&2; return 1; }
  printf '%s\n' "$net"
}

# stack_container [--network-of SERVICE] [--mount PATH]... [--env NAME]... IMAGE CMD [ARGS...]
#
# Runs CMD in a throwaway container as the host user, so the host needs only
# bash and the container engine. The repo is mounted at its own host path and
# the current directory is kept, so absolute paths under the repo work as
# they are. --mount adds another host path the same way (a data dir outside
# the repo, say). --env NAME passes that variable's value through without
# putting it on the command line. stdin is attached, for scripts read from it.
stack_container() {
  local root="${ROOT:?}" net="" mounts=() envs=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --network-of) net="$(stack_network_of "$2")" || return 1; shift 2 ;;
      --mount) mkdir -p "$2"; mounts+=(-v "$2:$2"); shift 2 ;;
      --env) envs+=(-e "$2"); shift 2 ;;
      *) break ;;
    esac
  done
  local image="$1" cmd="$2"
  shift 2
  local netargs=()
  [[ -n "$net" ]] && netargs=(--network "$net")
  # bash 3.2 treats an empty "${a[@]}" as unbound under set -u.
  docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp \
    ${netargs[@]+"${netargs[@]}"} ${envs[@]+"${envs[@]}"} \
    -v "$root:$root" ${mounts[@]+"${mounts[@]}"} -w "$(pwd)" \
    --entrypoint "$cmd" "$image" "$@"
}

# stack_python [stack_container options] ARGS...
#
# python3 from the pinned Hermes image, which every install already pulls.
# Pass `-` to read the script from stdin, or a script path under the repo.
# STACK_PYTHON=host runs the host's python3 instead (tests, development).
stack_python() {
  if [[ "${STACK_PYTHON:-}" == host ]]; then
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --network-of|--mount|--env) shift 2 ;;
        *) break ;;
      esac
    done
    require_python3
    python3 "$@"
    return
  fi
  local opts=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --network-of|--mount|--env) opts+=("$1" "$2"); shift 2 ;;
      *) break ;;
    esac
  done
  stack_container ${opts[@]+"${opts[@]}"} "$(stack_hermes_image)" python3 "$@"
}
