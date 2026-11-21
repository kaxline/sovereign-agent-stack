#!/usr/bin/env bash
# Tests for scripts/check-n8n-keys.sh against scratch .env files, and for
# scripts/generate-n8n-credentials.js with the host's node (skipped without
# one; n8n-import runs it in the n8n image).
#   scripts/test-check-n8n-keys.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "${tmp}/scripts/lib" "${tmp}/bin"
cp "${ROOT}/scripts/check-n8n-keys.sh" "${tmp}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${tmp}/scripts/lib/"
# docker and node stubs log any call: the check must run neither.
for stub in docker node; do
  # shellcheck disable=SC2016 # ${STUB_LOG} expands when the stub runs
  printf '#!/usr/bin/env bash\necho "%s $*" >> "${STUB_LOG}"\n' "$stub" > "${tmp}/bin/${stub}"
  chmod +x "${tmp}/bin/${stub}"
done
for tool in bash env grep head cut dirname tr sed; do
  ln -s "$(command -v "$tool")" "${tmp}/bin/${tool}"
done
export STUB_LOG="${tmp}/stub.log"

fail=0
run() {
  : > "$STUB_LOG"
  if PATH="${tmp}/bin" "${tmp}/scripts/check-n8n-keys.sh" > "${tmp}/out" 2>&1; then
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
# Called through check, which shellcheck can't see (SC2317 before 0.10, SC2329 after).
# shellcheck disable=SC2317,SC2329
ran_nothing() { [[ ! -s "$STUB_LOG" ]]; }
# shellcheck disable=SC2317,SC2329
wrote_nothing() { [[ -z "$(find "$tmp" -name '*.json')" ]]; }

# Automation off: no keys needed.
printf 'COMPOSE_PROFILES=core,rag\n' > "${tmp}/.env"
run
check "automation off exits 0" test "$status" -eq 0

# Automation on with keys missing: stops and names each one.
printf 'COMPOSE_PROFILES=core,automation\nN8N_USER_MANAGEMENT_JWT_SECRET=def456\n' > "${tmp}/.env"
run
check "missing keys fail" test "$status" -ne 0
check "missing N8N_ENCRYPTION_KEY is named" grep -q N8N_ENCRYPTION_KEY "${tmp}/out"
check "missing LIGHTRAG_API_KEY is named" grep -q LIGHTRAG_API_KEY "${tmp}/out"
check "a set key is not named" bash -c "! grep -q N8N_USER_MANAGEMENT_JWT_SECRET '${tmp}/out'"

# Automation on, keys set: passes, and runs no container or node.
printf 'COMPOSE_PROFILES=core,automation\nN8N_ENCRYPTION_KEY=abc123\nN8N_USER_MANAGEMENT_JWT_SECRET=def456\nLIGHTRAG_API_KEY=lr_x\n' > "${tmp}/.env"
run
check "keys set exits 0" test "$status" -eq 0
check "runs no docker or node" ran_nothing
check "writes no credential files" wrote_nothing

# No .env: a clear error.
rm "${tmp}/.env"
run
check "no .env fails" test "$status" -ne 0
check "no .env says so" grep -q '.env missing' "${tmp}/out"

# The generator, with the real node. It writes only into N8N_CREDENTIALS_OUT
# and has no default output directory.
if real_node="$(command -v node)"; then
  gen="${ROOT}/scripts/generate-n8n-credentials.js"
  genout="${tmp}/gen-out"
  gen_run() {
    if (cd "$tmp" && env -i PATH="$(dirname "$real_node")" "$@" "$real_node" "$gen") > "${tmp}/out" 2>&1; then
      status=0
    else
      status=$?
    fi
  }
  # shellcheck disable=SC2317,SC2329
  two_in_out() { [[ "$(find "$genout" -name '*.json' | wc -l | tr -d ' ')" -eq 2 ]]; }
  # shellcheck disable=SC2317,SC2329
  envelope() { grep -q '"data": "U2FsdGVkX1' "${genout}/LrH7tR4gApiKey01.json"; }
  gen_run N8N_CREDENTIALS_OUT="$genout" N8N_ENCRYPTION_KEY=abc123 LIGHTRAG_API_KEY=lr_x
  check "generator writes to N8N_CREDENTIALS_OUT" test "$status" -eq 0
  check "generator wrote both files there" two_in_out
  check "the data is an OpenSSL Salted__ envelope" envelope
  check "the key is not written in clear" bash -c "! grep -rq lr_x '${genout}'"
  rm -rf "$genout"
  gen_run N8N_ENCRYPTION_KEY=abc123 LIGHTRAG_API_KEY=lr_x
  check "without N8N_CREDENTIALS_OUT exits 1" test "$status" -eq 1
  check "without N8N_CREDENTIALS_OUT says so" grep -q N8N_CREDENTIALS_OUT "${tmp}/out"
  check "without N8N_CREDENTIALS_OUT writes nothing" wrote_nothing
  gen_run N8N_CREDENTIALS_OUT="$genout" N8N_ENCRYPTION_KEY= LIGHTRAG_API_KEY=lr_x
  check "empty N8N_ENCRYPTION_KEY exits 1" test "$status" -eq 1
  check "empty N8N_ENCRYPTION_KEY is named" grep -q N8N_ENCRYPTION_KEY "${tmp}/out"
  check "N8N_ENCRYPTION_KEY error does not name the other key" bash -c "! grep -q LIGHTRAG_API_KEY '${tmp}/out'"
  gen_run N8N_CREDENTIALS_OUT="$genout" N8N_ENCRYPTION_KEY=abc123 LIGHTRAG_API_KEY=
  check "empty LIGHTRAG_API_KEY exits 1" test "$status" -eq 1
  check "empty LIGHTRAG_API_KEY is named" grep -q LIGHTRAG_API_KEY "${tmp}/out"
else
  echo "skip: generator checks (no node)"
fi

exit "$fail"
