#!/usr/bin/env bash
# scripts/project-index-refresh.sh, the start-time refresh scripts/up.sh runs:
# it rewrites stale generated indexes under the data root's projects folder,
# leaves hand-written ones alone, and never fails a start when python3 can't
# run.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/testkit.sh
. "${ROOT}/scripts/lib/testkit.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# A stack copy with only what the script needs, and its data root in tmp.
stack="${tmp}/stack"
mkdir -p "${stack}/scripts/lib"
cp "${ROOT}/scripts/project-index-refresh.sh" "${ROOT}/scripts/project-index.py" "${stack}/scripts/"
for f in env.sh data-root.sh python.sh projects.sh project_sources.py; do
  cp "${ROOT}/scripts/lib/${f}" "${stack}/scripts/lib/"
done
cp "${ROOT}/docker-compose.yml" "${stack}/"
printf 'ASSISTANT_DATA_ROOT=%s\n' "${tmp}/data" > "${stack}/.env"

run() { (cd "$stack" && bash scripts/project-index-refresh.sh "$@"); }

# No projects folder yet: nothing to do, still valid JSON.
out="$(STACK_PYTHON=host run --json)"
[ "$out" = '{"failed": [], "fresh": [], "hand_written": [], "refreshed": []}' ] \
  || fail "no projects folder: $out"

projects="${tmp}/data/projects"
mkdir -p "${projects}/gen/notes" "${projects}/hand"
printf '# A\n' > "${projects}/gen/notes/a.md"
STACK_PYTHON=host python3 "${stack}/scripts/project-index.py" --root "${projects}/gen" > /dev/null
printf '# B\n' > "${projects}/gen/notes/b.md"
printf '# Mine\n' > "${projects}/hand/INDEX.md"

out="$(STACK_PYTHON=host run --json)"
[ "$out" = '{"failed": [], "fresh": [], "hand_written": ["hand"], "refreshed": ["gen"]}' ] \
  || fail "refresh: $out"
grep -q 'notes/b.md' "${projects}/gen/INDEX.md" || fail "refresh missed the new file"
[ "$(cat "${projects}/hand/INDEX.md")" = '# Mine' ] || fail "refresh touched the hand-written index"

out="$(STACK_PYTHON=host run)"
case "$out" in
  *'0 refreshed, 1 fresh, 1 hand-written, 0 failed'*) ;;
  *) fail "second run: $out" ;;
esac

# The image route with a docker that fails: a warning, exit 0, JSON with error.
mkdir "${tmp}/bin"
printf '#!/bin/sh\nexit 1\n' > "${tmp}/bin/docker"
chmod +x "${tmp}/bin/docker"
set +e
out="$(PATH="${tmp}/bin:${PATH}" STACK_PYTHON=image run --json 2> "${tmp}/err")"
rc=$?
set -e
[ "$rc" = 0 ] || fail "failed refresh exit $rc"
grep -q 'warning: project index refresh did not run' "${tmp}/err" || fail "no warning: $(cat "${tmp}/err")"
case "$out" in
  *'"error": "refresh did not run"'*) ;;
  *) fail "failed refresh JSON: $out" ;;
esac

grep -q '^\./scripts/project-index-refresh\.sh$' "${ROOT}/scripts/up.sh" || fail "up.sh no longer runs the refresh"

echo "ok project-index-refresh.sh"
