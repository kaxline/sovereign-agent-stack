#!/usr/bin/env bash
# Tests for scripts/sync-profiles.sh against scratch .env files.
#   scripts/test-sync-profiles.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "${tmp}/scripts/lib"
cp "${ROOT}/scripts/sync-profiles.sh" "${tmp}/scripts/"
cp "${ROOT}/scripts/lib/env.sh" "${tmp}/scripts/lib/"

fail=0
get() { grep -E "^$1=" "${tmp}/.env" | head -1 | cut -d= -f2- || true; }
sync() { "${tmp}/scripts/sync-profiles.sh" > "${tmp}/out" 2>&1; }
expect() {
  local label="$1" key="$2" want="$3" got
  got="$(get "$key")"
  if [[ "$got" == "$want" ]]; then
    echo "ok: ${label}"
  else
    echo "FAIL: ${label}: ${key}=${got}, want ${want}"
    sed 's/^/    /' "${tmp}/out"
    fail=1
  fi
}

# A flag set to 1 adds its profile and records it as managed.
keys='NEO4J_PASSWORD=pw\nLIGHTRAG_API_KEY=lr\n'
printf "COMPOSE_PROFILES=core\nLIGHTRAG_MCP_ENABLED=1\nMAIL_MCP_ENABLED=0\n${keys}" > "${tmp}/.env"
sync
expect "flag on adds profile" COMPOSE_PROFILES core,rag
expect "flag on marks profile managed" COMPOSE_PROFILES_MANAGED rag

# Turning it off removes the profile it manages.
sed -i.bak 's/^LIGHTRAG_MCP_ENABLED=1/LIGHTRAG_MCP_ENABLED=0/' "${tmp}/.env"
sync
expect "flag off removes managed profile" COMPOSE_PROFILES core
expect "flag off drops managed entry" COMPOSE_PROFILES_MANAGED ""

# A profile added by hand with its flag at 0 keeps running, with a warning.
printf 'COMPOSE_PROFILES=core,todo\nVIKUNJA_MCP_ENABLED=0\n' > "${tmp}/.env"
sync
expect "hand-added profile with flag off is kept" COMPOSE_PROFILES core,todo
if grep -q "VIKUNJA_MCP_ENABLED is not 1" "${tmp}/out"; then
  echo "ok: hand-added profile with flag off warns"
else
  echo "FAIL: hand-added profile with flag off warns"
  fail=1
fi

# An existing flag with its profile already on is adopted, so 0 later removes it.
printf 'COMPOSE_PROFILES=core,mail\nMAIL_MCP_ENABLED=1\n' > "${tmp}/.env"
sync
expect "existing pair is adopted" COMPOSE_PROFILES_MANAGED mail
expect "existing pair keeps profile" COMPOSE_PROFILES core,mail

# A missing newer flag is seeded from the profile, so nothing stops or starts.
printf 'COMPOSE_PROFILES=core,automation\n' > "${tmp}/.env"
sync
expect "missing AUTOMATION_ENABLED seeded on" AUTOMATION_ENABLED 1
expect "missing OLLAMA_ENABLED seeded off" OLLAMA_ENABLED 0
expect "seeding leaves profiles as they were" COMPOSE_PROFILES core,automation

# A missing older flag leaves its profile alone.
printf "COMPOSE_PROFILES=core,rag\n${keys}" > "${tmp}/.env"
sync
expect "missing LIGHTRAG_MCP_ENABLED leaves rag" COMPOSE_PROFILES core,rag
expect "missing older flag is not written" LIGHTRAG_MCP_ENABLED ""

# NEO4J_ENABLED adds neo4j on its own and needs only the Neo4j password.
printf 'COMPOSE_PROFILES=core\nNEO4J_ENABLED=1\nNEO4J_PASSWORD=pw\n' > "${tmp}/.env"
sync
expect "neo4j flag adds neo4j alone" COMPOSE_PROFILES core,neo4j

# rag without its keys stops before COMPOSE_PROFILES changes.
printf 'COMPOSE_PROFILES=core\nLIGHTRAG_MCP_ENABLED=1\n' > "${tmp}/.env"
if sync; then
  echo "FAIL: rag without keys fails"
  fail=1
elif grep -q "NEO4J_PASSWORD LIGHTRAG_API_KEY not set" "${tmp}/out"; then
  echo "ok: rag without keys fails and names both"
else
  echo "FAIL: rag without keys names both"
  sed 's/^/    /' "${tmp}/out"
  fail=1
fi
expect "rag without keys leaves profiles" COMPOSE_PROFILES core

# neo4j added by hand without a password fails too.
printf 'COMPOSE_PROFILES=core,neo4j\nLIGHTRAG_API_KEY=lr\n' > "${tmp}/.env"
if sync; then
  echo "FAIL: hand-added neo4j without password fails"
  fail=1
elif grep -q "but NEO4J_PASSWORD not set" "${tmp}/out"; then
  echo "ok: hand-added neo4j without password fails"
else
  echo "FAIL: hand-added neo4j without password names the key"
  sed 's/^/    /' "${tmp}/out"
  fail=1
fi

# Profiles without a flag are untouched, and order is kept.
printf 'COMPOSE_PROFILES=signal,core,n8n-postgres-legacy,custom\nOPENCODE_MCP_ENABLED=yes\n' > "${tmp}/.env"
sync
expect "unflagged profiles kept in order" COMPOSE_PROFILES signal,core,n8n-postgres-legacy,custom,coding

# Legacy hermes-webui name and spaces are cleaned.
printf 'COMPOSE_PROFILES=core, hermes-webui\n' > "${tmp}/.env"
sync
expect "legacy name and spaces cleaned" COMPOSE_PROFILES core

# A second run over a synced .env changes nothing.
printf "COMPOSE_PROFILES=core\nLIGHTRAG_MCP_ENABLED=1\nSEARCH_GATE_ENABLED=1\n${keys}" > "${tmp}/.env"
sync
cp "${tmp}/.env" "${tmp}/.env.first"
sync
if cmp -s "${tmp}/.env" "${tmp}/.env.first" && [[ ! -s "${tmp}/out" ]]; then
  echo "ok: second run is a no-op"
else
  echo "FAIL: second run is a no-op"
  diff "${tmp}/.env.first" "${tmp}/.env" || true
  fail=1
fi

# --list runs and does not write.
"${tmp}/scripts/sync-profiles.sh" --list > "${tmp}/out" 2>&1
if cmp -s "${tmp}/.env" "${tmp}/.env.first" && grep -q '^LIGHTRAG_MCP_ENABLED *rag *on$' "${tmp}/out"; then
  echo "ok: --list reports without writing"
else
  echo "FAIL: --list reports without writing"
  cat "${tmp}/out"
  fail=1
fi

exit "$fail"
