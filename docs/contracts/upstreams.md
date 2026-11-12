# Upstreams

Every piece of the stack that runs someone else's code with local changes: the Hermes image
and its overlays, and the vendored MCP adapters. Each entry gives the pinned version, what is
changed, whether the change has gone upstream and the command that re-tests it after a bump.
Images the stack runs unchanged (LightRAG, SearXNG, `mcp-searxng`, Postgres, Neo4j and the rest)
are pinned in `docker-compose.yml` and not listed.

Before a patch to an upstream tool lands, it is filed upstream or the reason it isn't is
recorded. A patch over about 50 lines or touching more than one upstream file needs a decision
first: accept the limitation, contribute upstream, or build the feature in the stack. Several
Hermes overlays predate that rule and exceed it; they are marked below by their line counts.

## Vendored adapters

| Adapter | Upstream and pin | Local changes | Upstream | Re-test |
|---|---|---|---|---|
| `compose/actual-mcp` | [giorgiobrullo/actual-mcp](https://github.com/giorgiobrullo/actual-mcp) at `0c73b0d` (1.15.1) | Builds from npm `@actual-app` packages, runs as `node`, ends a session that can't sync instead of serving a stale copy, adds the per-budget launcher and `actual-admin.mjs` | session fix not filed yet | `make actual-mcp-test`, `scripts/test-actual-rows.sh` |
| `compose/gptr-mcp` | [assafelovic/gptr-mcp](https://github.com/assafelovic/gptr-mcp) at `6388477` | Trimmed to the server files, reformatted, three output changes | nothing to file | `scripts/test-gptr-mcp-registration.sh` |
| `compose/lightrag-mcp` | [shemhamforash23/lightrag-mcp](https://github.com/shemhamforash23/lightrag-mcp) `0.1.1` from PyPI (`39ce48e`), patched at build time | Sends the API key as `X-API-Key`, `get_documents` over `/documents/paginated`, `mcp` held below 2 | auth header: upstream issue #15 is open; `get_documents`: not filed | `scripts/test-lightrag-mcp-registration.sh` |
| `compose/search-gate` | the lead-gen project's `search_gate.py` at `85cb2b5` | Listed at the top of `search_gate.py` | not applicable: the stack's own code | `scripts/test-search-gate.py` |

Each adapter directory has a `VENDORED.md` with the full list of changes and what to re-check
on a bump: [actual-mcp](../../compose/actual-mcp/VENDORED.md),
[gptr-mcp](../../compose/gptr-mcp/VENDORED.md),
[lightrag-mcp](../../compose/lightrag-mcp/VENDORED.md).

## Hermes

`nousresearch/hermes-agent` at `v0.23.1` (`HERMES_AGENT_IMAGE_TAG`), unmodified as an image.
The stack changes it at container start: cont-init scripts in `compose/hermes/` run patch
scripts that edit files under `/opt/hermes`, copy modules beside them and install the `bwbroker`
plugin. Each patch anchors on text in the pinned release, so a bump means running the re-test
commands below and `make hermes-image-test`. An overlay whose anchor no longer matches logs a
warning and leaves Hermes unpatched; the container still starts.

The overlays fall into three classes:

- **upstream**: a general fix or feature Hermes could take. An issue draft exists; none is filed
  yet.
- **client API**: the wire shape is something clients rely on whatever agent runs behind the
  stack. The overlay stays as the Hermes implementation until a neutral client API replaces it.
- **adapter**: Hermes-specific behaviour or plumbing that stays in `compose/hermes/`.

Overlays with no re-test command fail only as a warning in the container log.

### Client-facing APIs

| Overlay | Lines | What it changes | Class | Upstream | Re-test |
|---|---:|---|---|---|---|
| `client-context` | 332 | `context` on session.create/resume/prompt.submit; refuses a mismatched `profile` on prompt.submit (4080-4083); `client_context` capability. | upstream | draft, not filed | `scripts/test-client-context.py` |
| `gateway-liveness` | 282 | `turn.heartbeat` event and `session.live_status` method; extends `gateway.capabilities`. | upstream | draft, not filed | `scripts/test-gateway-liveness.py` |
| `turn-speaker` | 305 | `speaker {id, display_name}` on prompt.submit; stamped on assistant rows and in the `api_content` sidecar (4084-4086); `turn_speaker` capability. | client API | becomes a client API method | `scripts/test-turn-speaker.py`, `scripts/test-hosted-rooms.py` |
| `history-budget` | 1074 | `history_budget` on session.create/resume windows the prompt without archiving rows; `session.context_breakdown` fields; error 4028. | client API | becomes a client API method | `scripts/test-history-budget.py` |
| `mcp-wake` | 369 | `POST /v1/mcp/servers/{name}/reconnect` wakes a parked MCP server in every gateway process; `mcp_servers.<name>.parked_retry_interval`; parks a server once when another profile just parked it. | client API | becomes a client API method | `scripts/test-mcp-wake.py`, `scripts/test-mcp-parked-retry.sh` |
| `dashboard-handoff` | 251 | `GET /auth/handoff?code=` one-time sign-in link, HMAC code, off until `HERMES_DASHBOARD_HANDOFF_SECRET` is set; `make dashboard-open`. | client API | becomes a client API method | `scripts/test-dashboard-handoff.py` |
| `mirror-exclude` | 116 | `mirror_exclude` glob list on `profiles.create` removes keys from the mirrored `.env`; result lists `mirrored.env_excluded`; fails closed. | upstream | draft, not filed | `scripts/test-mirror-exclude.py` |
| `mcp-trust-details` | 160 | Structured `mcp: {server, tool, arguments, tool_call_id}` on trust-gate approval entries via a new `details` keyword on `request_elicitation_consent`. | upstream | draft, not filed | `scripts/test-mcp-trust-details.py` |
| `kanban-guards` | 313 | Refuses unbuildable worktree/dir workspaces at filing; gives up at once (`retryable: false`) on final workspace errors; creator-comment wrap. | adapter | stays in the adapter | `scripts/test-kanban-guards.py` |
| `kanban-edit` | 374 | Workspace fields on the dashboard's `PATCH /tasks/{id}`; `kanban_update` and `kanban_archive` tools. | adapter | stays in the adapter | `scripts/test-kanban-edit.py` |
| `kanban-wake` | 211 | Re-keys `kanban_notify_subs` through the compression lineage so `tui` subscriptions keep receiving events; repairable `gave_up` text. | adapter | stays in the adapter | `scripts/test-kanban-wake.py` |
| `dashboard-board` | 137 | Kanban page honours `?task=<id>&board=<slug>`; `?embed=1` hides sidebar and headers. | adapter | stays in the adapter | `scripts/test-dashboard-handoff.py` (covers the same container start; no dedicated board test) |
| `projects-library` | 648 | Merges the `/opt/projects` folder library into `projects.list` / `projects.tree`; WebUI side patch. | adapter | stays in the adapter | `python3 scripts/test-project-sources.py` (`make project-sources-test`) |

### Credential handling

| Overlay | Lines | What it changes | Class | Upstream | Re-test |
|---|---:|---|---|---|---|
| `bitwarden-broker-fill` | 373 | Routes `browser_vault` fills and `enter_code` of `bwbroker:` handles to the broker's `POST /fill`; adds a `bitwarden` entry to `browser_vault list`. | adapter | stays in the adapter | `scripts/test-bitwarden-fill.py`, `make bitwarden-test` |
| `digest-redaction` | 137 | `register_vault_redaction_digest` and digest scrub in `agent/redact.py`, so values the gateway never held are still scrubbed from output. | adapter | stays in the adapter | `scripts/test-digest-redaction.py` |
| `bwbroker plugin + install` | 289 | Bundled `bwbroker` login-backend plugin copied into `/opt/hermes/plugins` on every start. | adapter | stays in the adapter | `sh scripts/test-bitwarden-registration.sh --image`, `make bitwarden-test` |
| `vault-create-login` | 712 | `browser_vault` action `create_login` (generated password the model never sees) and the `vault.create_login` server request. | client API | becomes a client API method | `scripts/test-vault-create-login.py --image` |
| `vault-capture-secret` | 624 | `browser_vault` action `capture_secret`, the `vault.capture_secret` request and the `vault.take` RPC. | client API | becomes a client API method | `scripts/test-vault-capture-secret.py --image` |
| `fabricated-result` | 343 | Continues a stop turn that claims a tool result without calling a tool, or asks the user for a password in chat; hides the nudge from persistence and compaction. | adapter | stays in the adapter | `scripts/test-fabricated-result.py` |

### Local-model and agent-loop behaviour

| Overlay | Lines | What it changes | Class | Upstream | Re-test |
|---|---:|---|---|---|---|
| `text-tool-call-recovery` | 329 | Parses Python- or XML-style tool calls in assistant text into structured `tool_calls` before dispatch. | adapter | stays in the adapter | none (apply-time anchor only); `scripts/test-tool-eval-trace.py` exercises the call site |
| `tool-call-flat-args` | 190 | Normalises weak-model `tool_call` shapes: flat siblings, nested name, short MCP aliases, stringified `calls`. | adapter | stays in the adapter | none |
| `intent-ack-continuation` | 146 | Lets `looks_like_codex_intermediate_ack` continue a turn past 1,200 characters (up to 4,000) when the reply opens with an ack and `require_workspace` is off. Before v0.23.0 it also stops earlier turns' tool messages from blocking it; v0.23.0 does that upstream (#69778). | upstream | draft, not filed | `scripts/test-intent-ack.py` |
| `tool-search-visible` | 126 | `tool_search` also returns already-visible core tools, with a call-directly hint. | upstream | draft, not filed | none |
| `tool-search-always-include` | 95 | Honors `tools.tool_search.always_include` so stack MCP pins (SearXNG, LightRAG, OpenCode) stay eager. | upstream | draft, not filed | `docker compose exec hermes grep -F 'assistant-stack: honor always_include' /opt/hermes/tools/tool_search.py` (docs/hermes.md) |
| `tool-loop-notes` | 72 | Queues loop warnings and halt notices on `plugin_live_notes` so they land outside `<untrusted_tool_result>`. | upstream | draft, not filed | `scripts/test-tool-loop-notes.py` |
| `tool-loop-replay` | 132 | Counts stale replays per (tool, arguments) per turn; notes the second, halts the third (`stale_replay_halt`). | upstream | draft, not filed | `scripts/test-tool-loop-replay.py` |
| `guardrail-halt-stream` | 62 | Streams the guardrail stop note through `_deliver_to_stream_callbacks`, so dashboard gateway clients get it as `message.delta`, not only in `message.complete`. | upstream | draft, not filed | `scripts/test-guardrail-halt-stream.py` |
| `compaction-tool-facts` | 248 | Gives the compaction summarizer the original tool bodies (4,000 chars each, 60,000 total) instead of prune stubs; adds a `## Findings` section. | upstream | draft, not filed | `scripts/test-compaction-tool-facts.py` |
| `tool-eval-trace` | 498 | JSONL turn traces for the tool-calling eval; records web rewrites. | adapter | stays in the adapter | `scripts/test-tool-eval-trace.py` |
| `block-native-web-tools` | 149 | Rewrites hallucinated `web_search` / `web_extract` to the SearXNG MCP tools, or returns a terminal error. | adapter | stays in the adapter | none; `scripts/test-tool-eval-trace.py` logs the rewrite path |
| `send-message tools` | 343 | Synthesises a Signal platform config from `SIGNAL_*` with the adapter disabled; registers `send_message` as an agent-callable tool and keeps it in the schema. | adapter | stays in the adapter | `scripts/test-send-message-tool.py` |
| `cron-scheduler-delivery` | 307 | (1) Rejects `api_server` cron origins and falls back to home channels. (2) Synthesises Signal config for cron delivery. | adapter | stays in the adapter | `scripts/test-cron-delivery-errors.py` |

### Search and browser hints

| Overlay | Lines | What it changes | Class | Upstream | Re-test |
|---|---:|---|---|---|---|
| `web-search-mode` | 428 | `HERMES_WEB_SEARCH_MODE` serves `searxng_web_search` and `web_url_read` from SearXNG, Hermes' keyless ring, or both. | adapter | stays in the adapter | `scripts/test-web-search-mode.py --image` |
| `blocked-fetch-hint` | 214 | On 403/429/503, empty JS-only page or bot-check from searxng `web_url_read`, queues a note to open the URL with `browser_exec`. | adapter | stays in the adapter | `scripts/test-blocked-fetch-hint.py` |
| `searxng-pause-browser` | 68 | Adds how to search with `browser_exec` to the searxng circuit-breaker pause error. | adapter | stays in the adapter | `scripts/test-searxng-pause-browser.py` |
| `browser-unresolvable-host` | 71 | When a blocked browser URL's hostname has no DNS answer, the error names the host and says the address is probably wrong. | upstream | draft, not filed | `scripts/test-browser-unresolvable-host.py` |
| `browser-exec-hint` | 281 | Hints for bad helper imports, empty output and `.value` assignment in `js()`; prints a trailing bare expression; `save_login` next step. | adapter | stays in the adapter | `python3 scripts/test-browser-exec-hints.py` |
| `buzz-localhost-proxy` | 166 | Forwards container `localhost:3000` to `host.docker.internal` so Buzz keeps the community host Desktop uses. | adapter | stays in the adapter | `scripts/test-buzz-sync-gate.sh` (gate only, not the proxy) |

### Platform plumbing

| Overlay | Lines | What it changes | Class | Upstream | Re-test |
|---|---:|---|---|---|---|
| `control-socket-tmp` | 115 | Binds each gateway's control socket under `/tmp` (`HERMES_CONTROL_SOCKET_IN_TMP=1`) with a pointer file in `$HERMES_HOME`. | upstream | draft, not filed | `scripts/test-hermes-topology.py` |
| `gateway-compat-proxy` | 174 | Keeps ports 8643/8644 answering under the multiplexed gateway by forwarding to `/p/<profile>/`; bridges SSE and WebSocket. | adapter | stays in the adapter | `scripts/test-gateway-multiplex.py` |
| `019-pin-profile-gateways` | 158 | Chooses standalone or multiplex topology; writes `gateway.standalone: true` and `sessions.auto_prune: false` where unset. | adapter | stays in the adapter | `scripts/test-hermes-topology.py`, `scripts/test-gateway-multiplex.py` |
| `005-remap-hermes-uid` | 63 | Remaps the `hermes` user to `HERMES_UID`/`HERMES_GID` before the image's `01-hermes-setup`, so `usermod` does not chown `/opt/data`. | adapter | stays in the adapter | none (`docker compose up` on a fresh container; failure shows as exit 12 in 01-hermes-setup) |
| `007-profile-ownership` | 71 | Removes orphaned sockets under `profiles/` and chowns the few files not owned by hermes, so the image's `chown -R` walk is skipped. | adapter | stays in the adapter | `scripts/test-profile-ownership.sh` |
| `09-install-worker-key` | 35 | Installs the SSH key for the `hermes-worker` terminal backend and seeds `known_hosts`. | adapter | stays in the adapter | `scripts/test-terminal-backend-compose.py` |
| `11-warmup-uv-cache` | 25 | Makes the shared `UV_CACHE_DIR` volume writable by the hermes user. | adapter | stays in the adapter | none |

`compose/hermes/bootstrap-api-profile.sh`, which writes each profile's configuration from the
add-on plans, is the Hermes adapter rather than an overlay; see [agent](agent.md).
