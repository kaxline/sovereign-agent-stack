[← Back to README](../README.md)

# GPT Researcher (deep research)

Autonomous multi-step web research that writes structured reports, backed by SearXNG and your local model server.

[GPT Researcher](https://github.com/assafelovic/gpt-researcher) performs autonomous multi-step web research and writes structured reports. Retrieval goes through your existing **SearXNG** instance, and LLM calls go to your **model server** (via `OPENAI_BASE_URL` / `host.docker.internal`), the same one LightRAG, OpenCode, and Hermes use.

Two containers share the stack:

| Service | Role | Consumers |
|---|---|---|
| `gpt-researcher` | REST API on port 8000 | n8n, Hermes (HTTP) |
| `gptr-mcp` | MCP sidecar (SSE, internal) | OpenCode, Hermes (MCP) |

Quick search (`mcp-searxng` / SearXNG JSON) and deep research (`gptr-mcp` / GPT Researcher REST) do different jobs, so keep both.

## Configuration

1. Copy the GPT Researcher block from `.env.example` into `.env` (or rely on defaults wired in `docker-compose.yml`).
2. Ensure `LLM_MODEL` and `EMBEDDING_MODEL` match what your model server is serving — GPT Researcher reads them via the `x-gptr` compose anchor.
3. GPT Researcher uses `SEARX_URL` (not `SEARXNG_URL`) — already set to `http://searxng:8080/` in compose.
4. Start the stack: `docker compose up -d` (first run builds `gptr-mcp`).

## Resource sharing

LightRAG, OpenCode, Hermes, and GPT Researcher typically share one host model endpoint (`OPENAI_BASE_URL`, often `http://host.docker.internal:1234/v1`). Deep research fires off many parallel LLM requests, so the defaults in `.env.example` (`GPTR_MAX_SCRAPER_WORKERS=2`, `GPTR_DEEP_RESEARCH_CONCURRENCY=1`, and friends) are tuned to keep load manageable on a 16 GB machine. Avoid running deep research while LightRAG is indexing or Hermes has active subagents.

## n8n usage

Inside the compose network:

```http
POST http://gpt-researcher:8000/report/
Content-Type: application/json

{
  "task": "What are the latest developments in local LLM inference?",
  "report_type": "research_report",
  "report_source": "web",
  "tone": "Objective",
  "generate_in_background": false
}
```

Set the HTTP Request node timeout to **600000 ms** (10 min). Research against a local model is slow.

A starter workflow is auto-imported on first boot:

- **Deep Research (Webhook)** — `POST /webhook/deep-research` with `{"query": "..."}`

For existing n8n installs, re-import workflows (see [SearXNG n8n usage](searxng.md#n8n-usage)).

## OpenCode MCP

OpenCode connects to `http://gptr-mcp:8000/sse` (configured in `opencode/opencode.local.json`). Available tools include `deep_research`, `quick_search`, `write_report`, `get_research_sources`, and `get_research_context`. Use `mcp-searxng` for fast lookups; use `gptr` for in-depth research.

## Verification

```bash
# REST (returns HTML on root; JSON on /report/)
curl -sf http://localhost:8000/ >/dev/null && echo "gptr REST ok"

# MCP sidecar health (internal)
docker compose exec gptr-mcp wget -qO- http://127.0.0.1:8000/health

# End-to-end research (slow; needs the model server up)
curl -X POST http://localhost:8000/report/ \
  -H "Content-Type: application/json" \
  -d '{"task":"What is SearXNG?","report_type":"research_report","report_source":"web","generate_in_background":false}'

# Deep research via n8n webhook (activate workflow first in UI)
curl -X POST http://localhost:5678/webhook/deep-research \
  -H "Content-Type: application/json" \
  -d '{"query": "What is GPT Researcher?"}'
```
