#!/bin/sh
set -e

# n8n stores everything in SQLite under /home/node/.n8n, shared with the n8n
# service. Seed only a store that has never been created: checking the file
# instead of running `n8n list:workflow` means this never opens (or migrates)
# a database that a running n8n still holds, which `docker compose up` can
# otherwise cause when it restarts this one-shot next to a live n8n.
DB=/home/node/.n8n/database.sqlite

if [ -e "$DB" ]; then
  echo "n8n database exists, skipping import"
  exit 0
fi

n8n import:credentials --separate --input=/demo-data/credentials
n8n import:workflow --separate --input=/demo-data/workflows
