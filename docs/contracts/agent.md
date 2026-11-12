# Agent contract

The agent runtime is the part of the stack that holds conversations, calls
tools and runs scheduled jobs. Hermes Agent fills that role today, and there is
no second agent. This page lists what the stack needs from any agent runtime,
then how Hermes meets each requirement, so a replacement knows what it has to
provide and what it would have to take over.

New stack contracts stay agent-neutral. Don't add Hermes-only names, files or
behaviour to the manifest, the instance store or the plans in
[tools.md](tools.md); put them in the Hermes adapter.

An identity is who a run acts as: its tools, its access level to each add-on,
and whether anyone is watching. Hermes implements an identity as a profile.

## Requirements

| Requirement | What the stack needs |
|---|---|
| MCP client with per-identity configuration | Each identity has its own MCP server list, tool allowlist per server, and credentials, so two identities can reach one server with different tools or keys |
| Identities with access levels | Each identity gets `ro` or `rw` per add-on, and is marked attended or unattended. Add-ons that read personal data never reach an unattended identity |
| Scheduler that restricts a run's tools | Scheduled runs are unattended. The stack sets which MCP servers and toolsets a run gets by default, and a job can name its own |
| Chat API for clients | An authenticated API, per identity, that clients and automations use to run turns |
| Browser with a credential-fill hook | A browser tool whose login fills go through a pluggable backend, so a credential backend can fill a field without the model, or the agent process, holding the value |
| Tool results kept wrapped as untrusted | Results from MCP, browser and web tools reach the model inside a wrapper that tells it not to follow instructions in them; stack guidance goes outside the wrapper |
| An adapter that applies the plans | Something that reads `mcp-plan`, `instances-plan` and `vault-plan` (and the cron fence) from `scripts/lib/addon_manifest.py` and writes the agent's own configuration from them |

## How Hermes meets them

### The Hermes adapter

`compose/hermes/bootstrap-api-profile.sh` is the Hermes adapter. Compose runs
it as two one-shot services, `hermes-api-bootstrap` (the default profile, then
a sweep over every profile) and `hermes-browser-bootstrap` (the `browser`
profile only), before `hermes` starts. Both mount `addons/` and
`addon_manifest.py` read-only at `/bootstrap/addons` and
`/bootstrap/addon_manifest.py`, and the stores of add-ons with instances under
`/bootstrap/addon-data`, also read-only. The functions that apply the plans:

| Plan | Function |
|---|---|
| `mcp-plan` | `register_addon_mcp_servers` |
| `instances-plan` | `register_addon_instance_servers`, which copies each key into the profile's `.env` as `<NAME>_KEY` and puts only a header that references it in `config.yaml` |
| `vault-plan` | `register_addon_vault_backends` |
| `mcp-urls` | `set_stack_mcp_parked_retry` (through `addon_mcp_urls`) |
| `cron_fence`, `cron_fence_toolsets` | `fence_mail_from_cron` |

