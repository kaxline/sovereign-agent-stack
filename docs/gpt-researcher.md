[← Back to README](../README.md)

# GPT Researcher (deep research)

Autonomous multi-step web research that writes structured reports, backed by SearXNG and your local model server.

[GPT Researcher](https://github.com/assafelovic/gpt-researcher) performs autonomous multi-step web research and writes structured reports. Retrieval goes through your existing **SearXNG** instance. LLM calls go to `llm-proxy` (`OPENAI_BASE_URL=http://llm-proxy:4000/v1`, `OPENAI_API_KEY=local-llm`). The proxy forwards them to `LLM_BINDING_HOST` with the real key. GPT Researcher has one base URL, so its embeddings use that chat route. LightRAG can split chat and embeddings on the proxy.

Two containers run it, each in its own set of profiles:

| Service | Role | Profiles | Consumers |
|---|---|---|---|
| `gpt-researcher` | REST API on 127.0.0.1:`GPTR_PORT` (8000) | `automation` | n8n |
| `gptr-mcp` | MCP sidecar (SSE, internal, no published port) | `research`, `automation` | Hermes, OpenCode |

`gptr-mcp` runs GPT Researcher in-process and does not call the REST service.

Quick search (`mcp-searxng` / SearXNG JSON) and deep research (`gptr-mcp`) do different jobs, so keep both.

## Turning it on for Hermes

Set `GPTR_MCP_ENABLED=1` in `.env` and run `make up`, or run
`./scripts/setup.sh --research`. The sync adds the `research` profile, which
starts `gptr-mcp` without n8n, and both Hermes bootstraps register it as
`mcp_servers.gptr` with five tools: `deep_research`, `quick_search`,
`write_report`, `get_research_sources` and `get_research_context`. The first
three are pinned so local models call them directly.

The default profile (dashboard, CLI, Boundary) and the browser profile (WebUI)
get the tools. The api-server profile does not, because one `deep_research`
call sends dozens of requests to `llm-proxy` and the model server. Hermes waits
up to `GPTR_MCP_TIMEOUT` seconds (900) for one call.

`GPTR_MCP_ENABLED=0` removes the profile again, if the sync added it, and the
next bootstrap drops the `gptr` entries. With `automation` on and the flag at
`0`, `gptr-mcp` still runs for OpenCode and n8n, but Hermes doesn't register it.

The flag, profile, tools, pins and profile list come from
[`addons/gptr/addon.json`](../addons/README.md), which both `sync-profiles.sh`
and the bootstraps read. `GPTR_MCP_URL`, `GPTR_MCP_TIMEOUT` and
`GPTR_MCP_CONNECT_TIMEOUT` still override the manifest's values.

## Configuration

1. Copy the GPT Researcher block from `.env.example` into `.env` (or rely on defaults wired in `docker-compose.yml`).
2. Ensure `LLM_MODEL` and `EMBEDDING_MODEL` match what your model server is serving — GPT Researcher reads them via the `x-gptr` compose anchor.
3. GPT Researcher uses `SEARX_URL` (not `SEARXNG_URL`). Compose sets it from `WEB_SEARCH_URL`, which defaults to `http://searxng:8080`; point that at the [search gate](searxng.md#search-gate) to pace research queries.
4. Start the stack: `make up` (first run builds `gptr-mcp`).

## Resource sharing

LightRAG, OpenCode, Hermes, and GPT Researcher share `llm-proxy`, which forwards to the host model endpoint (`LLM_BINDING_HOST`, often `http://host.docker.internal:1234/v1`). Deep research fires off many parallel LLM requests, so the defaults in `.env.example` (`GPTR_MAX_SCRAPER_WORKERS=2`, `GPTR_DEEP_RESEARCH_CONCURRENCY=1`, and friends) are tuned to keep load manageable on a 16 GB machine. Avoid running deep research while LightRAG is indexing or Hermes has active subagents.

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

# Deep research via n8n webhook (publish the workflow in the UI first)
curl -X POST http://localhost:5678/webhook/deep-research \
  -H "Content-Type: application/json" \
  -d '{"query": "What is GPT Researcher?"}'
```
