#!/bin/sh
# Weak-model tool-call recovery + OpenRouter empty-stream cache bust.
# See patch-text-tool-call-recovery.py / patch-openrouter-empty-stream.py.
# Continue across skips so later overlays still apply.
set -u
run_patch() {
  script="$1"
  if [ -f "$script" ]; then
    python3 "$script" || echo "warn: $script exited $?"
  fi
}
run_patch /bootstrap/patch-text-tool-call-recovery.py
# Stop turns that claim a write (or any <result>) without calling a tool.
run_patch /bootstrap/patch-fabricated-result.py
# Re-prompt a reasoning-only stop that holds a malformed tool call.
run_patch /bootstrap/patch-reasoning-only-tool-markup.py
run_patch /bootstrap/patch-openrouter-empty-stream.py
# Rewrite hallucinated native web_search/web_extract → MCP SearXNG.
run_patch /bootstrap/patch-block-native-web-tools.py
# Normalize tool_call shapes: flat siblings, nested name, short MCP aliases.
run_patch /bootstrap/patch-tool-call-flat-args.py
# JSONL turn traces for tool-calling evals (after recovery + web rewrite).
run_patch /bootstrap/patch-tool-eval-trace.py
# Pin agent-browser + --no-install (fallback when 08's PATH install is absent).
run_patch /bootstrap/patch-agent-browser-npx.py
# EACCES on the agent-browser binary is not a missing Chromium install.
run_patch /bootstrap/patch-browser-exec-hint.py
# Per-session history budget for DM threads (request window; transcript stays).
run_patch /bootstrap/patch-history-budget.py
