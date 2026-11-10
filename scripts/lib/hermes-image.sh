# Sourced by shell tests that run the pinned Hermes image. POSIX sh.
#
# Sets HERMES_IMAGE from HERMES_AGENT_IMAGE_TAG, else the default pinned in
# docker-compose.yml, so a Hermes bump changes one line. hermes_image_gate is
# the shell side of hermes_image.image_gate in hermes_image.py.

_hermes_image_root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC2016 # a literal ${...} in the compose file
_hermes_image_default="$(sed -n 's/.*hermes-agent:\${HERMES_AGENT_IMAGE_TAG:-\([^}]*\)}.*/\1/p' \
  "${_hermes_image_root}/docker-compose.yml" | head -1)"
if [ -z "$_hermes_image_default" ]; then
  echo "FAIL docker-compose.yml has no hermes-agent:\${HERMES_AGENT_IMAGE_TAG:-...} pin" >&2
  exit 1
fi
HERMES_IMAGE="nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG:-$_hermes_image_default}"

# hermes_image_gate MODE LABEL: returns when the image checks should run.
# MODE --host-only exits 0. A missing Docker or image exits 0 with a skip line,
# unless MODE is --image or HERMES_IMAGE_REQUIRED=1, which exit 1 instead.
hermes_image_gate() {
  [ "$1" = "--host-only" ] && exit 0
  if command -v docker >/dev/null 2>&1 && docker image inspect "$HERMES_IMAGE" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$1" = "--image" ] || [ "${HERMES_IMAGE_REQUIRED:-}" = 1 ]; then
    echo "FAIL $2: $HERMES_IMAGE not present" >&2
    exit 1
  fi
  echo "skip $2 ($HERMES_IMAGE not present)"
  exit 0
}
