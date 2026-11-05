#!/usr/bin/env bash
# Tests for scripts/sync-n8n-credentials.sh against scratch .env files, with a
# stub node on PATH that records each call.
#   scripts/test-sync-n8n-credentials.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "${tmp}/scripts/lib" "${tmp}/n8n/demo-data/credentials" "${tmp}/bin"
cp "${ROOT}/scripts/sync-n8n-credentials.sh" "${tmp}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${tmp}/scripts/lib/"

# The stub stands in for generate-n8n-credentials.js: it logs the key it was
# given and writes one credential file.
cat > "${tmp}/bin/node" <<'EOF'
#!/usr/bin/env bash
echo "${N8N_ENCRYPTION_KEY}" >> "${STUB_LOG}"
echo '{}' > n8n/demo-data/credentials/stub.json
EOF
chmod +x "${tmp}/bin/node"
# PATH holds only these tools and the stub, so a real node on the runner
# never stands in for it.
for tool in bash env grep head cut dirname mktemp mv cat touch tr; do
  ln -s "$(command -v "$tool")" "${tmp}/bin/${tool}"
done
export STUB_LOG="${tmp}/node.log"

fail=0
run() {
  : > "$STUB_LOG"
  if PATH="${tmp}/bin" "${tmp}/scripts/sync-n8n-credentials.sh" "$@" > "${tmp}/out" 2>&1; then
    status=0
  else
    status=$?
  fi
}
check() {
  local label="$1"
  shift
  if "$@"; then
    echo "ok: ${label}"
  else
    echo "FAIL: ${label}"
    sed 's/^/    /' "${tmp}/out"
    fail=1
  fi
}
calls() { wc -l < "$STUB_LOG" | tr -d ' '; }
creds() { compgen -G "${tmp}/n8n/demo-data/credentials/*.json" >/dev/null; }
no_creds() { ! creds; }

keys='N8N_ENCRYPTION_KEY=abc123\nN8N_USER_MANAGEMENT_JWT_SECRET=def456\nLIGHTRAG_API_KEY=lr_x\n'

# Automation off: no keys needed and node never runs, even with --force.
printf 'COMPOSE_PROFILES=core,rag\n' > "${tmp}/.env"
run --force
check "automation off exits 0" test "$status" -eq 0
check "automation off does not run node" test "$(calls)" -eq 0
check "automation off writes nothing" no_creds

# Automation on with a key missing: stops with an error, node never runs.
printf 'COMPOSE_PROFILES=core,automation\nN8N_USER_MANAGEMENT_JWT_SECRET=def456\nLIGHTRAG_API_KEY=lr_x\n' > "${tmp}/.env"
run
check "missing key fails" test "$status" -ne 0
check "missing key is named" grep -q N8N_ENCRYPTION_KEY "${tmp}/out"
check "missing key does not run node" test "$(calls)" -eq 0

# Automation on, keys set, no files yet: writes them with the .env key.
printf "COMPOSE_PROFILES=core,automation\n${keys}" > "${tmp}/.env"
run
check "first run exits 0" test "$status" -eq 0
check "first run writes credentials" creds
check "first run passes the .env key" grep -qx abc123 "$STUB_LOG"

# Files exist: a plain run leaves them, --force rewrites them.
run
check "existing files are kept" test "$(calls)" -eq 0
run --force
check "--force rewrites" test "$(calls)" -eq 1

# Automation on and Node missing: a clear error, not a command-not-found.
rm -f "${tmp}/n8n/demo-data/credentials/"*.json "${tmp}/bin/node"
run
check "missing node fails" test "$status" -ne 0
check "missing node says so" grep -q 'needs Node' "${tmp}/out"

exit "$fail"
