[← Back to README](../README.md)

# SearXNG web search

Local meta-search API for quick lookups, shared by n8n over HTTP and OpenCode over MCP.

[SearXNG](https://docs.searxng.org/) runs as a shared internal meta-search API for **quick** lookups. **n8n** calls it directly over HTTP; **OpenCode** uses it via an MCP sidecar (`mcp-searxng`). For **deep** multi-step research reports, use [GPT Researcher](gpt-researcher.md) (separate service, same SearXNG backend).

## Configuration

1. Run `./scripts/setup.sh` — it generates `SEARXNG_SECRET` and syncs it into `searxng/settings.local.yml` (gitignored).
2. To change the secret later, update both `.env` and `server.secret_key` in `searxng/settings.local.yml` (or re-run setup).
3. Optionally set `SEARXNG_PORT` in `.env` (default `8080`).
4. Start the stack: `docker compose up -d`

SearXNG is bound to `127.0.0.1` only — do not expose the JSON API publicly; it is abuse-prone without rate limiting.

## n8n usage

Inside the compose network, use `http://searxng:8080/search?q=...&format=json`.

Two starter workflows are auto-imported on first boot:

- **Web Search (Webhook)** — `POST /webhook/web-search` with `{"query": "..."}`
- **Search and Query LightRAG (Webhook)** — `POST /webhook/search-and-query` with `{"query": "...", "question": "...", "mode": "mix"}`

For existing n8n installs (DB already seeded), import manually:

```bash
docker compose run --rm --entrypoint /bin/sh n8n-import -c \
  'n8n import:workflow --separate --input=/demo-data/workflows'
```

## OpenCode and Hermes MCP

OpenCode connects to `http://mcp-searxng:3000/mcp` (configured in `opencode/opencode.local.json`). Hermes is registered against the same sidecar on both its default and `api-server` profiles by [`compose/hermes/bootstrap-api-profile.sh`](../compose/hermes/bootstrap-api-profile.sh), filtered to `searxng_web_search` and `web_url_read`. MCP tools load when the agent invokes them during a session.

`web_url_read` fetches a single known URL and returns it as markdown, which is the cheapest way to read a specific page — no search, no research agent. It does not execute JavaScript, and it blocks private/internal URLs. See [Reading a URL directly](crawl4ai.md) for when to reach for it versus the opt-in Crawl4AI browser.

## Optional rate limiting

For production-style rate limiting, start with the Valkey profile and uncomment the `redis` / `limiter` block in `searxng/settings.local.yml`:

```bash
docker compose --profile searxng-prod up -d
```

## Verification

```bash
# SearXNG JSON API (must return results, not 403)
curl 'http://localhost:8080/search?q=test&format=json'

# MCP sidecar health
docker compose exec mcp-searxng wget -qO- http://127.0.0.1:3000/health

# Web search via n8n webhook (activate workflow first in UI)
curl -X POST http://localhost:5678/webhook/web-search \
  -H "Content-Type: application/json" \
  -d '{"query": "LightRAG knowledge graph"}'
```
