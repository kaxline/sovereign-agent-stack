#!/bin/sh
# WebUI Signal: synthesize credentials when adapter disabled, restore
# agent-callable send_message, make tool_search see visible core tools,
# and let cron deliver to Signal with the adapter disabled. WebUI (api_server)
# jobs reach Signal through upstream's home-channel fallback (v0.22, #69304).
# The overlays and their order: group 04 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 04 || echo "warn: run-overlays.sh 04 exited $?"

# Drop stale discovery verdicts so the next gateway start re-scans AST.
# A cached registers=False for send_message_tool.py skips the import entirely.
rm -f /opt/data/cache/tool_discovery_cache.json \
  /opt/data/profiles/*/cache/tool_discovery_cache.json 2>/dev/null || true
