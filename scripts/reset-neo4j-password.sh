#!/usr/bin/env bash
# Sync the Neo4j admin password with NEO4J_PASSWORD in .env when the data
# volume was initialized with a different password (healthcheck auth failures).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '[neo4j-reset] %s\n' "$1"; }
die() { printf '[neo4j-reset] ERROR: %s\n' "$1" >&2; exit 1; }

# shellcheck source=lib/env.sh
source "${ROOT}/scripts/lib/env.sh"

[[ -f .env ]] || die "Missing .env — run ./scripts/setup.sh first"

NEO4J_PASSWORD="$(env_get NEO4J_PASSWORD)"
NEO4J_USERNAME="$(env_get NEO4J_USERNAME neo4j)"
[[ -n "$NEO4J_PASSWORD" ]] || die "NEO4J_PASSWORD is not set in .env"

# cypher_string VALUE: VALUE as a single-quoted Cypher string literal, with \
# and ' escaped. sed, not ${v//pat/rep}: macOS's bash 3.2 keeps the backslash
# in a \\\' replacement that bash 5 drops, which closed the literal early.
cypher_string() {
  printf "'%s'" "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g")"
}

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

log "Setting ${NEO4J_USERNAME}'s password to match .env"
# Both values go in on stdin as parameters, so neither is parsed as Cypher nor
# shows up in the docker exec command line.
# shellcheck disable=SC2016  # $user and $pw are Cypher parameters
printf ':param user => %s\n:param pw => %s\nALTER USER $user SET PASSWORD $pw;\n' \
  "$(cypher_string "$NEO4J_USERNAME")" "$(cypher_string "$NEO4J_PASSWORD")" \
  | docker exec -i "$RESET_CONTAINER" cypher-shell -u "$NEO4J_USERNAME" -d system --format plain >/dev/null

docker rm -f "$RESET_CONTAINER" >/dev/null
RESET_CONTAINER=""

log "Starting neo4j service"
docker compose up -d neo4j

log "Waiting for healthcheck"
neo4j_id="$(docker compose ps -q neo4j)"
healthy=0
for _ in $(seq 1 24); do
  status="$(docker inspect "$neo4j_id" --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)"
  if [[ "$status" == "healthy" ]]; then
    healthy=1
    break
  fi
  sleep 5
done

if [[ "$healthy" -eq 1 ]]; then
  log "Neo4j is healthy"
else
  die "Neo4j started but healthcheck did not pass — run: docker compose ps neo4j; docker inspect $neo4j_id --format '{{json .State.Health}}'"
fi
