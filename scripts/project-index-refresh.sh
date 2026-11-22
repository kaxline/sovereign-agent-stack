#!/usr/bin/env bash
# Rewrite every stale generated INDEX.md under ASSISTANT_DATA_ROOT/projects,
# including projects whose sources.yaml names extra roots. Hand-written
# indexes (no .index-manifest.json) are left alone. scripts/up.sh runs this
# before each start; a client that starts the stack another way can run it too.
#
#   scripts/project-index-refresh.sh [--json]
#
# Prints scripts/project-index.py --refresh-all's output (one JSON object with
# --json). Exits 0 even when the refresh fails, with a warning on stderr: a
# stale index never blocks a start.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"
# shellcheck source=lib/python.sh
source "${ROOT}/scripts/lib/python.sh"
# shellcheck source=lib/projects.sh
source "${ROOT}/scripts/lib/projects.sh"

json=()
case "${1:-}" in
  "") ;;
  --json) json=(--json) ;;
  *) printf 'usage: %s [--json]\n' "$0" >&2; exit 2 ;;
esac

projects="$(resolve_data_root_path "")/projects"
if [[ ! -d "$projects" ]]; then
  [[ ${#json[@]} -eq 0 ]] || echo '{"failed": [], "fresh": [], "hand_written": [], "refreshed": []}'
  exit 0
fi
project_source_mounts "$projects"
if ! (fast_python "${PROJECT_SOURCE_MOUNTS[@]}" "${ROOT}/scripts/project-index.py" \
  --refresh-all "$projects" ${json[@]+"${json[@]}"} < /dev/null); then
  echo "warning: project index refresh did not run; generated INDEX.md files may be stale" >&2
  [[ ${#json[@]} -eq 0 ]] \
    || echo '{"error": "refresh did not run", "failed": [], "fresh": [], "hand_written": [], "refreshed": []}'
fi
exit 0
