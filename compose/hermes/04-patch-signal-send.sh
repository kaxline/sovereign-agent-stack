#!/bin/sh
# WebUI Signal: synthesize credentials when adapter disabled, restore
# agent-callable send_message, make tool_search see visible core tools,
# and fix cron delivery from WebUI (api_server origin + disabled adapter).
set -eu
if [ -f /bootstrap/patch-signal-send-disabled-adapter.py ]; then
  python3 /bootstrap/patch-signal-send-disabled-adapter.py
fi
if [ -f /bootstrap/patch-reregister-send-message.py ]; then
  python3 /bootstrap/patch-reregister-send-message.py
fi
if [ -f /bootstrap/patch-force-import-send-message.py ]; then
  python3 /bootstrap/patch-force-import-send-message.py
fi
if [ -f /bootstrap/patch-ensure-send-message-tools.py ]; then
  python3 /bootstrap/patch-ensure-send-message-tools.py
fi
if [ -f /bootstrap/patch-tool-search-visible.py ]; then
  python3 /bootstrap/patch-tool-search-visible.py
fi
if [ -f /bootstrap/patch-cron-api-server-origin.py ]; then
  python3 /bootstrap/patch-cron-api-server-origin.py
fi
if [ -f /bootstrap/patch-cron-signal-synthesize.py ]; then
  python3 /bootstrap/patch-cron-signal-synthesize.py
fi
if [ -f /bootstrap/patch-cron-default-deliver-signal.py ]; then
  python3 /bootstrap/patch-cron-default-deliver-signal.py
fi

# Drop stale discovery verdicts so the next gateway start re-scans AST.
# A cached registers=False for send_message_tool.py skips the import entirely.
rm -f /opt/data/cache/tool_discovery_cache.json \
  /opt/data/profiles/*/cache/tool_discovery_cache.json 2>/dev/null || true
