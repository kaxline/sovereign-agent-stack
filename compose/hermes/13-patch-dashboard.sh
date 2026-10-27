#!/bin/sh
# Kanban task links and ?embed=1 (patch-dashboard-board.py); one-time sign-in
# links at /auth/handoff (patch-dashboard-handoff.py).
set -u
if [ -f /bootstrap/patch-dashboard-board.py ]; then
  python3 /bootstrap/patch-dashboard-board.py || echo "warn: patch-dashboard-board exited $?"
fi
if [ -f /bootstrap/patch-dashboard-handoff.py ]; then
  python3 /bootstrap/patch-dashboard-handoff.py || echo "warn: patch-dashboard-handoff exited $?"
fi
