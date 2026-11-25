#!/bin/sh
# Weak-model tool-call recovery and the turn, gateway, vault, Bitwarden, cron,
# control-socket, mirror-exclude and MCP trust overlays.
# The overlays and their order: group 05 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 05 || echo "warn: run-overlays.sh 05 exited $?"
