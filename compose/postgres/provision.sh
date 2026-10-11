#!/bin/sh
# Idempotent role/schema reconciliation for the shared `db` Postgres.
#
# Runs as the one-shot db-provision service on every `up`. Safe to re-run: every
# statement either creates what is missing or re-asserts what should be true, so
# a rotated password or a newly registered service lands on the next start.
#
# Layout (one database, one schema per owner):
#   agent       written by agent_rw only; read by agent_ro and agent_rw
#   <slug>      one per services/<slug>.env; written by svc_<slug> only;
#               read by agent_ro and agent_rw
#
# Grants are the boundary. Nothing here relies on the agent following
# instructions about where it may write.
set -eu

SERVICES_DIR="${DB_SERVICES_DIR:-/services}"
DB_NAME="${PGDATABASE:?PGDATABASE is required}"

log() { printf '[db-provision] %s\n' "$1"; }
die() { printf '[db-provision] ERROR: %s\n' "$1" >&2; exit 1; }

[ -n "${PGPASSWORD:-}" ] || die "DB_ADMIN_PASSWORD is empty — run ./scripts/setup.sh"
[ -n "${DB_AGENT_RO_PASSWORD:-}" ] || die "DB_AGENT_RO_PASSWORD is empty — run ./scripts/setup.sh"
[ -n "${DB_AGENT_RW_PASSWORD:-}" ] || die "DB_AGENT_RW_PASSWORD is empty — run ./scripts/setup.sh"

psql_run() {
  PGOPTIONS="-c client_min_messages=warning" psql -X -q -v ON_ERROR_STOP=1 "$@"
}

log "Reconciling agent roles and the agent schema in '${DB_NAME}'"
psql_run -v db="$DB_NAME" -v ro_pw="$DB_AGENT_RO_PASSWORD" -v rw_pw="$DB_AGENT_RW_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE agent_ro LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') \gexec
SELECT 'CREATE ROLE agent_rw LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_rw') \gexec

ALTER ROLE agent_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 10 PASSWORD :'ro_pw';
ALTER ROLE agent_rw WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 10 PASSWORD :'rw_pw';

-- Session defaults. A client can SET these back, so they bound accidents, not
-- intent; the grants below are what actually deny writes.
ALTER ROLE agent_ro IN DATABASE :"db" SET default_transaction_read_only = on;
ALTER ROLE agent_ro IN DATABASE :"db" SET statement_timeout = '30s';
ALTER ROLE agent_ro IN DATABASE :"db" SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE agent_rw IN DATABASE :"db" SET statement_timeout = '60s';
ALTER ROLE agent_rw IN DATABASE :"db" SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE agent_rw IN DATABASE :"db" SET search_path = agent, public;

-- Nobody connects, makes temp tables, or creates in public unless granted.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO agent_ro, agent_rw;

-- The agent schema is owned by the admin role, not agent_rw, so the agent can
-- create and drop its own tables but cannot drop or re-grant the schema.
CREATE SCHEMA IF NOT EXISTS agent;
ALTER SCHEMA agent OWNER TO CURRENT_USER;
REVOKE ALL ON SCHEMA agent FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA agent TO agent_rw;
GRANT USAGE ON SCHEMA agent TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA agent TO agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_rw IN SCHEMA agent GRANT SELECT ON TABLES TO agent_ro;
COMMENT ON SCHEMA agent IS
  'Scratch and derived tables the assistant created itself (agent_rw). Not a source of record.';

-- One place to discover data: every non-system schema, table, and column with
-- its COMMENT ON text. Plain catalog joins rather than obj_description(),
-- which postgres-mcp's restricted mode rejects.
CREATE OR REPLACE VIEW public.data_catalog AS
SELECT n.nspname AS schema_name, NULL::name AS table_name, NULL::name AS column_name,
       'schema'::text AS kind, NULL::text AS data_type, d.description
FROM pg_catalog.pg_namespace n
LEFT JOIN pg_catalog.pg_description d
  ON d.objoid = n.oid AND d.classoid = 'pg_catalog.pg_namespace'::regclass
WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname NOT IN ('information_schema', 'public')
UNION ALL
SELECT n.nspname, c.relname, NULL,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
                      WHEN 'f' THEN 'foreign table' ELSE 'table' END,
       NULL, d.description
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_description d
  ON d.objoid = c.oid AND d.classoid = 'pg_catalog.pg_class'::regclass AND d.objsubid = 0
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname NOT IN ('information_schema', 'public')
UNION ALL
SELECT n.nspname, c.relname, a.attname, 'column',
       pg_catalog.format_type(a.atttypid, a.atttypmod), d.description
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_description d
  ON d.objoid = c.oid AND d.classoid = 'pg_catalog.pg_class'::regclass AND d.objsubid = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname NOT LIKE 'pg\_%' AND n.nspname NOT IN ('information_schema', 'public');
REVOKE ALL ON public.data_catalog FROM PUBLIC;
GRANT SELECT ON public.data_catalog TO agent_ro, agent_rw;
COMMENT ON VIEW public.data_catalog IS
  'Every schema, table, and column outside the system schemas, with its COMMENT ON description. Start here.';
SQL

# --- Service schemas ---------------------------------------------------------
# One file per integration: services/<slug>.env with DB_SERVICE_PASSWORD=...
# (make db-service-add NAME=<slug> writes it). The slug is both the schema
# name and the svc_<slug> role suffix, so it must be a plain SQL identifier.
slugs=""
if [ -d "$SERVICES_DIR" ]; then
  for file in "$SERVICES_DIR"/*.env; do
    [ -f "$file" ] || continue
    base="$(basename "$file")"
    slug="${base%.env}"
    case "$slug" in
      agent|public|information_schema|pg_*)
        log "WARNING: skipping ${base} - '${slug}' is a reserved schema name"
        continue
        ;;
    esac
    if ! printf '%s' "$slug" | grep -Eq '^[a-z][a-z0-9_]{0,57}$'; then
      log "WARNING: skipping ${base} - slug must match [a-z][a-z0-9_]{0,57}"
      continue
    fi

    password=""
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        DB_SERVICE_PASSWORD=*) password="${line#DB_SERVICE_PASSWORD=}" ;;
      esac
    done < "$file"
    password="$(printf '%s' "$password" | tr -d '\r')"
    if [ -z "$password" ]; then
      log "WARNING: skipping ${base} - DB_SERVICE_PASSWORD is empty"
      continue
    fi

    log "Reconciling service schema '${slug}' (role svc_${slug})"
    psql_run -v db="$DB_NAME" -v schema="$slug" -v role="svc_${slug}" -v pw="$password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec

ALTER ROLE :"role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 10 PASSWORD :'pw';
ALTER ROLE :"role" IN DATABASE :"db" SET search_path = :"schema";
GRANT CONNECT ON DATABASE :"db" TO :"role";

-- The service owns its schema and migrates it itself. It has no CREATE on the
-- database, so it cannot make any other schema.
CREATE SCHEMA IF NOT EXISTS :"schema" AUTHORIZATION :"role";
ALTER SCHEMA :"schema" OWNER TO :"role";
REVOKE ALL ON SCHEMA :"schema" FROM PUBLIC;
GRANT USAGE ON SCHEMA :"schema" TO agent_ro, agent_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA :"schema" TO agent_ro, agent_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE :"role" IN SCHEMA :"schema"
  GRANT SELECT ON TABLES TO agent_ro, agent_rw;
SQL
    slugs="${slugs} ${slug}"
  done
fi

# A service whose file is gone loses its login. Its schema and data stay; drop
# them by hand (docs/database.md) once you are sure nothing needs them.
psql_run -At -v keep="$(printf '%s' "$slugs" | tr ' ' '\n' | sed '/^$/d' | sed 's/^/svc_/' | paste -sd, -)" <<'SQL' |
SELECT format('ALTER ROLE %I NOLOGIN', rolname)
FROM pg_roles
WHERE rolname LIKE 'svc\_%'
  AND rolcanlogin
  AND NOT (rolname = ANY (COALESCE(string_to_array(NULLIF(:'keep', ''), ','), '{}'::text[])))
SQL
while IFS= read -r stmt; do
  [ -n "$stmt" ] || continue
  log "No services/*.env for this role any more: ${stmt}"
  printf '%s;\n' "$stmt" | psql_run
done

count="$(printf '%s' "$slugs" | wc -w | tr -d ' ')"
log "Done: agent_ro, agent_rw, ${count} service schema(s)"
