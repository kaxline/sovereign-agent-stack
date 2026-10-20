[← Back to README](../README.md)

# n8n workflows

Workflow automation with starter workflows and credentials pre-imported for LightRAG, Neo4j, SearXNG, and GPT Researcher. Enable with the **`automation`** profile (pair with `core` and usually `rag`):

```bash
./scripts/setup.sh --rag --automation
# COMPOSE_PROFILES=core,rag,automation
```

n8n follows patterns from the [n8n self-hosted AI starter kit](https://github.com/n8n-io/self-hosted-ai-starter-kit): a one-shot `n8n-import` container and demo data under `n8n/demo-data/`. Unlike the starter kit, n8n stores its data in SQLite, not Postgres. Ollama and Qdrant are left out by default, since LightRAG owns RAG here (`rag` profile) and inference runs on your model server or the `ollama` profile.

## Where n8n stores data

Workflows, credentials, the owner account and execution history live in SQLite at `/home/node/.n8n/database.sqlite` on the `n8n_storage` volume, next to the `config` file that holds the instance's encryption key. `n8n` and `n8n-import` both mount that volume and set `DB_TYPE=sqlite`. A compose overlay that binds `/home/node/.n8n` somewhere else must do it for both services.

`n8n-import` runs before `n8n` on every `up`. It imports the demo credentials and workflows only when `database.sqlite` does not exist yet, and otherwise exits without opening the database, so it never touches a store a running n8n holds.

Back up the volume with n8n stopped, since copying a SQLite file mid-write can give a corrupt copy:

```bash
docker compose stop n8n
docker run --rm -v assistant_n8n_storage:/from:ro -v "$PWD":/to alpine \
  tar czf /to/n8n_storage.tgz -C /from .
docker compose up -d n8n
```

The volume name is `<compose project>_n8n_storage`; check it with `docker volume ls`.

## First-time n8n setup

1. Run `./scripts/setup.sh --automation` (add `--rag` if you want the LightRAG/Neo4j demos). Setup sets secrets and regenerates encrypted credentials under `n8n/demo-data/credentials/`.

2. Start the stack: `make up`

3. Open `http://localhost:5678`, create your owner account (one-time).

4. Six starter workflows are auto-imported:
   - **LightRAG Query (Webhook)** — `POST /webhook/lightrag-query` with `{"question": "...", "mode": "mix"}`
   - **LightRAG Scan Inputs** — triggers `/documents/scan` on `$ASSISTANT_DATA_ROOT/inputs`
   - **Neo4j Explore Graph** — runs a label-count Cypher query via Neo4j HTTP API
   - **Web Search (Webhook)** — `POST /webhook/web-search` with `{"query": "..."}`
   - **Search and Query LightRAG (Webhook)** — web search + LightRAG query chain
   - **Deep Research (Webhook)** — `POST /webhook/deep-research` with `{"query": "..."}`

## Credentials

Encrypted credential JSON under `n8n/demo-data/credentials/` is **gitignored** and created locally by `./scripts/setup.sh` (or `node scripts/generate-n8n-credentials.js`). The files are encrypted with `N8N_ENCRYPTION_KEY` so they line up with your `.env` secrets. Never commit them.

| Credential | Type | Matches `.env` key |
|---|---|---|
| LightRAG API | Header Auth (`X-API-Key`) | `LIGHTRAG_API_KEY` |
| Neo4j | Basic Auth | `NEO4J_USERNAME` / `NEO4J_PASSWORD` |

If you change `LIGHTRAG_API_KEY`, `NEO4J_PASSWORD`, or `N8N_ENCRYPTION_KEY` after first boot, regenerate and re-import:

```bash
# Regenerate encrypted credential JSON (reads from your .env)
export $(grep -E '^(N8N_ENCRYPTION_KEY|LIGHTRAG_API_KEY|NEO4J_PASSWORD|NEO4J_USERNAME)=' .env | xargs)
node scripts/generate-n8n-credentials.js

# Re-import credentials into the existing database, with n8n stopped
docker compose stop n8n
docker compose run --rm --no-deps --entrypoint /bin/sh n8n-import -c \
  'n8n import:credentials --separate --input=/demo-data/credentials'
docker compose up -d n8n
```

`N8N_ENCRYPTION_KEY` has to match the key in `/home/node/.n8n/config` on the volume. n8n 2.x refuses to start when they differ ("Mismatching encryption keys"). 1.x starts with the `.env` key and then can't decrypt credentials saved under the other one.

Alternatively, create credentials manually in the n8n UI and re-attach them to the starter workflows.

## Upgrading an existing install

Releases up to v0.3.2 defined a Postgres service for n8n, but n8n never used it: the `n8n` service's environment list replaced the shared one, so it never got the `DB_*` settings and ran on SQLite all along. `n8n-import` did get them and seeded the demo data into Postgres, where n8n never saw it. Your workflows, credentials and owner account are in SQLite, so the upgrade leaves them where they are.

After moving to this release and running `docker compose up -d`, remove the old Postgres container. Its volume holds only the demo data `n8n-import` put there:

```bash
docker compose --profile n8n-postgres-legacy rm -sf postgres
docker volume rm assistant_postgres_storage   # optional
```

Downstream overlays: bind `n8n-import`'s `/home/node/.n8n` to the same place as n8n's, and drop any override of the `postgres` service. The service is kept, under a profile nothing enables, only so those overrides still parse; it will be removed in a later release.

An existing install never got the starter workflows in n8n. To add them (IDs that already exist are overwritten):

```bash
./scripts/setup.sh            # regenerates n8n/demo-data/credentials
docker compose stop n8n
docker compose run --rm --no-deps --entrypoint /bin/sh n8n-import -c \
  'n8n import:credentials --separate --input=/demo-data/credentials && n8n import:workflow --separate --input=/demo-data/workflows'
docker compose up -d n8n
```

## HTTP Request patterns

**LightRAG query** (`http://lightrag:9621/query`):

```json
{
  "query": "What entities are in my knowledge base?",
  "mode": "mix",
  "include_references": true
}
```

Use Header Auth with `X-API-Key`, and set the node timeout to 600000 ms. Queries against a local LLM are slow.

| Operation | Method | Path |
|---|---|---|
| Health check | GET | `/health` |
| List documents | POST | `/documents/paginated`, body `{"page": 1, "page_size": 200}` (10–200 per page) |
| Count documents by status | GET | `/documents/status_counts` |
| Ingest text | POST | `/documents/text` |
| Scan inputs dir | POST | `/documents/scan` |
| Track ingest job | GET | `/documents/track_status/{track_id}` |
| API explorer | GET | `/docs` |

**Neo4j Cypher** (`http://neo4j:7474/db/neo4j/tx/commit`):

```json
{
  "statements": [
    {
      "statement": "MATCH (n) RETURN labels(n) AS label, count(*) AS cnt ORDER BY cnt DESC LIMIT 20",
      "parameters": {}
    }
  ]
}
```

Use Basic Auth (`neo4j` / `NEO4J_PASSWORD`). Cypher has to be a single line in JSON. Stick to read-only queries here; LightRAG owns graph ingestion, so go through its APIs to modify the knowledge base.

**SearXNG search** (`http://searxng:8080/search`):

```
GET http://searxng:8080/search?q=your+query&format=json
```

No authentication required (internal instance). Response includes a `results` array with `title`, `url`, and `content` per hit.

**GPT Researcher** (`http://gpt-researcher:8000/report/`):

```json
{
  "task": "Your research question",
  "report_type": "research_report",
  "report_source": "web",
  "generate_in_background": false
}
```

No authentication required (internal instance). Set timeout to 600000 ms. Response includes `report`, `research_id`, and `costs`.

## Shared files

n8n mounts `$ASSISTANT_DATA_ROOT/inputs` at `/data/shared` (read-only). Files for your workspace live under `$ASSISTANT_DATA_ROOT/inputs/<WORKSPACE>/` on the host. Use **Read/Write Files from Disk** or **Local File Trigger** nodes with paths under `/data/shared/<WORKSPACE>/`. See [User data directory](data-dir.md).

Scan and query workflows always hit the **hot** LightRAG workspace (whatever `WORKSPACE` is in `.env`). To switch corpora from the host, use `make corpus-use SLUG=…` — see [Knowledge bases](knowledge-bases.md).

## Verification

```bash
# Stack healthy
docker compose ps

# n8n-import seeded the database (first run), or printed
# "n8n database exists, skipping import"
docker compose logs n8n-import

# LightRAG auth
curl -H "X-API-Key: $LIGHTRAG_API_KEY" http://localhost:9621/health

# Neo4j HTTP API
curl -u neo4j:$NEO4J_PASSWORD \
  -H "Content-Type: application/json" \
  -d '{"statements":[{"statement":"RETURN 1"}]}' \
  http://localhost:7474/db/neo4j/tx/commit

# LightRAG query via n8n webhook (publish the workflow in the UI first)
curl -X POST http://localhost:5678/webhook/lightrag-query \
  -H "Content-Type: application/json" \
  -d '{"question": "Summarize my knowledge base", "mode": "mix"}'

# SearXNG JSON API
curl 'http://localhost:8080/search?q=test&format=json'
```
