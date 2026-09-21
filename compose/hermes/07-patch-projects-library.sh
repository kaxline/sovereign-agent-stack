#!/bin/sh
# Treat /opt/projects children as discoverable Projects (see patch-projects-library.py).
set -u
if [ -f /bootstrap/patch-projects-library.py ]; then
  python3 /bootstrap/patch-projects-library.py || echo "warn: patch-projects-library exited $?"
fi
