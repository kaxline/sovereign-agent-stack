#!/bin/sh
# Wake parked MCP servers on request; per-server parked probe interval (see patch-mcp-wake.py).
# The overlays and their order: group 10 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 10 || echo "warn: run-overlays.sh 10 exited $?"
