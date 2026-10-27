#!/usr/bin/env bash
# Refuse a Hermes agent tag that dropped gateway.standalone while the stack
# still pins it.
#
# 019-pin-profile-gateways.sh keeps api-server and browser on their own ports
# with gateway.standalone: true, which upstream calls a temporary shim. An image
# without it folds both profiles into the root gateway, which has no listener,
# so 8643 and 8644 stop answering. `make hermes-upgrade` runs this before it
# touches .env, turning that outage into a refused upgrade.
#
# Usage: ./scripts/check-hermes-standalone.sh <agent-tag>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/env.sh
source "$ROOT/scripts/lib/env.sh"

tag="${1:?usage: $0 <agent-tag>}"
image="nousresearch/hermes-agent:${tag}"

topology="$(gateway_topology)"
if [ "$topology" = multiplex ]; then
  echo "check-hermes-standalone: gateway topology is multiplex, skipped"
  exit 0
fi

docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"

if docker run --rm --entrypoint /opt/hermes/.venv/bin/python "$image" -c \
  'import sys; sys.path.insert(0, "/opt/hermes"); from hermes_cli.profiles import profile_is_standalone' \
  >/dev/null 2>&1; then
  echo "check-hermes-standalone: ${tag} still supports gateway.standalone"
  exit 0
fi

cat >&2 <<EOF
check-hermes-standalone: ${tag} no longer has hermes_cli.profiles.profile_is_standalone,
so gateway.standalone is gone. This stack still pins it (HERMES_GATEWAY_TOPOLOGY=standalone),
and on this image ports 8643 and 8644 would stop answering. Move to the multiplexed
gateway first (./scripts/gateway-topology.sh multiplex, docs/hermes.md), or stay
on the current tag.
EOF
exit 1
