#!/bin/sh
# Weak-model tool-call recovery and the turn/gateway overlays below.
# See patch-text-tool-call-recovery.py.
# Continue across skips so later overlays still apply.
set -u
run_patch() {
  script="$1"
  if [ -f "$script" ]; then
    python3 "$script" || echo "warn: $script exited $?"
  fi
}
run_patch /bootstrap/patch-text-tool-call-recovery.py
# Stop turns that claim a write (or any <result>) without calling a tool, or
# ask the user for a password in chat.
run_patch /bootstrap/patch-fabricated-result.py
# Rewrite hallucinated native web_search/web_extract → MCP SearXNG.
run_patch /bootstrap/patch-block-native-web-tools.py
# Normalize tool_call shapes: flat siblings, nested name, short MCP aliases.
run_patch /bootstrap/patch-tool-call-flat-args.py
# JSONL turn traces for tool-calling evals (after recovery + web rewrite).
run_patch /bootstrap/patch-tool-eval-trace.py
# EACCES on the agent-browser binary is not a missing Chromium install; result
# hints for a bad helper import or code that printed nothing; print a trailing
# expression; vault save next step names browser_vault_fill.
run_patch /bootstrap/patch-browser-exec-hint.py
# Per-session history budget for DM threads (request window; transcript stays).
run_patch /bootstrap/patch-history-budget.py
# Client-supplied session/turn context; refuse a prompt.submit profile that
# differs from the session's (dashboard /api/ws).
run_patch /bootstrap/patch-client-context.py
# Turn heartbeat while a turn runs, and a one-session live status query
# (dashboard /api/ws).
run_patch /bootstrap/patch-gateway-liveness.py
# Gateway control socket on /tmp: data/hermes (virtiofs) cannot hold a
# connectable Unix socket, and v0.22 gateways coordinate through it.
run_patch /bootstrap/patch-control-socket-tmp.py
# profiles.create mirror_exclude: keys a mirrored .env leaves out, so an agent
# without calendar access never holds the password (docs/calendar.md).
run_patch /bootstrap/patch-mirror-exclude.py
# MCP trust-gate approvals carry the call's redacted arguments and tool call id,
# and offer only once/deny (docs/extending.md).
run_patch /bootstrap/patch-mcp-trust-details.py
