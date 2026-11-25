#!/bin/sh
# Treat /opt/projects children as discoverable Projects (see patch-projects-library.py).
# The overlays and their order: group 07 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 07 || echo "warn: run-overlays.sh 07 exited $?"
