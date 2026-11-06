# Web search modes for Hermes

Status: approved design, 2026-11-06. Branch `feat/web-search-modes`.

## Goal

Let a stack operator choose, with one `.env` setting, whether Hermes searches and reads pages through
our own SearXNG, through Hermes' anonymous keyless ring (Exa, Parallel, Firecrawl, Keenable), or
through one with the other as fallback. Today's behaviour, SearXNG only, stays the default, so
Boundary and existing installs see no change.

Out of scope: OpenCode, GPT Researcher and n8n, which keep calling SearXNG directly. Keyed (paid)
vendor setups. Re-enabling Hermes' native `web` toolset.

## Background

Hermes v0.22.7 picks a native web backend in this order: an explicit `web.*backend` setting, then
autodetect from API keys, the Nous managed gateway, `SEARXNG_URL`, a Brave key and the `ddgs`
package, and last the keyless ring (`plugins/web/keyless_mcp.py`, `_KEYLESS_RING`). The ring starts
at a vendor chosen from the process's random session id, advances one vendor per request, and on a
rate limit or HTTP 401/402/403/5xx moves on to the next vendor. It serves extract as well as search.
Tavily is not in the ring.

This stack disables the native `web` toolset on every profile (`bootstrap-api-profile.sh`,
`agent.disabled_toolsets`), sets `SEARXNG_URL` empty in the `hermes` container, and gives the model
the SearXNG MCP tools `searxng_web_search` and `web_url_read`. Several overlays key on those tool
names: the circuit-breaker pause hint, the blocked-fetch hint, tool-loop notes and the eval trace.

## The setting

`HERMES_WEB_SEARCH_MODE` in `.env`, passed through the `hermes` service's environment allowlist in
`docker-compose.yml`. The overlay reads it on each call, so a change takes effect on
`docker compose up -d hermes` without re-running the bootstrap.

| Value | `searxng_web_search` | `web_url_read` |
|---|---|---|
| `private` (default, also when unset or empty) | SearXNG only | plain fetch only |
| `private-first` | SearXNG; on failure, the ring | plain fetch; on failure, ring extract |
| `keyless-first` | the ring; if every vendor fails, SearXNG | ring extract; on failure, plain fetch |
| `keyless` | the ring only | ring extract only |

Any other value is treated as `private`, and the overlay logs one warning per process naming the bad
value. `make doctor` reports an invalid value.

The native `web` toolset stays disabled in every mode, so the model always sees one search tool and
one page-read tool.

## Overlay

`compose/hermes/patch-web-search-mode.py` installs `compose/hermes/web_search_mode.py` into the
agent package and wraps `_make_tool_handler` in `tools/mcp_tool_handlers.py`. The patch follows the
existing overlay conventions: a marker comment, exact anchors, and a `SystemExit` naming the missing
anchor when upstream code moves. It runs from `05-patch-tool-calling.sh`, and the two files are
mounted read-only under `/bootstrap` like the other overlays.

The wrapper applies only when the server key is `searxng` and the tool is `searxng_web_search` or
`web_url_read`. Every other MCP tool gets upstream's handler unchanged. In `private` mode the wrapper
returns upstream's handler result untouched, byte for byte.

### Search

The ring call is `plugins.web.keyless_mcp.search_with_failover(name, query, limit)`. `name` is the
first ring vendor the cursor picks, so upstream's rotation and failover decide the order.
`limit` is the tool's `num_results` argument when given, else 10, about one SearXNG results page (mcp-searxng has no default of its own).
The ring ignores `pageno`, `time_range`, `language` and `safesearch`. When a ring-served result
had any of them set, its text says they were not applied.

A ring result is rendered in mcp-searxng's text format: one block per result with `Title:`,
`Description:` and `URL:` lines, blocks separated by blank lines. A first line names the source:
`Served by the keyless web tier (<vendor>).` When the call used `response_format: "json"`, the result is
`{"results": [{"title", "url", "content"}], "served_by": "<vendor>"}`. The rendered text goes through
upstream's normal handler JSON (`{"result": "..."}`), so the untrusted-result wrapper and later
overlays see the same shape as an mcp-searxng answer.

A SearXNG search counts as failed when any of these is true:

- the handler result is a tool error (this includes the circuit-breaker pause and an unreachable
  server);
- it holds no `URL:` result lines (text) or an empty `results` list (JSON) **and** names unavailable
  or unresponsive engines. This covers suspended engines and the search gate's busy answer.

