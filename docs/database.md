# Shared database (Postgres + SQL MCP)

Opt-in Postgres for structured data: rows that integrations sync in, and tables
the agent keeps for itself. Hermes queries it through a SQL MCP server. Reads
span every schema, and writes are confined by database grants.

Requires the **`db`** compose profile (`./scripts/setup.sh --db`). This is
**not** n8n's database. That one is the `postgres` service (container
`n8n-postgres`, `automation` profile), and it is unchanged.

## Quick path

```bash
./scripts/setup.sh --db
make up
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
make doctor
```

In WebUI: *Make me a table of books I want to read, and add Dune.* Then in a
new chat: *What's on my reading list?*

## How it works

| Piece | Role |
|---|---|
| `postgres-db` | Postgres 18, one database (`DB_NAME`, default `assistant`). No host port. Data in `DB_DATA_DIR` |
| `db-provision` | One-shot that runs on every `up`. Creates or updates roles, schemas, grants, and the `data_catalog` view. See `compose/postgres/provision.sh` |
| `sql-mcp-ro` | [postgres-mcp](https://github.com/crystaldba/postgres-mcp) as `agent_ro`, restricted mode, `http://sql-mcp-ro:8000/sse` |
| `sql-mcp-rw` | postgres-mcp as `agent_rw`, unrestricted mode, `http://sql-mcp-rw:8000/sse` |
| `db` network | Internal (no internet). Members: the four services above, `hermes`, and integrations that store data |
| Hermes bootstrap | Registers `mcp_servers.sql` when `SQL_MCP_ENABLED=1` |
| `database` skill | Tells the agent how to find data and where it may write (`compose/hermes/skills/database/`) |

### One database, a schema per owner

Integrations share one database instead of getting one database each. Joins
across sources therefore work, and the SQL tool needs only one connection.

| Schema | Owner / writer | `agent_ro` | `agent_rw` |
|---|---|---|---|
| `agent` | `agent_rw` creates tables. The schema itself belongs to the admin role | read | read, create, write, drop own tables |
| `<slug>` (one per integration) | `svc_<slug>` | read | read |
| `public` | nobody (`CREATE` revoked) | `data_catalog` view | `data_catalog` view |

Each integration role (`svc_<slug>`) owns its schema and can write nowhere
else. It has no `CREATE` on the database, so it cannot create a second schema
or reach another integration's tables. Tables an integration creates later are
readable by both agent roles automatically through `ALTER DEFAULT PRIVILEGES`.

Grants enforce these boundaries. The skill and comments help the agent find
data, but nothing depends on it obeying them.

| Hermes profile | Used by | SQL endpoint |
|---|---|---|
| `api-server` | unattended API / n8n / Boundary | `sql-mcp-ro` (`agent_ro`) |
| default | dashboard, CLI | `sql-mcp-rw` (`agent_rw`) |
| `browser` | Hermes WebUI | `sql-mcp-rw` (`agent_rw`) |

This mirrors the CalDAV read/write split. The API profile is cloned from
default, so the bootstrap re-points its `sql` entry at the read-only sidecar
and drops any other entry that still points at `sql-mcp-rw`.

Allowed tools (all profiles): `list_schemas`, `list_objects`,
`get_object_details`, `execute_sql`, `explain_query`. The index and health
analysers postgres-mcp also ships are left out.

### Guards on the read-only side

1. `agent_ro` has `SELECT` and nothing else. This is the boundary.
2. `sql-mcp-ro` runs in restricted mode, so every statement runs in a read-only
   transaction, `COMMIT`/`ROLLBACK` are rejected, and functions outside an
   allowlist are refused. The last point is why discovery goes through the
   `data_catalog` view rather than `obj_description()`.
3. Role defaults: `default_transaction_read_only = on`, `statement_timeout =
   30s` (`agent_rw`: 60 s), and a 10-connection limit per role.

## Finding data: comments and `data_catalog`

`public.data_catalog` lists every non-system schema, table, view, and column
with its `COMMENT ON` text:

```sql
SELECT schema_name, table_name, kind, description
FROM data_catalog WHERE kind <> 'column' ORDER BY 1, 2;
```

Comment what you create. Integrations should comment their schema, every
table, and any column whose meaning is not obvious (units, currency, sign
conventions). The agent is told to comment its own tables in `agent`.

```sql
COMMENT ON SCHEMA my_integration IS 'Synced nightly from <source>. Read-only copy.';
COMMENT ON TABLE my_integration.items IS 'One row per item; amounts in cents, USD.';
```

## Adding an integration

An integration is a compose service that keeps its data in its own schema.
Keep integration code in its own service. This repo provides only the
database.

1. Register the schema and role:

   ```bash
   make db-service-add NAME=my_integration
   ```

   This writes `compose/postgres/services/my_integration.env` (gitignored,
   mode 600) with `DB_SERVICE_PASSWORD` and a ready `DATABASE_URL`. The slug
   must match `[a-z][a-z0-9_]{0,57}`. It becomes the schema name and the
   `svc_<slug>` role. The `.example` next to it documents the format.

2. Make the integration start the database. Append its profile to the
   `x-db-profiles` anchor at the top of `docker-compose.yml`:

   ```yaml
   x-db-profiles: &db-profiles ["db", "my_integration"]
   ```

   All four db services read that anchor, so enabling the integration's
   profile starts Postgres and the provisioner too.

3. Wire the service:

   ```yaml
   my-integration:
     profiles: ["my_integration"]
     networks: [default, db]          # default only if it needs the internet
     env_file:
       - path: ./compose/postgres/services/my_integration.env
         required: false              # compose parses it with the profile off
     depends_on:
       db-provision:
         condition: service_completed_successfully
   ```

   Add the service to `DB_BRIDGES` (on both networks) or `DB_ONLY` in
   `scripts/test-compose-env-allowlist.py`. That test fails on any
   unexpected member of the `db` network.

4. On start, the service runs its own migrations inside its schema. The
   role's `search_path` is already the schema. It must **not** run `CREATE
   SCHEMA`: the provisioner has created it, and the role has no right to.
   Use `CREATE TABLE IF NOT EXISTS` or a migration tool pointed at that
   schema.

Removing `services/<slug>.env` makes the next provision run set the role to
`NOLOGIN`. The schema and its data stay. To delete them:

```bash
make db-psql
-- then:
DROP SCHEMA my_integration CASCADE;
DROP ROLE svc_my_integration;
```

## Backup and restore

```bash
make db-backup
```

This writes `DB_BACKUP_DIR/<db>-<timestamp>.dump` (default
`./data/db-backups`, mode 600) using `pg_dump -Fc` over the container's
local socket. Roles and passwords are not in the dump. The provisioner
recreates them from `.env` and `services/*.env`.

Restore into the running database:

```bash
docker compose exec -T postgres-db pg_restore -U postgres -d assistant --clean --if-exists < data/db-backups/assistant-YYYYMMDD-HHMMSS.dump
docker compose run --rm db-provision
```

## Storage

The data lives in a bind mount at `DB_DATA_DIR` (default `./data/db`,
gitignored with the rest of `data/`). Like `./data/hermes`, it does **not**
move with `ASSISTANT_DATA_ROOT` or `make data-dir-migrate`. Do not put it in an
iCloud, Dropbox, OneDrive, or Google Drive folder. Sync clients corrupt a live
Postgres cluster. `make doctor` warns when the path looks like one. Keep it
apart from the directory n8n's `postgres` service uses (a downstream override
may bind that at `data/postgres`). The two clusters run different major
versions and must not share a directory.

Postgres 18 images keep the cluster in a versioned subdirectory
(`data/db/18/docker`). A future major upgrade means a dump and restore:
`make db-backup`, stop the stack, move `data/db` aside, bump `DB_IMAGE`,
`make up`, then restore as above.

`make clean` does not touch a bind mount. Delete `data/db` yourself to
start over.

## Settings

| `.env` key | Default | Meaning |
|---|---|---|
| `DB_ADMIN_PASSWORD` | generated by setup | `postgres` superuser. Only `postgres-db` and `db-provision` see it |
| `DB_AGENT_RO_PASSWORD` / `DB_AGENT_RW_PASSWORD` | generated by setup | Agent roles. Hex, so they go into `DATABASE_URI` as-is |
| `DB_NAME` | `assistant` | Database name |
| `DB_DATA_DIR` | `./data/db` | Cluster bind mount |
| `DB_BACKUP_DIR` | `./data/db-backups` | `make db-backup` output |
| `DB_IMAGE` / `SQL_MCP_IMAGE` | `postgres:18-alpine` / `crystaldba/postgres-mcp:0.3.0` | Image pins |
| `SQL_MCP_ENABLED` | `0` (setup `--db` sets `1`) | Hermes registers `mcp_servers.sql` |
| `SQL_MCP_RO_URL` / `SQL_MCP_RW_URL` | `http://sql-mcp-{ro,rw}:8000/sse` | Endpoints the bootstrap registers |
| `SQL_MCP_TIMEOUT` / `SQL_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts (seconds) |

Rotate a password by editing `.env`, then run `docker compose up -d` (the
provisioner applies it and the sidecars restart with the new URI). The admin
password is fixed at first initialisation. Change it with `ALTER ROLE postgres
PASSWORD ...` in `make db-psql` before editing `.env`.

To turn the feature off, set `SQL_MCP_ENABLED=0`, remove `db` from
`COMPOSE_PROFILES`, and re-run both Hermes bootstraps. They remove the `sql`
entries. The data stays in `DB_DATA_DIR`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `postgres-db` exits: *superuser password is not specified* | `DB_ADMIN_PASSWORD` empty. Run `./scripts/setup.sh` |
| Hermes has no `sql` tools | `SQL_MCP_ENABLED=1`, then re-run both bootstraps and `docker compose restart hermes`. `make doctor` |
| `db-provision` failed | `docker compose logs db-provision`. A password set in `.env` after first init does not change the admin password (see Settings) |
| Agent gets `permission denied` | Working as intended for writes outside `agent`, or any write on the API profile |
| Read-only query rejected by validation | Restricted mode refuses some functions. Rewrite with plain catalog joins or `data_catalog` |
