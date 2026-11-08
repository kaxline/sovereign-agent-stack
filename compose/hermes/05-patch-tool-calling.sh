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
# Result hints for a bad helper import or code that printed nothing; print a
# trailing expression; vault save next step names the browser_vault fill action.
run_patch /bootstrap/patch-browser-exec-hint.py
# browser_vault action=create_login: a sign-up login with a generated password
# the model never sees, approved through the vault.create_login request.
run_patch /bootstrap/patch-vault-create-login.py
# browser_vault action=capture_secret: store a key the page shows (profile .env,
# or handed to the client through vault.take) without the model seeing it.
run_patch /bootstrap/patch-vault-capture-secret.py
# Tool-loop warnings outside the untrusted-data wrapper.
run_patch /bootstrap/patch-tool-loop-notes.py
# Halt a turn that keeps re-running calls whose results it has already seen,
# whatever the cycle length (upstream catches cycles of at most 4 calls).
run_patch /bootstrap/patch-tool-loop-replay.py
# While the searxng MCP server is paused (circuit breaker), say how to search
# with browser_exec instead.
run_patch /bootstrap/patch-searxng-pause-browser.py
# When searxng's web_url_read is refused (403/429/503, empty JS page, bot
# check), a note outside the untrusted wrapper says to open it in the browser.
run_patch /bootstrap/patch-blocked-fetch-hint.py
# HERMES_WEB_SEARCH_MODE: serve searxng search and page reads from SearXNG,
# Hermes' keyless ring, or one with the other as fallback.
run_patch /bootstrap/patch-web-search-mode.py
# A browser URL whose hostname doesn't resolve says so, instead of being
# reported as a private address.
run_patch /bootstrap/patch-browser-unresolvable-host.py
# Per-session history budget for DM threads (request window; transcript stays).
run_patch /bootstrap/patch-history-budget.py
# Compaction summarizer reads full tool results (not prune stubs) and keeps
# facts with their sources in a Findings section.
run_patch /bootstrap/patch-compaction-tool-facts.py
# Client-supplied session/turn context; refuse a prompt.submit profile that
# differs from the session's (dashboard /api/ws).
run_patch /bootstrap/patch-client-context.py
# Turn heartbeat while a turn runs, and a one-session live status query
# (dashboard /api/ws).
run_patch /bootstrap/patch-gateway-liveness.py
# Speaker names on a prompt.submit turn's assistant rows, labelled for the
# model on later turns (dashboard /api/ws).
run_patch /bootstrap/patch-turn-speaker.py
# Gateway control socket on /tmp: data/hermes (virtiofs) cannot hold a
# connectable Unix socket, and v0.22 gateways coordinate through it.
run_patch /bootstrap/patch-control-socket-tmp.py
# profiles.create mirror_exclude: keys a mirrored .env leaves out, so an agent
# without calendar access never holds the password (docs/calendar.md).
run_patch /bootstrap/patch-mirror-exclude.py
# MCP trust-gate approvals carry the call's redacted arguments and tool call id,
# and offer only once/deny (docs/extending.md).
run_patch /bootstrap/patch-mcp-trust-details.py