A clean "no results" answer, with no engine failures, is not a failure and is returned as is. The
query really has no results, and sending it to the ring would only leak it.

A ring search counts as failed when `search_with_failover` returns `success: false`, which happens
only after it has walked every vendor or hit a non-failover error.

### Page reads

The ring call is `plugins.web.keyless_mcp.extract_with_failover(name, [url])`. Its content is sliced
with the tool's `startChar` and `maxLength` arguments the way mcp-searxng slices them: start at 0 and
return the rest of the page when they are absent. When
`section`, `paragraphRange` or `readHeadings` was given, the result says the ring returned the whole
page instead. The first line names the source, as for search.

A plain fetch counts as failed when `blocked_fetch_hint.hint_for` would fire on it (a 403, 429 or
503, an empty page that needs JavaScript, or a bot-check page) or when it is a tool error. Reusing
`hint_for` keeps one definition of "refused" for both overlays. A ring extract counts as failed when
its entry carries an `error`.

### Fallback results

When the fallback side succeeds, its result is returned with one added line naming why it was used,
for example `SearXNG was unavailable for this search (<first 200 chars of its error>).` The
blocked-fetch hint then does not fire, because the returned text no longer matches its checks.

When both sides fail, the result is the first side's failure with
` (fallback via <the other side> also failed: <its error>)` appended. A tool error stays a tool
error. That keeps the pause hint and the blocked-fetch hint working as the last resort: in
`private-first` the first side is SearXNG, whose text those overlays match.

In `keyless` mode a ring failure is returned as a tool error naming the vendors tried, with a line
saying SearXNG is not used in this mode.

### Guards

Before any URL goes to the ring:

- URLs that resolve to private or internal addresses are refused, using `tools.url_safety.async_is_safe_url`
  (the check `web_extract_tool` uses). The answer is the same refusal text `web_extract_tool` gives.
- URLs carrying secrets are refused: the overlay calls `_validate_extract_urls([url])` from
  `tools/web_tools_extract.py` and returns its blocked answer when it gives one.

Ring answers are never cached by the overlay, which matches upstream's rule for rescued results. The
SearXNG side's caching (mcp-searxng's cache, the search gate's cache) is unchanged.

The overlay honours upstream's `web.keyless_fallback: false`. `search_with_failover` and
`extract_with_failover` do not check it themselves, so the overlay calls
`plugins.web.keyless_mcp.keyless_enabled()` first and, when it is false, treats the ring side as failed
with the error `keyless web tier disabled (web.keyless_fallback: false)`.

## Documentation

- `docs/searxng.md`: a **Search modes** section after **Agent routing** with the table above, how to
  switch (`.env`, then `docker compose up -d hermes`), and that the ring modes send queries and URLs
  taken from chat to Exa, Parallel, Firecrawl and Keenable without an API key, from this machine's IP.
  It also says that the search gate's pacing and cache apply only to the SearXNG side.
- `docs/hermes.md`: an overlay table row for `patch-web-search-mode.py` with its test script, and the
  routing text updated to say the searxng tools may be served by the ring depending on the mode.
- `.env.example`: the variable, commented out, with the four values and the privacy note in one line.
- `CHANGELOG.md`: an entry under the next 0.3.x patch.

## Testing

`scripts/test-web-search-mode.py`, stdlib only, drives the Hermes image like the other overlay tests.
It applies the patch, then calls the wrapped handler with a stubbed upstream handler and stubbed
`search_with_failover` / `extract_with_failover`. No network. Cases:

- each mode for search and for page reads, checking which side was called and in what order;
- each failure trigger: tool error, breaker pause, empty with unresponsive engines, gate busy answer,
  clean empty (must not fall back), each `hint_for` refusal;
- both sides failing, in each fallback mode, with the pause-hint text preserved;
- text and JSON rendering, the source line, and the ignored-filters notes;
- the private-address and secret-URL guards, with the ring stub asserting it was never called;
- `private` mode returning upstream's result unchanged, and an invalid value behaving as `private`;
- the patch failing loudly when an anchor is missing.

After the offline tests pass, and only with Keith's go-ahead, one live check per ring mode: a single
query ("SearXNG documentation") and one read of a public page, confirming a ring vendor answered.

## Upkeep

Re-run `scripts/test-web-search-mode.py` on Hermes bumps. The overlay depends on
`_make_tool_handler`, `search_with_failover`, `extract_with_failover`, `keyless_enabled`,
`async_is_safe_url` and `_validate_extract_urls`. A rename of any of them fails the patch or the test, not a live search.
