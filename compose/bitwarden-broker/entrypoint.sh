#!/bin/sh
# bitwarden-setup and bitwarden-secrets-sync replace this entrypoint in compose.
exec python3 -m broker.server "$@"
