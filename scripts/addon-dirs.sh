#!/usr/bin/env bash
# Creates <data root>/addons/<id>/{config,instances,secrets} at mode 0700 for
# every add-on manifest in addons/, enabled or not, as addon_store.prepare
# does. Core services bind-mount some of these (the Hermes bootstraps read
# Twenty's and Actual's), and a missing bind source stops them: Docker creates
# it owned by root on Linux, and Podman refuses it. setup.sh and make
# ensure-local run this; a caller that skips both runs it before compose.
# Existing files are left alone. A directory it can't create or chmod is
# warned about, not fatal.
#
#   ./scripts/addon-dirs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

ADDONS="${ADDONS_DIR:-${ROOT}/addons}"
# ASSISTANT_DATA_ROOT from the environment wins over .env, as it does for
# compose.
root="$(resolve_data_root_path "${ASSISTANT_DATA_ROOT:-}")"
if ! err="$(validate_data_root_path "$root" 2>&1)"; then
  printf '[addon-dirs] warning: %s; no add-on directories created\n' "$err" >&2
  exit 0
fi

for manifest in "$ADDONS"/*/addon.json; do
  [[ -f "$manifest" ]] || continue
  # The short id is the manifest id's last dot-separated part, as
  # addon_manifest.short_id derives it.
  id="$(sed -n 's/^  "id": *"\([^"]*\)".*/\1/p' "$manifest" | head -1)"
  sid="${id##*.}"
  if ! [[ "$sid" =~ ^[a-z][a-z0-9-]*$ ]]; then
    printf '[addon-dirs] warning: %s has no usable id; skipped\n' "$manifest" >&2
    continue
  fi
  d="${root}/addons/${sid}"
  if ! mkdir -p "${d}/config" "${d}/instances" "${d}/secrets" 2> /dev/null \
      || ! chmod 700 "$d" "${d}/config" "${d}/instances" "${d}/secrets" 2> /dev/null; then
    fix="sudo chown -R \"\$(id -u):\$(id -g)\" '${d}'"
    printf '[addon-dirs] warning: could not create %s and its config, instances and secrets at 0700 (another user, often root, owns one); run %s\n' \
      "$d" "$fix" >&2
  fi
done
