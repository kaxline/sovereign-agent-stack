[← Back to README](../README.md)

# SearXNG web search

Local meta-search API for quick lookups, shared by n8n over HTTP and OpenCode over MCP.

[SearXNG](https://docs.searxng.org/) runs as a shared internal meta-search API for **quick** lookups. **n8n** calls it directly over HTTP; **OpenCode** uses it via an MCP sidecar (`mcp-searxng`). For **deep** multi-step research reports, use [GPT Researcher](gpt-researcher.md) (separate service, same SearXNG backend).

## Configuration

1. Run `./scripts/setup.sh` — it generates `SEARXNG_SECRET` and syncs it into `searxng/settings.local.yml` (gitignored).
2. To change the secret later, update both `.env` and `server.secret_key` in `searxng/settings.local.yml` (or re-run setup).
3. Optionally set `SEARXNG_PORT` in `.env` (default `8080`).
4. Start the stack: `docker compose up -d`

SearXNG binds `127.0.0.1` only. Keep the JSON API off the public internet: without rate limiting it is easy to abuse.

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

`web_url_read` fetches a single known URL and returns it as markdown. That is the cheapest way to read a specific page, with no search step and no research agent involved. It does not execute JavaScript, and it blocks private/internal URLs.

## Empty results / suspended engines

Upstream engines (Brave, DuckDuckGo, Google CSE, Startpage) often CAPTCHA or rate-limit residential and datacenter IPs. SearXNG then marks them **Suspended** and default search returns `results: []` even though the API is healthy.

This stack enables **Yep** as a general-search fallback and shortens `search.suspended_times` so bans clear faster. After changing `searxng/settings.yml`, re-sync the secret into the gitignored overlay and recreate:

```bash
./scripts/setup.sh   # or make ensure-local — refreshes settings.local.yml from the template
docker compose up -d --force-recreate searxng
```

Check suspensions with:

```bash
curl -sS 'http://localhost:8080/search?q=ownCloud&format=json' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("results",[])), d.get("unresponsive_engines"))'
```

A non-zero result count with some engines still listed as Suspended is fine — at least one engine answered.

## Optional rate limiting

For production-style rate limiting, start with the Valkey profile and uncomment the `redis` / `limiter` block in `searxng/settings.local.yml`:

```bash
docker compose --profile searxng-prod up -d
```

## Verification

```bash
# SearXNG JSON API (must return results, not 403)
curl 'http://localhost:8080/search?q=test&format=json'

# MCP sidecar health (loopback inside the container)
docker compose exec mcp-searxng wget -qO- http://127.0.0.1:3000/health

# Cross-container reachability (must succeed; needs MCP_HTTP_HOST=0.0.0.0)
docker compose exec hermes python3 -c \
  "import urllib.request; print(urllib.request.urlopen('http://mcp-searxng:3000/health', timeout=5).read().decode())"

# Web search via n8n webhook (activate workflow first in UI)
curl -X POST http://localhost:5678/webhook/web-search \
  -H "Content-Type: application/json" \
  -d '{"query": "LightRAG knowledge graph"}'
```
