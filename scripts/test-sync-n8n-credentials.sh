#!/usr/bin/env bash
# Tests for scripts/sync-n8n-credentials.sh against scratch .env files, with a
# stub docker on PATH that records each call (the generator runs in the n8n
# image), and a stub node for N8N_NODE=host.
#   scripts/test-sync-n8n-credentials.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "${tmp}/scripts/lib" "${tmp}/n8n/demo-data/credentials" "${tmp}/bin"
cp "${ROOT}/scripts/sync-n8n-credentials.sh" "${tmp}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${ROOT}/scripts/lib/python.sh" "${tmp}/scripts/lib/"
# A one-line compose file: the literal ${...} is what compose_image_default reads.
# shellcheck disable=SC2016
printf '  image: ${N8N_IMAGE:-n8nio/n8n:9.9.9}\n' > "${tmp}/docker-compose.yml"

# The stubs stand in for generate-n8n-credentials.js: each logs the key it was
# given and writes one credential file. docker also logs its arguments.
cat > "${tmp}/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "${N8N_ENCRYPTION_KEY}" >> "${STUB_LOG}"
echo "$*" > "${STUB_LOG}.args"
echo '{}' > n8n/demo-data/credentials/stub.json
EOF
cat > "${tmp}/bin/node" <<'EOF'
#!/usr/bin/env bash
echo "${N8N_ENCRYPTION_KEY}" >> "${STUB_LOG}"
echo '{}' > n8n/demo-data/credentials/stub.json
EOF
chmod +x "${tmp}/bin/docker" "${tmp}/bin/node"
mv "${tmp}/bin/node" "${tmp}/node"
# PATH holds only these tools and the stub, so a real node on the runner
# never stands in for it.
for tool in bash env grep head cut dirname mktemp mv cat touch tr id sed mkdir; do
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
# Called through check, which shellcheck can't see (SC2317 before 0.10, SC2329 after).
# shellcheck disable=SC2317,SC2329
creds() { compgen -G "${tmp}/n8n/demo-data/credentials/*.json" >/dev/null; }
# shellcheck disable=SC2317,SC2329
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
printf "COMPOSE_PROFILES=core,automation\n%b" "$keys" > "${tmp}/.env"
run
check "first run exits 0" test "$status" -eq 0
check "first run writes credentials" creds
check "first run passes the .env key" grep -qx abc123 "$STUB_LOG"

# Files exist: a plain run leaves them, --force rewrites them.
run
check "existing files are kept" test "$(calls)" -eq 0
run --force
check "--force rewrites" test "$(calls)" -eq 1

check "runs node in the compose default n8n image" grep -q -- '--entrypoint node n8nio/n8n:9.9.9 scripts/generate-n8n-credentials.js' "${STUB_LOG}.args"
check "passes the keys by name, not value" grep -q -- '-e N8N_ENCRYPTION_KEY ' "${STUB_LOG}.args"
check "keeps the key off the command line" bash -c "! grep -q abc123 '${STUB_LOG}.args'"
printf 'N8N_IMAGE=n8nio/n8n:1.0.0\n' >> "${tmp}/.env"
run --force
check "N8N_IMAGE picks the image" grep -q -- '--entrypoint node n8nio/n8n:1.0.0 ' "${STUB_LOG}.args"

# N8N_NODE=host: the host's node, and a clear error without one.
rm -f "${tmp}/n8n/demo-data/credentials/"*.json
N8N_NODE=host run
check "N8N_NODE=host without node fails" test "$status" -ne 0
check "N8N_NODE=host without node says so" grep -q 'needs Node' "${tmp}/out"
mv "${tmp}/node" "${tmp}/bin/node"
N8N_NODE=host run
check "N8N_NODE=host runs the host node" test "$(calls)" -eq 1

exit "$fail"
