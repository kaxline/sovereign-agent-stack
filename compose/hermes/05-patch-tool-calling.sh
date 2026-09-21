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
run_patch /bootstrap/patch-openrouter-empty-stream.py
# Rewrite hallucinated native web_search/web_extract → MCP SearXNG.
run_patch /bootstrap/patch-block-native-web-tools.py
# Normalize tool_call shapes: flat siblings, nested name, short MCP aliases.
run_patch /bootstrap/patch-tool-call-flat-args.py
