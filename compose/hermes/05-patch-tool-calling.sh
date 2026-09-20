#!/bin/sh
# Weak-model tool-call recovery + OpenRouter empty-stream cache bust.
# See patch-text-tool-call-recovery.py / patch-openrouter-empty-stream.py.
set -eu
if [ -f /bootstrap/patch-text-tool-call-recovery.py ]; then
  python3 /bootstrap/patch-text-tool-call-recovery.py
fi
if [ -f /bootstrap/patch-openrouter-empty-stream.py ]; then
  python3 /bootstrap/patch-openrouter-empty-stream.py
fi
# Rewrite hallucinated native web_search/web_extract → MCP SearXNG.
if [ -f /bootstrap/patch-block-native-web-tools.py ]; then
  python3 /bootstrap/patch-block-native-web-tools.py
fi
# Normalize tool_call shapes: flat siblings, nested name, short MCP aliases.
if [ -f /bootstrap/patch-tool-call-flat-args.py ]; then
  python3 /bootstrap/patch-tool-call-flat-args.py
fi
