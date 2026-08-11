#!/bin/sh
set -e

# First n8n invocation runs DB migrations and prints to stdout; discard that noise.
n8n list:workflow --onlyId >/dev/null 2>&1 || true

if [ -z "$(n8n list:workflow --onlyId 2>/dev/null)" ]; then
  n8n import:credentials --separate --input=/demo-data/credentials
  n8n import:workflow --separate --input=/demo-data/workflows
else
  echo "Workflows exist, skipping import"
fi
