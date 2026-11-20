#!/usr/bin/env bash
# The lint checks .github/workflows/ci.yml runs, at the versions pinned here.
# CI's shellcheck and hadolint jobs call this script, and `make lint` and
# scripts/test-lint.sh run it locally, so a lint failure shows up before a
# push. Both linters run from container images, so the host needs only Docker.
#
#   scripts/lint.sh [shellcheck|hadolint]...    default: both
#
# To move a linter to a new version, change its image here. Nothing else
# names a version.
set -euo pipefail

SHELLCHECK_IMAGE="koalaman/shellcheck-alpine:v0.11.0"
HADOLINT_IMAGE="ghcr.io/hadolint/hadolint:v2.12.0-debian"
# With -x, shellcheck follows every `source`, and a directive that resolves
# to the wrong file can make it loop until it runs out of memory, with no output.
# On a GitHub runner that ends in a runner shutdown 3-5 minutes in.
SHELLCHECK_TIMEOUT=300

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

run_shellcheck() {
  # A `# shellcheck source=` directive in scripts/lib resolves against the
  # top-level script's source-path. Under source-path=SCRIPTDIR that is
  # scripts/, so a bare name such as env.sh finds scripts/env.sh instead of
  # scripts/lib/env.sh. scripts/env.sh sources lib/data-root.sh, and that
  # loop hung CI. Name lib files as lib/NAME.
  local bare
  bare="$(grep -nE '^[[:space:]]*# shellcheck source=[^/[:space:]]+$' scripts/lib/*.sh || true)"
  if [[ -n "$bare" ]]; then
    printf '%s\n' "$bare" >&2
    echo "error: name scripts/lib files as source=lib/NAME in shellcheck directives" >&2
    return 1
  fi

  docker run --rm -q -v "${ROOT}:/mnt" -w /mnt --entrypoint sh "$SHELLCHECK_IMAGE" -c '
    set -e
    t='"$SHELLCHECK_TIMEOUT"'
    rc=0
    timeout "$t" shellcheck -x -e SC1090,SC1091 scripts/*.sh upgrades/*.sh || rc=$?
    if [ "$rc" = 143 ]; then
      echo "error: shellcheck -x ran past ${t}s; look for a source directive that loops" >&2
      exit 1
    fi
    [ "$rc" = 0 ] || exit "$rc"
    shellcheck -s sh -e SC1090,SC1091 compose/hermes/bootstrap-api-profile.sh
    shellcheck -s sh compose/hermes/007-profile-ownership.sh
    shellcheck -s sh -e SC1090,SC1091 n8n/import-demo-data.sh || true
  '
}

run_hadolint() {
  local files=() f
  while IFS= read -r f; do
    files+=("$f")
  done < <(git ls-files 'compose/*/Dockerfile')
  docker run --rm -q -v "${ROOT}:/mnt" -w /mnt "$HADOLINT_IMAGE" hadolint \
    --failure-threshold warning \
    --ignore DL3008 --ignore DL3018 --ignore DL3009 \
    "${files[@]}"
}

checks=("$@")
[[ ${#checks[@]} -gt 0 ]] || checks=(shellcheck hadolint)
for c in "${checks[@]}"; do
  case "$c" in
    shellcheck) echo "== shellcheck (${SHELLCHECK_IMAGE})"; run_shellcheck ;;
    hadolint) echo "== hadolint (${HADOLINT_IMAGE})"; run_hadolint ;;
    *) echo "error: unknown check: ${c} (shellcheck or hadolint)" >&2; exit 2 ;;
  esac
done
