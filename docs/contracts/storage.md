# Storage

Storage covers the data the stack keeps between runs: files under the data root, the shared
Postgres database, the Neo4j graph store and each add-on's store directory. Most of it meets
the rest of the stack as files at fixed container paths. The two databases are reached over
their own wire protocols on the compose network.

## Interface

### The data root

`ASSISTANT_DATA_ROOT` in `.env` names the host directory for user content. The default is
`./data`, the same tree as Hermes state. `scripts/lib/data-root.sh` resolves and checks it:
`~` and paths relative to the repo are expanded, and the result must be absolute, must not be
`/`, must not end in a slash, and must not be `./data/hermes` or inside it. Compose, and `scripts/addon-dirs.sh`, take a value from the
process environment over the one in `.env`.

Services see the data root through bind mounts at fixed container paths, so moving the root
changes only the host side:

| Host path | Container path | Contents |
|---|---|---|
| `<data root>/projects/` | `/opt/projects` | per-project working directories |
| `<data root>/voice/` | `/opt/voice` | writing-voice corpora |
| `<data root>/memory/` | `/opt/memory` | curated long-form notes |
| `<data root>/inputs/` | LightRAG `/app/data/inputs`, n8n `/data/shared` (ro), GPT Researcher `/usr/src/app/my-docs` (ro) | document drop folder |
| `<data root>/rag_storage/` | LightRAG `/app/data/rag_storage` | LightRAG vector and key-value storage |
| `<data root>/corpora/` | (host scripts only) | corpus registry, `registry.json` |
| `<data root>/addons/<short id>/` | per add-on, see below | add-on store directories |

These six subdirectories, without `addons/`, are the ones `make data-dir-migrate` copies.
[User data directory](../data-dir.md) has the commands.

Some data stays outside the data root:

| Setting | Default | Contents |
|---|---|---|
| (fixed) | `./data/hermes` | Hermes config, sessions, `USER.md` and `MEMORY.md`, WebUI state |
| `DB_DATA_DIR` | `./data/db` | the shared Postgres cluster |
| `DB_BACKUP_DIR` | `./data/db-backups` | `make db-backup` output |
| `TWENTY_DATA_DIR` | `./data/twenty` | Twenty's own Postgres (`db/`) and file storage (`storage/`) |
| `ACTUAL_DATA_DIR` | `./data/actual/server` | Actual Budget server data |
| `BITWARDEN_HOST_DATA_DIR` | `./data/addons/bitwarden` | the Bitwarden add-on's `secrets/`, `bw/` and `broker/` |
| `SIGNAL_CLI_DATA_DIR` | set by the user | Signal session store; see [Signal](../signal.md) |

Named Docker volumes hold the rest: `neo4j_data` and `neo4j_logs`, `n8n_storage` (n8n's SQLite
database), `opencode_config` and `opencode_data`, `search_gate_data`, `gptr_outputs`,
`valkey_data`, `ollama_data`, the email add-on's `email_index` and `email_drafts`, and caches.

### Add-on store

Each add-on keeps its settings under `<data root>/addons/<short id>/`, where the short id is the
last dot-separated part of the manifest's `id`. `scripts/lib/addon_store.py` owns the layout:

| Path | Mode | Contents |
|---|---|---|
| `config/settings.env` | 0644 | `ADDON_<SHORT>_<NAME>=value` lines, secrets excluded |
| `instances/<slug>.json` | 0644 | one instance's non-secret values |
| `secrets/<name>`, `secrets/<slug>.<name>` | 0600 | one secret value per file |
| `state.json` | 0600 | what the lifecycle recorded, such as `{"init": {"version", "at"}}` |

