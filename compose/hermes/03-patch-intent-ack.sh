#!/bin/sh
# Relax intent-ack continuation for browser/WebUI (see patch-intent-ack-continuation.py).
set -eu
if [ -f /bootstrap/patch-intent-ack-continuation.py ]; then
  python3 /bootstrap/patch-intent-ack-continuation.py
fi
