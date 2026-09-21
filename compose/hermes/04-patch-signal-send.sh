#!/bin/sh
# WebUI Signal: synthesize credentials when adapter disabled, restore
# agent-callable send_message, make tool_search see visible core tools,
# and fix cron delivery from WebUI (api_server origin + disabled adapter).
# Individual patch scripts may skip on unknown Hermes shapes — continue so
# later patches still run.
set -u
run_patch() {
  script="$1"
  if [ -f "$script" ]; then
    python3 "$script" || echo "warn: $script exited $?"
  fi
}
run_patch /bootstrap/patch-send-message-tool.py
run_patch /bootstrap/patch-ensure-send-message-tools.py
run_patch /bootstrap/patch-tool-search-visible.py
run_patch /bootstrap/patch-cron-scheduler-delivery.py
run_patch /bootstrap/patch-cron-default-deliver-signal.py

# Drop stale discovery verdicts so the next gateway start re-scans AST.
# A cached registers=False for send_message_tool.py skips the import entirely.
rm -f /opt/data/cache/tool_discovery_cache.json \
  /opt/data/profiles/*/cache/tool_discovery_cache.json 2>/dev/null || true
