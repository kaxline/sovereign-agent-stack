#!/bin/sh
set -e

# Seeds n8n's demo credentials and starter workflows.
#
#   import-demo-data.sh               seed a store that does not exist yet (the
#                                     n8n-import service runs this on every up)
#   import-demo-data.sh credentials   re-import the credentials into an existing
#                                     store, after a key changed
#   import-demo-data.sh all           re-import the credentials and workflows
#                                     into an existing store
#
# The last two open the database, so n8n must be stopped first; make
# n8n-reimport does that.
#
# n8n stores everything in SQLite under /home/node/.n8n, shared with the n8n
# service. The seed checks for the file instead of running `n8n
# list:workflow`, so it never opens (or migrates) a database that a running
# n8n still holds, which `docker compose up` can otherwise cause when it
# restarts this one-shot next to a live n8n.
DB=/home/node/.n8n/database.sqlite
MODE="${1:-seed}"

case "$MODE" in
  seed)
    if [ -e "$DB" ]; then
      echo "n8n database exists, skipping import"
      exit 0
    fi
    ;;
  credentials | all)
    if [ ! -e "$DB" ]; then
      echo "no n8n database yet; make up seeds it" >&2
      exit 1
    fi
    ;;
  *)
    echo "usage: import-demo-data.sh [credentials|all]" >&2
    exit 2
    ;;
esac

# The credentials are generated here, from this container's environment,
# encrypted with N8N_ENCRYPTION_KEY. Nothing on the host is read. The
# generator exits 1 and names any key that is empty.
CRED_DIR=/tmp/n8n-credentials
trap 'rm -rf "$CRED_DIR"' EXIT
N8N_CREDENTIALS_OUT="$CRED_DIR" node /scripts/generate-n8n-credentials.js
n8n import:credentials --separate --input="$CRED_DIR"
if [ "$MODE" != credentials ]; then
  n8n import:workflow --separate --input=/demo-data/workflows
fi
