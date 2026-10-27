#!/usr/bin/env bash
# Dry-run Hermes' multiplex migration preflight against a copy of the Hermes
# home with gateway.standalone removed, so pinned profiles are checked too.
#
# The live home is never changed: the copy is a clone (cp -c) in a temp
# directory, the image runs `hermes gateway migrate --multiplex --dry-run`
# against it with no network, and the copy is deleted on exit. Prints only
# Hermes' plan, blockers and notices.
#
# Usage: ./scripts/gateway-preflight.sh [hermes-home]   (default: data/hermes)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/scripts/lib/env.sh"

src="${1:-$(env_get ASSISTANT_DATA_ROOT ./data)/hermes}"
[ -f "$src/config.yaml" ] || { echo "gateway-preflight: no config.yaml in $src" >&2; exit 1; }
image="nousresearch/hermes-agent:$(env_get HERMES_AGENT_IMAGE_TAG v0.22.5)"

work="$(mktemp -d "${TMPDIR:-/tmp}/hermes-preflight.XXXXXX")"
trap 'rm -rf "$work"' EXIT
cp -c -R "$src" "$work/home" 2>/dev/null || cp -R "$src" "$work/home"

docker run --rm --init --network none --user "$(id -u):$(id -g)" \
  -v "$work/home:/opt/data" -e HERMES_HOME=/opt/data \
  --entrypoint sh "$image" -c '
set -e
/opt/hermes/.venv/bin/python - <<"PY"
import sys
from pathlib import Path
sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import read_user_config_raw
from utils import atomic_roundtrip_yaml_update

root = Path("/opt/data/profiles")
for cfg_path in sorted(root.glob("*/config.yaml")) if root.is_dir() else []:
    gw = read_user_config_raw(cfg_path).get("gateway")
    if isinstance(gw, dict) and gw.get("standalone") is not None:
        atomic_roundtrip_yaml_update(cfg_path, "gateway.standalone", False)
        print(f"preflight copy: {cfg_path.parent.name}: gateway.standalone -> false")
PY
exec /opt/hermes/.venv/bin/hermes gateway migrate --multiplex --dry-run
'
