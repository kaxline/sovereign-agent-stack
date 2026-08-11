[← Back to README](../README.md)

# n8n workflows

Workflow automation with starter workflows and credentials pre-imported for LightRAG, Neo4j, SearXNG, and GPT Researcher. Enable with the **`automation`** profile (pair with `core` and usually `rag`):

```bash
./scripts/setup.sh --rag --automation
# COMPOSE_PROFILES=core,rag,automation
```

n8n follows patterns from the [n8n self-hosted AI starter kit](https://github.com/n8n-io/self-hosted-ai-starter-kit): Postgres for n8n metadata, a one-shot `n8n-import` container, and demo data under `n8n/demo-data/`. Ollama and Qdrant are left out by default, since LightRAG owns RAG here (`rag` profile) and inference runs on your model server or the `ollama` profile.

## First-time n8n setup

1. Run `./scripts/setup.sh --automation` (add `--rag` if you want the LightRAG/Neo4j demos). Setup sets secrets and regenerates encrypted credentials under `n8n/demo-data/credentials/`.

2. Start the stack: `make up`

3. Open `http://localhost:5678`, create your owner account (one-time).

4. Six starter workflows are auto-imported:
   - **LightRAG Query (Webhook)** — `POST /webhook/lightrag-query` with `{"question": "...", "mode": "mix"}`
   - **LightRAG Scan Inputs** — triggers `/documents/scan` on `./data/inputs`
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

# Re-import credentials (only needed if n8n DB already exists)
docker compose run --rm --entrypoint /bin/sh n8n-import -c \
  'n8n import:credentials --separate --input=/demo-data/credentials'
```

Alternatively, create credentials manually in the n8n UI and re-attach them to the starter workflows.

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
| List documents | GET | `/documents` |
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

n8n mounts `./data/inputs` at `/data/shared` (read-only). Files for your workspace live under `data/inputs/<WORKSPACE>/` on the host. Use **Read/Write Files from Disk** or **Local File Trigger** nodes with paths under `/data/shared/<WORKSPACE>/`.

## Verification

```bash
# Stack healthy
docker compose ps

# n8n-import succeeded (first run only)
docker compose logs n8n-import

# LightRAG auth
curl -H "X-API-Key: $LIGHTRAG_API_KEY" http://localhost:9621/health

# Neo4j HTTP API
curl -u neo4j:$NEO4J_PASSWORD \
  -H "Content-Type: application/json" \
  -d '{"statements":[{"statement":"RETURN 1"}]}' \
  http://localhost:7474/db/neo4j/tx/commit

# LightRAG query via n8n webhook (activate workflow first in UI)
curl -X POST http://localhost:5678/webhook/lightrag-query \
  -H "Content-Type: application/json" \
  -d '{"question": "Summarize my knowledge base", "mode": "mix"}'

# SearXNG JSON API
curl 'http://localhost:8080/search?q=test&format=json'
```
