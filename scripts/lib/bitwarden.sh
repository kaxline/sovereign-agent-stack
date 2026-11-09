# Bitwarden helpers for stack scripts.
# Source from a script that sets ROOT:  source "${ROOT}/scripts/lib/bitwarden.sh"

# Call after every command that recreates or restarts hermes. bitwarden-broker
# runs in hermes' network namespace (network_mode: service:hermes); after a
# recreate it is left in the removed container's namespace, unreachable. This
# recreates it in the current one and prints the manual accounts that need
# make vault-unlock. A no-op while BITWARDEN_ENABLED is off.
follow_hermes() {
  "${ROOT}/scripts/vault.sh" follow-hermes
}
