#!/usr/bin/env bash
# Sync the Neo4j admin password with NEO4J_PASSWORD in .env when the data
# volume was initialized with a different password (healthcheck auth failures).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[neo4j-reset] %s\n' "$1"; }
die() { printf '[neo4j-reset] ERROR: %s\n' "$1" >&2; exit 1; }

[[ -f .env ]] || die "Missing .env — run ./scripts/setup.sh first"

# shellcheck disable=SC1091
source .env

[[ -n "${NEO4J_PASSWORD:-}" ]] || die "NEO4J_PASSWORD is not set in .env"
NEO4J_USERNAME="${NEO4J_USERNAME:-neo4j}"

log "Stopping neo4j service"
docker compose stop neo4j

RESET_CONTAINER=""
cleanup() {
  if [[ -n "$RESET_CONTAINER" ]]; then
    docker rm -f "$RESET_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

log "Starting temporary Neo4j with auth disabled"
RESET_CONTAINER="$(docker compose run -d --no-deps \
  -e NEO4J_dbms_security_auth__enabled=false \
  -e "NEO4J_AUTH=${NEO4J_USERNAME}/placeholder1" \
  neo4j)"
RESET_CONTAINER="${RESET_CONTAINER//$'\n'/}"

log "Waiting for Neo4j to accept connections"
ready=0
for _ in $(seq 1 60); do
  if docker exec "$RESET_CONTAINER" cypher-shell -u "$NEO4J_USERNAME" "RETURN 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[[ "$ready" -eq 1 ]] || die "Timed out waiting for temporary Neo4j"

log "Setting neo4j user password to match .env"
docker exec "$RESET_CONTAINER" cypher-shell -u "$NEO4J_USERNAME" \
  "ALTER USER ${NEO4J_USERNAME} SET PASSWORD '${NEO4J_PASSWORD}'"

docker rm -f "$RESET_CONTAINER" >/dev/null
RESET_CONTAINER=""

log "Starting neo4j service"
docker compose up -d neo4j

log "Waiting for healthcheck"
healthy=0
for _ in $(seq 1 24); do
  status="$(docker inspect lightrag-neo4j --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)"
  if [[ "$status" == "healthy" ]]; then
    healthy=1
    break
  fi
  sleep 5
done

if [[ "$healthy" -eq 1 ]]; then
  log "Neo4j is healthy"
else
  die "Neo4j started but healthcheck did not pass — run: docker inspect lightrag-neo4j --format '{{json .State.Health}}'"
fi