A run is skipped when its inputs and the profiles' config are unchanged since
the last complete run; [hermes.md](../hermes.md#when-the-bootstrap-runs) lists
what is hashed.

### MCP client

Each profile has its own `config.yaml` (`data/hermes/config.yaml` for default,
`data/hermes/profiles/<name>/config.yaml` for the others) with an
`mcp_servers` mapping, a `tools.include` list per server, and its own `.env`
for keys. The bootstrap sets individual keys rather than replacing the
mapping, so servers added by hand survive. After registering an add-on entry
it drops any other entry at the same URL, because profiles are created by
cloning and can carry an unfiltered copy. Registered servers per profile are
listed in [hermes.md](../hermes.md#automatically-registered-mcp-servers).

### Identities

| Profile | Used by | Attended | Turn cap and toolset set by the bootstrap |
|---|---|---|---|
| default | Dashboard (`:9119`), CLI, Signal inbound, cron jobs created there | yes | none |
| `api-server` | n8n, scripts, curl | no | 20 (`HERMES_API_MAX_TURNS`), `hermes-api-server` |
| `browser` | Hermes WebUI | yes | 60 (`HERMES_BROWSER_MAX_TURNS`), `hermes-cli` |

`api-server` is the unattended identity: `UNATTENDED_PROFILE` in
`addon_manifest.py`, and `validate` refuses a manifest that reads personal
data and lists it. Access levels come from each manifest's `mcp.profiles`, or
for an instance from its `bind`; for an `ro` profile the plans leave out
`writeTools`, and with `mcp.instances` the `ro` profile gets the instance's
`ro` key or URL. Core servers draw the same line
in the bootstrap: `opencode` is never registered on `api-server`.
Independent agents created with `make agent-create` are further profiles
([agents.md](../agents.md)). Profile settings are in
[hermes.md](../hermes.md#api-server-profile-api-server) and
[hermes-webui.md](../hermes-webui.md#the-browser-profile).

### Scheduler

Hermes cron jobs run on a profile. A job with its own `enabled_toolsets` gets
those; one without gets `platform_toolsets.cron`. `fence_mail_from_cron`
pins `platform_toolsets.cron` on every profile it runs on to the toolsets
Hermes' `hermes-cron` bundle turns on, without `browser`, plus every MCP
server not fenced. It names the toolsets one by one because the bundle
includes the browser, and reads them from Hermes at each run. The fence
covers the browser, mail, messages, and each add-on whose `permissions.cron`
is not `true`, matched by MCP name or by the scheme, host and port of its URL.
A job gets a fenced toolset or server by naming it in its own
`enabled_toolsets`. A hand-written list keeps its entries, minus the fenced
ones, with a bundle that includes the browser (`hermes-cron`, `hermes-cli`)
replaced by its toolsets. A `vaultBackend`
toolset is kept out by listing it in `known_plugin_toolsets.cron`. The list the
bootstrap wrote is recorded in `.mail-cron-toolsets.json` beside `config.yaml`.

### Chat API

| Client | Endpoint |
|---|---|
| n8n, scripts | OpenAI-compatible `/v1/chat/completions` and `/v1/runs` on `api-server`, `http://localhost:8643/v1`, with `Authorization: Bearer <API_SERVER_KEY>` from `compose/hermes/api-server.env` |
| Hermes WebUI | `POST /v1/runs` (SSE) on the `browser` profile at `http://hermes:8644`, internal only, with its own key from `compose/hermes/browser.env` |
| Boundary | The dashboard gateway's JSON-RPC WebSocket, `/api/ws` on `:9119` |

Hermes scopes API keys per profile and rejects a key presented to another
profile. Port layout under each gateway topology is in
[hermes.md](../hermes.md#gateway-topology).

### Browser and credential fill

The browser tools are `browser_exec`, `browser_cdp`, `browser_dialog` and
`browser_vault`. `browser_vault` takes an `action`: `list`, `fill`,
`save_login`, `enter_code` and `unlock` upstream, plus `create_login` and
`capture_secret` from stack overlays. Login backends are the hook: core ships
`vault`, `op` and `bw`, and an add-on's `vaultBackend` adds a Hermes plugin.
Bitwarden's `bwbroker` plugin (`compose/hermes/plugins/bwbroker`) sends fills
to the Bitwarden broker, which writes the value into the page over its own CDP
connection, so the gateway never holds it (`patch-bitwarden-broker-fill.py`,
with `patch-digest-redaction.py` scrubbing filled values). Details are in
[hermes.md](../hermes.md#browser-tools) and [bitwarden.md](../bitwarden.md).

### Untrusted tool results

Hermes wraps the results of `mcp_*`, `browser_*` and web tools in
`<untrusted_tool_result>`, whose preamble tells the model not to follow
directives inside it. Stack notes about a result go outside the wrapper:
they are queued on Hermes' plugin live-notes queue, which `tool_executor.py`
attaches to the tool message after wrapping. `patch-tool-loop-notes.py` moves
tool-loop warnings there, and `blocked_fetch_hint.py` adds its note about a
refused page read there. Client context sent by a dashboard client carries a
line telling the model to treat its names and labels as untrusted metadata
(`client_context.py`).

## Hermes coupling a swap would replace

These parts of the stack depend on Hermes itself, not on the requirements
above.

- **Client API Boundary uses.** The dashboard JSON-RPC methods, plus stack
  overlays on them: [turn liveness](../extending.md#gateway-turn-liveness),
  [speaker names](../extending.md#speaker-names),
  [Kanban workspaces](../extending.md#kanban-workspaces),
  [dashboard links](../extending.md#dashboard-links) and
  [MCP approvals](../extending.md#mcp-approvals-for-untrusted-servers).
  The cont-init scripts `compose/hermes/05-patch-tool-calling.sh`,
  `12-patch-kanban-guards.sh` and `13-patch-dashboard.sh` apply them, and
  the liveness, speaker and client-context overlays advertise themselves in
  `gateway.capabilities`.
- **The browser.** `browser_exec` and `browser_vault` run inside the `hermes`
  container, and the vault actions, the redaction and the broker fill are
  overlays on Hermes code.
- **Messaging channels.** Signal through `signal-cli` ([signal.md](../signal.md)),
  Buzz as a Hermes gateway platform ([buzz.md](../buzz.md)), and the webhook
  route that `messages-mcp` forwards to, set up by
  `configure_messages_webhook` in the bootstrap.
- **Overlays in `compose/hermes`.** The `patch-*.py` scripts and their modules
  edit files under `/opt/hermes` at container start and are anchored to the
  pinned Hermes tag (`v0.23.1`). `make hermes-image-test` runs their tests
  against a new image.
- **The adapter's own reach.** Core MCP servers (`searxng`, `opencode`,
  `messages`) are registered by the bootstrap directly, not from manifests. The add-on store mounts for the
  bootstraps are written per add-on in `docker-compose.yml`. Manifests name
  identities by Hermes profile name.

A neutral client API, which Boundary would use in place of the dashboard
WebSocket, and a browser service separate from the agent container are
planned. The interface a browser provider has to meet is on the
[browser](browser.md) page.