The add-on directory and `config/`, `instances/` and `secrets/` are 0700. Files are written to
a temporary name in the same directory and renamed. `scripts/addon-dirs.sh` creates the three
subdirectories for every manifest in `addons/`, enabled or not, before compose starts, because
core services bind-mount some of them and a missing bind source would be created owned by
root on Linux, or refused by Podman. The instance file format is on the tools page under
[instance store](tools.md#instance-store).

### Shared Postgres

| Item | Value |
|---|---|
| Address | `postgres-db:5432` on the internal `db` network; no host port |
| Database | `DB_NAME` (default `assistant`) |
| Agent roles | `agent_ro` (read every schema), `agent_rw` (write schema `agent` only) |
| Integration roles | `svc_<slug>`, owner of schema `<slug>` and nothing else |
| Discovery | the `public.data_catalog` view: every non-system schema, table, view and column with its comment |
| Integration credentials | `compose/postgres/services/<slug>.env` with `DB_SERVICE_PASSWORD` and `DATABASE_URL`, from `make db-service-add NAME=<slug>` |

An integration's service reads `DATABASE_URL` through `env_file`, runs its own migrations inside
its schema with the schema as its `search_path`, and must not run `CREATE SCHEMA`. The
`sql-mcp-ro` and `sql-mcp-rw` sidecars connect as the agent roles. A replacement database has
to keep these role, schema and grant boundaries, because they are what confines writes, not the
agent's instructions. [Shared database](../database.md) describes them in full.

### Neo4j

| Item | Value |
|---|---|
| Address | `bolt://neo4j:7687` (`NEO4J_URI`); HTTP on `neo4j:7474` |
| Credentials | `NEO4J_USERNAME` (default `neo4j`), `NEO4J_PASSWORD` |
| Required plugin | APOC, which LightRAG needs |

LightRAG keeps its knowledge graph there (`LIGHTRAG_GRAPH_STORAGE=Neo4JStorage`) and separates
corpora by a per-workspace node label. See [Knowledge bases](../knowledge-bases.md).

## Current implementation

| Store | Service | Image | Profiles |
|---|---|---|---|
| Shared Postgres | `postgres-db`, plus the one-shot `db-provision` | `postgres:18-alpine` (`DB_IMAGE`) | `db`, `todo`, and any profile appended to `x-db-profiles` |
| SQL access for Hermes | `sql-mcp-ro`, `sql-mcp-rw` | `crystaldba/postgres-mcp:0.3.0` (`SQL_MCP_IMAGE`) | same |
| Graph store | `neo4j` (container `lightrag-neo4j`) | `neo4j:5.26-community` (`NEO4J_IMAGE`) | `neo4j`, `rag` (`x-neo4j-profiles`) |

`db-provision` runs `compose/postgres/provision.sh` on every `up` and reconciles roles,
schemas, grants and `data_catalog` from `.env` and `compose/postgres/services/*.env`. Removing a
service's `.env` file sets its role to `NOLOGIN` and keeps its data. The Postgres 18 image keeps
the cluster in a versioned subdirectory of `DB_DATA_DIR` (`18/docker`), so a major upgrade is
a dump and restore.

Neo4j runs with conservative memory settings (heap 512 MB to 1 GB, page cache 512 MB) and
publishes 7474 and 7687 on loopback (`NEO4J_HTTP_HOST_PORT`, `NEO4J_BOLT_HOST_PORT`).

The deprecated `postgres` service (profile `n8n-postgres-legacy`, volume `postgres_storage`) is
not part of storage: n8n never read it, and no profile starts it.

### Backups

`make db-backup` (`scripts/db.sh backup`) writes `<DB_BACKUP_DIR>/<db>-<timestamp>.dump` at mode
600 with `pg_dump -Fc`. Roles and passwords are not in the dump; `db-provision` recreates them.
Restore steps are in [Backup and restore](../database.md#backup-and-restore). The stack has no
backup command for the data root, `./data/hermes`, Neo4j or the named volumes.

`make clean` runs `docker compose down -v`, which removes the named volumes, and then
`rm -rf data/` in the repo. That takes `./data/hermes` and every default path above that starts
with `./data`, including `./data/db`. A data root or `DB_DATA_DIR` outside the repo is left
alone.

## Consumers

| Store | Used by |
|---|---|
| Data root files | Hermes and `hermes-worker` (projects, voice, memory), Hermes WebUI, LightRAG, n8n, GPT Researcher |
| Add-on store | each add-on's services through their mounts; the Hermes bootstraps read Twenty's and Actual's |
| Shared Postgres | Hermes through the SQL MCP sidecars, Vikunja (`todo` profile), integrations with a schema |
| Neo4j | LightRAG; n8n has `NEO4J_HTTP_URL=http://neo4j:7474` in its environment |
