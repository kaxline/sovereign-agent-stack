#!/bin/sh
# Wake parked MCP servers on request; per-server parked probe interval (see patch-mcp-wake.py).
set -u
if [ -f /bootstrap/patch-mcp-wake.py ]; then
  python3 /bootstrap/patch-mcp-wake.py || echo "warn: patch-mcp-wake exited $?"
fi
