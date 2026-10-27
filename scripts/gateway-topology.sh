#!/usr/bin/env bash
# Switch the Hermes gateway topology: standalone (one gateway per profile) or
# multiplex (one gateway serving every profile at :8642/p/<profile>/).
#
# 1. Clones data/hermes to data/hermes.bak-<time> (APFS clone, cheap).
# 2. Stops hermes and hermes-webui.
# 3. Sets gateway.standalone on every named profile: false for multiplex, true
#    for standalone. 019-pin-profile-gateways.sh only pins where the key is
#    unset, so it cannot undo an earlier pin by itself.
# 4. Writes HERMES_GATEWAY_TOPOLOGY to .env and brings hermes and hermes-webui
#    back up; the profile bootstraps re-run first.
#
# Rollback: run it again with the other topology, or restore the backup.
#
# Usage: ./scripts/gateway-topology.sh standalone|multiplex [--no-backup] [--no-restart]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/scripts/lib/env.sh"

usage() { echo "Usage: $0 standalone|multiplex [--no-backup] [--no-restart]" >&2; exit 2; }

target="${1:-}"
case "$target" in standalone | multiplex) ;; *) usage ;; esac
shift
backup=1
restart=1
for arg in "$@"; do
  case "$arg" in
    --no-backup) backup=0 ;;
    --no-restart) restart=0 ;;
    *) usage ;;
  esac
done

home="data/hermes"
[ -f "$home/config.yaml" ] || { echo "gateway-topology: no config.yaml in $home" >&2; exit 1; }
image="nousresearch/hermes-agent:$(env_get HERMES_AGENT_IMAGE_TAG v0.22.5)"
current="$(gateway_topology)"
echo "gateway-topology: ${current} -> ${target}"

if [ "$target" = standalone ]; then
  ./scripts/check-hermes-standalone.sh "${image##*:}" || exit 1
fi

if [ "$backup" = 1 ]; then
  dest="data/hermes.bak-$(date +%Y%m%d-%H%M%S)"
  if ! cp -c -R "$home" "$dest" 2>/dev/null; then
    rm -rf "$dest"
    echo "gateway-topology: could not clone $home (needs APFS). Back it up yourself, then re-run with --no-backup." >&2
    exit 1
  fi
  echo "gateway-topology: backup at $dest"
fi

if [ "$restart" = 1 ]; then
  docker compose stop hermes-webui hermes
fi

standalone=false
[ "$target" = standalone ] && standalone=true
docker run --rm --init --network none --user "$(id -u):$(id -g)" \
  -v "$ROOT/$home:/opt/data" -e HERMES_HOME=/opt/data -e STANDALONE="$standalone" \
  --entrypoint /opt/hermes/.venv/bin/python "$image" - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/hermes")
from hermes_cli.config import read_user_config_raw
from utils import atomic_roundtrip_yaml_update

want = os.environ["STANDALONE"] == "true"
root = Path("/opt/data/profiles")
for path in sorted(root.iterdir()) if root.is_dir() else []:
    cfg_path = path / "config.yaml"
    if path.name == "default" or not (path / "SOUL.md").exists() or not cfg_path.exists():
        continue
    gw = read_user_config_raw(cfg_path).get("gateway")
    if isinstance(gw, dict) and gw.get("standalone") is want:
        continue
    atomic_roundtrip_yaml_update(cfg_path, "gateway.standalone", want)
    print(f"gateway-topology: {path.name}: gateway.standalone: {str(want).lower()}")
PY

upsert_env .env HERMES_GATEWAY_TOPOLOGY "$target"
echo "gateway-topology: HERMES_GATEWAY_TOPOLOGY=${target} in .env"

if [ "$restart" = 1 ]; then
  docker compose up -d hermes hermes-webui
  echo "gateway-topology: done. Check with: make doctor"
else
  echo "gateway-topology: recreate to apply: docker compose up -d hermes hermes-webui"
fi
