#!/bin/sh
# bitwarden-setup replaces this entrypoint in compose.
exec python3 -m broker.server "$@"
