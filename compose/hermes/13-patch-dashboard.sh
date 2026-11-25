#!/bin/sh
# Kanban task links and ?embed=1; one-time sign-in links at /auth/handoff.
# The overlays and their order: group 13 in overlays.list.
set -u
sh /bootstrap/run-overlays.sh 13 || echo "warn: run-overlays.sh 13 exited $?"
