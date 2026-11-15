# Bitwarden helpers for stack scripts.
# Source from a script that sets ROOT:  source "${ROOT}/scripts/lib/bitwarden.sh"

# The broker used to share hermes' network namespace, so every recreate of
# hermes needed scripts/vault.sh follow-hermes to put it back. It now has its own
# networks (browser and credentials) and its own lifecycle, so nothing has to
# follow hermes; this file stays for the helpers the stack scripts still source.
