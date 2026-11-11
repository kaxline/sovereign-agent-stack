# Search

Web search covers finding pages for a query and reading a known URL. It has two seams. Agents
reach it over MCP, through a server Hermes registers as `searxng` with two tools. Services
without an MCP client, and the MCP server itself, call a SearXNG-compatible JSON API over HTTP.
A replacement can sit at either seam: a different search backend behind the HTTP API, or a
different MCP server in front of it.

## Interface

### HTTP search API

A backend answers SearXNG's JSON search API on the compose network:

| Request | Answer |
|---|---|
| `GET /search?q=<query>&format=json` | SearXNG's JSON answer, including the `results` list and `unresponsive_engines` |
| `GET /healthz` | 200 when ready; the compose healthchecks for `searxng` and `search-gate` call it |

Optional query parameters consumers send are `pageno`, `time_range`, `language` and
`safesearch`. An empty `results` with a non-empty `unresponsive_engines` means the backend could
not search right now, as opposed to finding nothing. Hermes' fallback modes rely on that
difference (see [Search modes](#search-modes)).

Consumers find the backend through these settings:

| Setting | Default | Read by |
|---|---|---|
| `WEB_SEARCH_URL` | `http://searxng:8080` | `mcp-searxng` (as its `SEARXNG_URL`), GPT Researcher and `gptr-mcp` (as `SEARX_URL`) |
| `SEARXNG_URL` | `http://searxng:8080` | n8n's environment |

n8n's starter workflows (`n8n/demo-data/workflows/`) call `http://searxng:8080/search`
directly and ignore both settings.

### MCP search server

| Setting | Default |
|---|---|
| `SEARXNG_MCP_URL` | `http://mcp-searxng:3000/mcp` |
| `SEARXNG_MCP_TIMEOUT`, `SEARXNG_MCP_CONNECT_TIMEOUT` | 60s, 30s (Hermes' MCP client) |

The Hermes bootstrap (`compose/hermes/bootstrap-api-profile.sh`, `register_web_mcp_servers`)
registers the server as `searxng` on the default profile and on the profile each bootstrap run
sets up (`api-server`, `browser`), allows only these tools and pins them so they stay loaded:

| Tool | Arguments | Does |
|---|---|---|
| `searxng_web_search` | `query`, and optionally `pageno`, `time_range`, `language`, `safesearch` | one search through the HTTP API |
| `web_url_read` | `url`, and optionally `startChar`, `maxLength`, `section`, `paragraphRange`, `readHeadings` | fetches one page as markdown, without JavaScript |

A replacement MCP server must keep the server name and both tool names. Several Hermes overlays
match on them: the search mode wrapper, the circuit-breaker hint that points the agent at the
browser while `searxng` is paused, and the blocked-fetch hint on `web_url_read`. The search
mode wrapper also reads `mcp-searxng`'s result and error text (`URL:` lines, `SearXNG Error
(<status>)`, `Some search engines were unavailable`) to decide whether a call failed. A server
that words these differently still works in `private` mode, but the fallback modes stop
recognising its failures.

OpenCode reaches the same server at `http://mcp-searxng:3000/mcp`, set in
`opencode/opencode.json`.

### Search modes

`HERMES_WEB_SEARCH_MODE` (default `private`) decides who answers the two tools in Hermes:
SearXNG, Hermes' keyless ring (the anonymous tiers of Exa, Parallel, Firecrawl and Keenable),
or one with the other as fallback. The values are `private`, `private-first`, `keyless-first`
and `keyless`; an unknown value behaves as `private`. It applies to Hermes only. The overlay is
`compose/hermes/web_search_mode.py`. [Search modes](../searxng.md#search-modes) has the table
and what counts as a failure.

## Current implementation

| Service | Image or source | Role |
|---|---|---|
| `searxng` | `docker.io/searxng/searxng` (pinned by `SEARXNG_IMAGE`) | the HTTP search API, `core` profile, published on `127.0.0.1:${SEARXNG_PORT:-8080}` |
| `mcp-searxng` | `isokoliuk/mcp-searxng:2.7.0` (`MCP_SEARXNG_IMAGE`) | the MCP server, `core` profile, compose-internal |
| `search-gate` | `compose/search-gate/search_gate.py`, vendored from lead-gen | optional pacing proxy, `search-gate` profile, compose-internal |

SearXNG reads `searxng/settings.local.yml`, which setup writes from the tracked
`searxng/settings.yml` with `SEARXNG_SECRET`. The engines it enables (Bing, Google CSE and
Naver by default), suspension times and the optional Valkey limiter (`searxng-prod` profile)
are SearXNG configuration, not part of the interface. See [SearXNG](../searxng.md).

The search gate answers the same `GET /search` and `/healthz`, so it slots in by setting
`WEB_SEARCH_URL=http://search-gate:8080` with `SEARCH_GATE_ENABLED=1`. It sends each query to
one SearXNG engine, paces each engine, rests engines that block, and caches results in SQLite
(`search_gate_data` volume). It adds a `gate` field to the answer, ignores `categories` and
`engines`, and after `SEARCH_GATE_MAX_WAIT` seconds (default 15) answers with no results and a
`search-gate` entry in `unresponsive_engines`. `mcp-searxng`'s `SEARXNG_TIMEOUT_MS` defaults to
20000 so that busy answer arrives before the MCP call times out. `GET /status`, `GET /blocks`
and `POST /queue` are gate-only. With `SEARCH_GATE_FALLBACK_URL` set, the gate sends queries to
a remote SearXNG while local engines are blocked. See [Search gate](../searxng.md#search-gate).

Hermes' container sets `SEARXNG_URL` empty and the bootstraps disable its native `web` toolset,
so its built-in `web_search` and `web_extract` cannot bind to SearXNG. A cont-init patch
rewrites calls to those names into the MCP tools when the arguments map.

## Consumers

| Consumer | Seam | What it uses |
|---|---|---|
| Hermes (default, `api-server`, `browser` profiles) | MCP | `searxng_web_search`, `web_url_read` |
| OpenCode | MCP | the same server |
| GPT Researcher, `gptr-mcp` | HTTP | `SEARX_URL`, from `WEB_SEARCH_URL` |
| n8n | HTTP | `http://searxng:8080/search` in the starter workflows |

The search gate's cache holds every query asked and its results. Queries come from chat, so
treat the `search_gate_data` volume like Hermes session content.
