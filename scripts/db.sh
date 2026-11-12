#!/usr/bin/env bash
# Shared Postgres (`db` profile) helpers: backup, service registration, psql.
# See docs/database.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/data-root.sh
source "${ROOT}/scripts/lib/data-root.sh"

SERVICES_DIR="${ROOT}/compose/postgres/services"

usage() {
  cat <<'EOF'
Usage: ./scripts/db.sh <command> [args]

Commands:
  backup              pg_dump the shared database (custom format) into
                      DB_BACKUP_DIR (default ./data/db-backups)
  service-add <slug>  Register an integration: writes
                      compose/postgres/services/<slug>.env with a generated
                      password and DATABASE_URL. db-provision creates role
                      svc_<slug> and schema <slug> on its next run.
  service-remove <slug> --yes
                      Drop schema <slug> (CASCADE) and role svc_<slug>, then
                      delete compose/postgres/services/<slug>.env. Every
                      table in the schema is lost. postgres-db must be running.
  psql                Admin psql shell inside postgres-db

Examples:
  make db-backup
  make db-service-add NAME=my_integration
  make db-psql
EOF
}

log() { printf '[db] %s\n' "$1"; }
die() { printf '[db] ERROR: %s\n' "$1" >&2; exit 1; }

db_name() { data_root_env_get DB_NAME assistant; }

require_running() {
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
  if ! docker compose ps --status running postgres-db 2>/dev/null | grep -q postgres-db; then
    die "postgres-db is not running — add db to COMPOSE_PROFILES (./scripts/setup.sh --db) and make up"
  fi
}

cmd_backup() {
  require_running
  local dir name stamp out
  dir="$(resolve_data_root_path "$(data_root_env_get DB_BACKUP_DIR ./data/db-backups)")"
  name="$(db_name)"
  stamp="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dir"
  chmod 700 "$dir"
  out="${dir}/${name}-${stamp}.dump"
  umask 077
  # Local socket inside the container; the admin password never leaves .env.
  if ! docker compose exec -T postgres-db pg_dump -U postgres -d "$name" -Fc > "$out"; then
    rm -f "$out"
    die "pg_dump failed"
  fi
  [[ -s "$out" ]] || { rm -f "$out"; die "pg_dump wrote nothing"; }
  log "Wrote ${out} ($(du -h "$out" | cut -f1 | tr -d ' '))"
  log "Restore: docker compose exec -T postgres-db pg_restore -U postgres -d ${name} --clean --if-exists < ${out}"
}

cmd_service_add() {
  local slug="${1:-}"
  [[ -n "$slug" ]] || die "Usage: ./scripts/db.sh service-add <slug>"
  check_slug "$slug"
  local file="${SERVICES_DIR}/${slug}.env"
  [[ ! -e "$file" ]] || die "${file} already exists — edit or delete it instead"
  mkdir -p "$SERVICES_DIR"
  local password
  password="$(openssl rand -hex 24)"
  (
    umask 077
    cat > "$file" <<EOF
# Shared Postgres registration for ${slug} (gitignored). db-provision reads
# DB_SERVICE_PASSWORD; the ${slug} service reads DATABASE_URL via env_file.
# Role svc_${slug} owns schema ${slug} and can write nowhere else.
DB_SERVICE_PASSWORD=${password}
DATABASE_URL=postgresql://svc_${slug}:${password}@postgres-db:5432/$(db_name)
EOF
  )
  log "Wrote ${file}"
  log "Apply now (if the stack is up): docker compose run --rm db-provision"
  log "The ${slug} service must join the db network and use env_file: ${file#"${ROOT}"/}"
}

check_slug() {
  [[ "$1" =~ ^[a-z][a-z0-9_]{0,57}$ ]] || die "slug must match [a-z][a-z0-9_]{0,57} (it becomes a schema name): $1"
  case "$1" in
    agent|public|information_schema|pg_*) die "'$1' is a reserved schema name" ;;
  esac
}

# Runs with postgres-db named explicitly, so it works while no active profile
# includes it (scripts/addon.sh remove starts it that way).
cmd_service_remove() {
  local slug="${1:-}" yes="${2:-}"
  [[ -n "$slug" ]] || die "Usage: ./scripts/db.sh service-remove <slug> --yes"
  check_slug "$slug"
  [[ "$yes" == "--yes" ]] || die "service-remove drops schema ${slug} and every table in it; pass --yes"
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
  # DROP OWNED BY also revokes the role's grants and default privileges, which
  # DROP ROLE would otherwise refuse over.
  docker compose exec -T postgres-db psql -X -q -v ON_ERROR_STOP=1 -U postgres -d "$(db_name)" \
    -v schema="$slug" -v role="svc_${slug}" <<'SQL' || die "dropping schema ${slug} and role svc_${slug} failed (is postgres-db running?)"
SET client_min_messages = warning;
DROP SCHEMA IF EXISTS :"schema" CASCADE;
SELECT format('DROP OWNED BY %I', :'role')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
DROP ROLE IF EXISTS :"role";
SQL
  rm -f "${SERVICES_DIR}/${slug}.env"
  log "Dropped schema ${slug} and role svc_${slug}; removed services/${slug}.env"
}

cmd_psql() {
  require_running
  exec docker compose exec postgres-db psql -U postgres -d "$(db_name)"
}

case "${1:-}" in
  backup) cmd_backup ;;
  service-add) shift; cmd_service_add "$@" ;;
  service-remove) shift; cmd_service_remove "$@" ;;
  psql) cmd_psql ;;
  -h|--help|"") usage ;;
  *) usage >&2; exit 1 ;;
esac
