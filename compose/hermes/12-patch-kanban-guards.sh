#!/bin/sh
# Refuse kanban workspaces that can't be built, at filing; give up on final
# workspace errors at once (see patch-kanban-guards.py).
set -u
if [ -f /bootstrap/patch-kanban-guards.py ]; then
  python3 /bootstrap/patch-kanban-guards.py || echo "warn: patch-kanban-guards exited $?"
fi
