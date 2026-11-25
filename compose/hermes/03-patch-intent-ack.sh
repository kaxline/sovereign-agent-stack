#!/bin/sh
# Relax intent-ack continuation for browser/WebUI (see patch-intent-ack-continuation.py).
# The overlays and their order: group 03 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 03 || echo "warn: run-overlays.sh 03 exited $?"
