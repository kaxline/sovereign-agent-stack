#!/bin/sh
# Kanban overlays: workspace guards, in-place edits, wake after compression, hold.
# The overlays and their order: group 12 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 12 || echo "warn: run-overlays.sh 12 exited $?"
