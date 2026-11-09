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

# The credentials are generated here, from this container's environment, so
# enabling n8n needs no host-side step. Nothing under /demo-data/credentials
# on the host is used. The generator exits 1 and names any key that is empty.
CRED_DIR=/tmp/n8n-credentials
trap 'rm -rf "$CRED_DIR"' EXIT
N8N_CREDENTIALS_OUT="$CRED_DIR" node /scripts/generate-n8n-credentials.js
n8n import:credentials --separate --input="$CRED_DIR"
n8n import:workflow --separate --input=/demo-data/workflows
