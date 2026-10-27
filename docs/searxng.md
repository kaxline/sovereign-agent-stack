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

`web_url_read` fetches a single known URL and returns it as markdown. That is the cheapest way to read a specific page, with no search step and no research agent involved. It does not execute JavaScript, and it blocks private/internal URLs. Many publishers return 403 to this naive fetch; when that happens, prefer search snippets or the local browser tools rather than declaring search empty.

## Empty results / suspended engines

Upstream engines often CAPTCHA or rate-limit residential and datacenter IPs. SearXNG then marks them **Suspended** and default search returns `results: []` even though the API is healthy. One Hermes/MCP query fans out to **every enabled** general engine from a single IP, so parallel agent searches empty the pool quickly.

This stack keeps default general search lean:

| Engine | Role |
|---|---|
| **Bing** + **Google CSE** | Enabled — primary general pair |
| DuckDuckGo, Startpage, Brave, Yep, Mojeek | Disabled — frequent CAPTCHA / 429 / 403 magnets |

`search.suspended_times` are shortened so bans clear without a container restart. After changing `searxng/settings.yml`, re-sync the secret into the gitignored overlay and recreate:

```bash
./scripts/setup.sh   # or make ensure-local — refreshes settings.local.yml from the template
docker compose up -d --force-recreate searxng
```

Check suspensions with:

```bash
curl -sS 'http://localhost:8080/search?q=ownCloud&format=json' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("results",[])), d.get("unresponsive_engines")); print("engines", sorted({r.get("engine") for r in d.get("results",[])}))'
```

A non-zero result count with some engines still listed as Suspended is fine — at least one engine answered. Expect roughly **Bing and/or Google CSE** in the engine set, not six upstreams.

## Search gate

The `search-gate` service is a proxy in front of SearXNG that changes the traffic itself, rather than waiting for SearXNG to suspend engines after they block. Each query goes to one engine (SearXNG's `engines` parameter), and each engine gets at most one request per interval, with ±30% jitter. When an engine answers with a CAPTCHA, 403, 429 or timeout, it rests for 10 minutes, doubling after each further failure up to 6 hours, and the query moves to the next engine. Results are cached in SQLite, and identical queries in flight share one search. It is [`compose/search-gate/search_gate.py`](../compose/search-gate/search_gate.py), standard library only, vendored from the lead-gen project with the changes listed at the top of the file.

It is off by default. To route searches through it, add the profile and point clients at it in `.env`:

```bash
COMPOSE_PROFILES=core,search-gate
WEB_SEARCH_URL=http://search-gate:8080
```

Then `docker compose up -d`. `WEB_SEARCH_URL` sets the base URL for `mcp-searxng` (Hermes and OpenCode) and for GPT Researcher and `gptr-mcp`. n8n's starter workflows still call `searxng` directly. If `WEB_SEARCH_URL` names the gate while its profile is off, searches fail.

`GET /search` answers like SearXNG with an added `gate` field (`cached`, `engine`). It forwards `pageno`, `time_range`, `language` and `safesearch`, and the cache key includes them. It ignores `categories` and `engines`, since the gate picks the engine. A search waits for its turn for up to `SEARCH_GATE_MAX_WAIT` seconds (15). After that the gate answers with no results and a `search-gate` entry in `unresponsive_engines`, and the search stays queued. Hermes sees this as `Search Degraded: … search-gate (busy: the search is queued; ask again in a minute or two)`. Asking again after the search has run returns it from the cache. `mcp-searxng`'s `SEARXNG_TIMEOUT_MS` is 20000, so the gate's answer arrives before the MCP call gives up.

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_GATE_ENGINES` | `bing:10,google cse:20` | `name:interval_s[:daily_cap]`, comma-separated, in order of preference. Each engine must be enabled in SearXNG. |
| `SEARCH_GATE_MAX_WAIT` | `15` | Seconds a search waits before the busy answer. Keep it below `SEARXNG_TIMEOUT_MS`. |
| `SEARCH_GATE_TTL` | `86400` | Seconds a result list stays cached. |
| `SEARCH_GATE_EMPTY_TTL` | `3600` | Seconds an empty answer stays cached. An answer every engine failed on is never cached. |

At the default intervals the gate runs about 9 searches a minute. Of four searches sent at once, two run immediately, one runs about 10 s later and one gets the busy answer. A GPT Researcher report that sends more queries than that loses some sources on its first run and finds them in the cache on a rerun.

`GET /status` shows queue counts, the cache size and, per engine, usage today, cool-down left, consecutive failures, the last error and median block lengths. `GET /blocks` lists each recorded block, from its first failure to the engine's next answer. Use these to tune intervals from observed blocks. From inside the compose network:

```bash
docker compose exec search-gate wget -qO- http://127.0.0.1:8080/status
```

The cache, in the `search_gate_data` volume, holds every query asked and its results. Queries come from chat, so treat the volume like Hermes session content. The request log records only the method and path.

Pacing state (cool-downs, daily counts) is in memory, so a restart clears it. Run one gate per egress IP; two would each pace on their own and double the rate.

`scripts/test-search-gate.py` runs the gate against a fake SearXNG in about four seconds.

## Agent routing (Hermes / WebUI)

Reducing empty results is as much task routing as engine choice. Soft prompt guidance alone is not enough: with `SEARXNG_URL` set, Hermes auto-binds native `web_search` / `web_extract` to SearXNG, extract always fails (search-only), and models spray parallel native searches that empty upstream engines.

This stack **disables the native `web` toolset** on Hermes profiles (via bootstrap `agent.disabled_toolsets: [todo, web]`) **and** rewrites hallucinated `web_search` / `web_extract` at dispatch to MCP SearXNG when args are mappable (cont-init patch) — schema-only disables are not enough because Hermes still executes invented tool names, and bare refusal caused multi-turn retry loops. Hermes also clears `SEARXNG_URL` so a missed call cannot auto-bind to SearXNG — MCP uses `SEARXNG_MCP_URL` instead. Supported paths:

1. **LightRAG** first for KB questions (`query_document` for content, `get_documents` for catalog) — before web search.
2. **One** MCP `searxng_web_search` for a quick web fact — not several parallel query variants.
3. **`web_url_read`** when the URL is already known (no search fan-out); if it 403s, use snippets or the local browser.
4. **`gptr`** (`deep_research` / `quick_search`) for deep multi-step reports — only when the `automation` compose profile is running; otherwise stay on searxng + browser.

`HERMES_ENVIRONMENT_HINT` repeats that policy for every gateway. See [Hermes](hermes.md#optional-internal-integrations).

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

# Web search via n8n webhook (publish the workflow in the UI first)
curl -X POST http://localhost:5678/webhook/web-search \
  -H "Content-Type: application/json" \
  -d '{"query": "LightRAG knowledge graph"}'
```
