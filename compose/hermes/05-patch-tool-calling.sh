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
