#!/bin/sh
# Refuse kanban workspaces that can't be built, at filing; give up on final
# workspace errors at once (see patch-kanban-guards.py). Change a task's
# workspace in place; kanban_update / kanban_archive (patch-kanban-edit.py).
set -u
if [ -f /bootstrap/patch-kanban-guards.py ]; then
  python3 /bootstrap/patch-kanban-guards.py || echo "warn: patch-kanban-guards exited $?"
fi
if [ -f /bootstrap/patch-kanban-edit.py ]; then
  python3 /bootstrap/patch-kanban-edit.py || echo "warn: patch-kanban-edit exited $?"
fi
