#!/usr/bin/env bash
# v0.3.19: OpenCode runs as HERMES_UID, opencode-mcp's GET /tasks, new Hermes
# overlays, the local web vault over HTTPS. CHANGELOG.md "v0.3.19" has the
# detail.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck disable=SC2034  # data-root.sh resolves relative paths against it
ROOT="$UPGRADE_ROOT"
# shellcheck source=../scripts/lib/data-root.sh
source "${UPGRADE_ROOT}/scripts/lib/data-root.sh"
cd "$UPGRADE_ROOT"

# New overlays (cron gated sessions, kanban hold, history task ids,
# session.live_turns), and the bootstraps now read LLM_MODEL.
reload_hermes

# OpenCode's image has a new entrypoint that drops to HERMES_UID, and its state
# volumes move to /home/opencode; the entrypoint hands them over on the first
# start. opencode-mcp gains GET /tasks, which coding-root.sh --apply reads.
if enabled OPENCODE_MCP_ENABLED; then
  rebuild opencode
  rebuild opencode-mcp
  if [[ "$(uname -s)" == Linux ]]; then
    notice "OpenCode now runs as HERMES_UID instead of root. Files it already wrote in a coding root stay root-owned; for each coding root, sudo chown -R \"\$(id -u):\$(id -g)\" DIR fixes them."
  fi
fi

# ca_names CERT: the CA's permitted DNS names, sorted, space-separated.
ca_names() {
  openssl x509 -in "$1" -noout -text 2> /dev/null \
    | awk '/Name Constraints/ { f = 1; next }
           f && /X509v3|Excluded|Signature Algorithm/ { f = 0 }
           f && /DNS:/ { sub(/.*DNS:/, ""); print }' \
    | sort | tr '\n' ' ' | sed 's/ $//'
}

# The local web vault moved from http://localhost:<port> to
# https://vaultwarden.localhost:<port>. Its certificate comes from a CA that
# bitwarden-setup tls makes once, at make vault-init; a CA made before this
# release doesn't cover vaultwarden.localhost, and only the user can replace
# the CA their browser trusts.
if enabled BITWARDEN_LOCAL_ENABLED; then
  port="$(env_get VAULTWARDEN_PORT 8222)"
  url="https://vaultwarden.localhost:${port}"
  tls_dir="$(expand_user_path "$(env_get BITWARDEN_HOST_DATA_DIR ./data/addons/bitwarden)")/local/tls"
  rotate="run docker compose --profile bitwarden-tools run --rm bitwarden-setup tls --rotate, then docker compose restart vaultwarden-tls bitwarden-broker, then replace the CA your browser trusts with ${tls_dir}/ca.crt (docs/bitwarden.md has the macOS command) and open ${url}"
  if [[ -f "${tls_dir}/ca.crt" ]]; then
    if ! command -v openssl > /dev/null 2>&1; then
      needs_user bitwarden-local-ca \
        "The local web vault is now ${url}. Without openssl the upgrade can't tell whether ${tls_dir}/ca.crt covers vaultwarden.localhost. If that address shows a certificate error, ${rotate}."
    elif [[ "$(ca_names "${tls_dir}/ca.crt")" != "vaultwarden.localhost vaultwarden.vault.internal" ]]; then
      needs_user bitwarden-local-ca \
        "The local web vault is now ${url}, and the CA in ${tls_dir} doesn't cover that name. To use it, ${rotate}."
    fi
  fi
  notice "The local web vault moved from http://localhost:${port} to ${url}. Vaultwarden no longer publishes a port of its own."
fi
