# Changelog

Each release lists what a downstream install needs to know before moving its
pin: new services, required `.env` keys, new host requirements and upgrade
steps. Pin a tag, not a branch. See [docs/releasing.md](docs/releasing.md) for
how releases are cut and [docs/extending.md](docs/extending.md) for running
the stack under another app.

## Unreleased

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. It adds
   `VIKUNJA_MCP_ENABLED=0` and generates `VIKUNJA_SECRET`. Nothing new starts
   until you opt in with `--todo`.
2. `docker compose up -d`. The `hermes` system prompt (one sentence on where
   living todos live), both bootstrap services' environments, four new
   overlay mounts and `HERMES_TURN_HEARTBEAT_S` change, so compose recreates
   `hermes`.
3. Optional, for todo lists: `./scripts/setup.sh --todo`, `make up`,
   `make todo-init`, re-run `hermes-api-bootstrap` and
   `hermes-browser-bootstrap`, restart `hermes`. See [docs/todo.md](docs/todo.md).
4. Dashboard clients that send `profile` on `prompt.submit`: make sure it
   names the session's own profile, or drop it. A different profile used to
   be ignored silently and is now an error (see Changes).

### New services

- `vikunja` (new **`todo`** profile, off by default): [Vikunja](https://vikunja.io)
  2.7.0 (AGPL-3.0) web app on `127.0.0.1:3456` with its built-in MCP server.
  It joins `default` and the internal `db` network and keeps its data in
  schema `vikunja` of the shared Postgres. `todo` is added to
  `x-db-profiles`, so it starts `postgres-db`, `db-provision`, and the SQL MCP
  sidecars too. The SQL MCP is still registered only with `SQL_MCP_ENABLED=1`.

### Required `.env` keys

Only with `todo` enabled.

- `VIKUNJA_SECRET`: `setup.sh` generates it for every install. If it is empty,
  Vikunja picks a new one each start and everyone is logged out.
- `VIKUNJA_MCP_TOKEN_RW` and `VIKUNJA_MCP_TOKEN_RO`: written by
  `make todo-init`. Without them the bootstrap logs a warning and registers
  nothing.

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `VIKUNJA_MCP_ENABLED` | `0` | `1` registers `mcp_servers.vikunja` on Hermes. `setup.sh --todo` sets it |
| `VIKUNJA_PORT` | `3456` | Loopback host port for the web UI |
| `VIKUNJA_PUBLIC_URL` | `http://localhost:${VIKUNJA_PORT}/` | Base URL in Vikunja's links and MCP page |
| `VIKUNJA_ENABLE_REGISTRATION` | `false` | Allow sign-up from the web UI |
| `VIKUNJA_FILES_DIR` | `./data/vikunja/files` | Attachments bind mount |
| `VIKUNJA_IMAGE` | `vikunja/vikunja:2.7.0` | Vikunja image (MCP needs 2.7.0+) |
| `VIKUNJA_MCP_URL` | `http://vikunja:3456/api/v2/mcp` | Endpoint the bootstrap registers |
| `VIKUNJA_MCP_TIMEOUT` / `VIKUNJA_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `vikunja` |

### Changes

- **Hermes bootstrap**: registers `mcp_servers.vikunja` with an
  `Authorization: Bearer ${VIKUNJA_MCP_TOKEN}` header. The read-only token goes
  to `api-server`, the read/write token to default and `browser`, each into
  that profile's `.env`. With the profile off or the token missing, it removes
  the entry and the token.
- **`living-todos` skill**: uses the Vikunja tools when they are listed, and
  the markdown file otherwise. The Hermes system prompt says the same.
- **`make todo-init`** (`scripts/todo.sh`): creates the Vikunja account from
  the CLI and mints both tokens. Re-running it rotates them.
- **`make ensure-local` / `make up`**: with `todo` in `COMPOSE_PROFILES`,
  writes `compose/postgres/services/vikunja.env` if it is missing.
- **`make doctor`**: a todo section (profile, secret, database login, tokens,
  per-profile registration, `api-server` not holding the read/write token,
  container and HTTP health) and a port check for 3456.
- **Hermes gateway (overlay)**: while a turn runs, the gateway sends a
  `turn.heartbeat` event every 30 s (`HERMES_TURN_HEARTBEAT_S`, `0` for off)
  with `status`, `turn_started_at` and `idle_s`, and never after
  `message.complete`. New `session.live_status` returns one session's
  `status`, `running`, `turn_started_at` and `idle_s` by `session_id` or
  `session_key`, without a database read; a session that is not live answers
  `idle`. `gateway.capabilities` advertises both. Clients that ignore them see
  no change. See [docs/hermes.md](docs/hermes.md#turn-liveness).
- **Hermes gateway (overlay)**: `session.create`, `session.resume` and
  `prompt.submit` take an optional `context` string. Session context is added
  to the system prompt. Turn context is added to that turn's model input. The
  user's message and `session.history` stay as typed. Errors are `4080`
  (invalid), `4082` (a turn is running) and `4083` (isolated compute-host
  turns). `gateway.capabilities` advertises `client_context` and
  `prompt_profile_check`. Clients that leave the field out see no change. See
  [docs/hermes.md](docs/hermes.md#client-context).
- **Hermes gateway (overlay)**: `prompt.submit` with a `profile` that differs
  from the session's profile now fails with `4081` and
  `data.session_profile`. It used to be accepted and ignored: the turn ran on
  the session's profile. A matching or omitted `profile` behaves as before.
- **Docs**: [docs/hermes.md](docs/hermes.md#platform-hints) documents
  `agent.platform_hints.<source>` for clients whose `source` has no built-in
  hint, and corrects the claim that `profile` works per turn.

## v0.3.2 — 2026-10-11

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. It adds
   `SQL_MCP_ENABLED=0` and generates the three `DB_*_PASSWORD` keys. Nothing
   new starts until you opt in with `--db`.
2. `docker compose up -d`. The `hermes` system prompt, mounts, and networks
   (it joins a new internal `db` network beside `default`, even with `db`
   off) and the `opencode-mcp` environment change, so compose recreates both.
   Compose creates the `hermes_uv_cache` volume and the `db` network. An
   override that sets `networks:` on `hermes` must keep `default` and `db`.
3. Optional: delete the old per-profile uv caches, which nothing reads any
   more (several hundred MB each):
   `rm -rf data/hermes/home/.cache/uv data/hermes/profiles/*/home/.cache/uv`.
4. Optional, to use the shared database: `./scripts/setup.sh --db`, `make up`,
   re-run `hermes-api-bootstrap` and `hermes-browser-bootstrap`, restart
   `hermes`. See [docs/database.md](docs/database.md).

### New services

All in the new **`db`** profile, off by default. They are separate from n8n's
`postgres` (`automation`), which is unchanged.

- `postgres-db`: shared Postgres 18 with one database and a schema per owner.
  No host port.
- `db-provision`: one-shot role, schema, and grant reconciliation on every
  `up`.
- `sql-mcp-ro` / `sql-mcp-rw`: [postgres-mcp](https://github.com/crystaldba/postgres-mcp)
  0.3.0 as `agent_ro` (restricted mode) and `agent_rw`. Hermes registers ro on
  `api-server` and rw on the default and `browser` profiles.

### Required `.env` keys

Only with `db` enabled. `setup.sh` fills all three for every install.

- `DB_ADMIN_PASSWORD`, `DB_AGENT_RO_PASSWORD`, `DB_AGENT_RW_PASSWORD`. If
  they are empty, `postgres-db` refuses to initialise and `db-provision`
  exits non-zero. Compose does not fail on them, because it parses the
  service with the profile off.

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `CODING_ROOTS_APP_MANAGED` | empty | `1`: the `needs_access` hint says the app will ask for access instead of naming `make coding-root-add` |
| `CONTEXT_ROOTS_APP_MANAGED` | empty | `1`: the Hermes prompt says the app will ask for a missing context root instead of naming `make context-root-add` |
| `HERMES_UV_CACHE_DIR` | `hermes_uv_cache` | Host path to use for the shared uv cache instead of the named volume |
| `CODING_PATH_ALIASES` | empty | `;`-separated `hermes-path=host-path` pairs for project folders that are also host folders |
| `SQL_MCP_ENABLED` | `0` | `1` registers the shared-database SQL MCP on Hermes. `setup.sh --db` sets it |
| `DB_NAME` | `assistant` | Shared database name (`db` profile) |
| `DB_DATA_DIR` | `./data/db` | Shared Postgres bind mount |
| `DB_BACKUP_DIR` | `./data/db-backups` | `make db-backup` output |
| `DB_IMAGE` | `postgres:18-alpine` | Shared Postgres image |
| `SQL_MCP_IMAGE` | `crystaldba/postgres-mcp:0.3.0` | SQL MCP sidecar image |
| `SQL_MCP_RO_URL` / `SQL_MCP_RW_URL` | `http://sql-mcp-{ro,rw}:8000/sse` | Endpoints the bootstrap registers |
| `SQL_MCP_TIMEOUT` / `SQL_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `sql` |

### Changes

- **opencode-mcp**: `coding_start_task` translates a path listed in
  `CODING_PATH_ALIASES` (or a path under it) to its host path before
  checking the roots, and `coding_list_roots` returns the pairs as `aliases`.
  An alias does not grant access. See
  [docs/opencode.md](docs/opencode.md#project-folder-aliases).
- **opencode-mcp**: the default `needs_access` hint names `hermes-worker`
  (or `hermes` on the local backend) as the container to recreate, instead
  of `hermes`.
- **Hermes**: the system prompt no longer repeats the `make coding-root-add`
  steps. It tells the model to follow the `needs_access` hint, so the hint is
  the one source for that text. It lists the alias pairs when
  `CODING_PATH_ALIASES` is set.
- **Hermes**: the `delegate-coding` skill (1.3.0) follows the `needs_access`
  hint. When the hint says the app will ask, the model names the folder in
  one sentence and ends the turn. It uses the alias pairs for "this project"
  instead of matching folder names against the roots.
- **Hermes**: `POST /v1/mcp/servers/{name}/reconnect` on the API server
  (8643, API key) wakes a parked MCP server in every profile within about a
  second. It returns `202`, or `404` when no profile configures the name. It
  never restarts a connected server. See
  [docs/extending.md](docs/extending.md#mcp-servers-your-app-hosts).
- **Hermes**: `mcp_servers.<name>.parked_retry_interval` (seconds, floor 10)
  sets how often a parked server is tried again. Unset keeps Hermes' 300 s.
  An app that hosts its own MCP server should set it, and
  `keepalive_interval`, to about 15.
- **Hermes**: every profile shares one uv cache on the new `hermes_uv_cache`
  volume (`UV_CACHE_DIR=/opt/uv-cache`), and a start-up script warms it with
  browser-use in the background. `browser_exec` ran the browser-use CLI with
  `uvx` from a cache under each profile's home on `data/hermes`, which took
  about a minute per call on Docker Desktop. It now takes about a second.
- **model-use**: writes every Hermes profile in one container run instead
  of 15 `hermes config set` calls, so a switch takes a couple of seconds.
  It now updates every profile under `data/hermes/profiles/`, not only the
  default, `browser` and `api-server`, so profiles from `make agent-create`
  follow the stack model. A profile whose `model.base_url` is not
  `llm-proxy` keeps its model.
- **model-sync**: `make model-sync` (`scripts/model-use.sh --sync`) copies
  `LLM_MODEL` from `.env` to Hermes and OpenCode, for when `.env` was edited
  by hand.
- **Database**: grants enforce the boundaries. `agent_ro` reads every schema.
  `agent_rw` also creates and writes in schema `agent`. Each integration's
  `svc_<slug>` role (`make db-service-add NAME=<slug>`) owns and migrates only
  its own schema. `public.data_catalog` lists every schema, table, and column
  with its `COMMENT ON` text.
- **Hermes**: new `database` skill. `mcp_servers.sql` exposes five tools
  (`list_schemas`, `list_objects`, `get_object_details`, `execute_sql`,
  `explain_query`). With `SQL_MCP_ENABLED=0` the bootstrap removes earlier
  `sql` registrations.
- **Make**: `db-backup` (`pg_dump -Fc`, mode 600), `db-service-add`,
  `db-psql`. `make doctor` gains a shared-database section, including a probe
  that `agent_ro` cannot write.
- **Compose**: an `x-db-profiles` anchor lists the profiles that start the
  database. An integration appends its own profile there.

## v0.3.1 — 2026-10-07

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. No
   `.env` changes.
2. `docker compose up -d`. The `hermes` service gains three read-only
   `/bootstrap` mounts, so compose recreates it and cont-init applies the new
   overlays.

### Changes

- **Hermes**: a reply that is only reasoning and contains a malformed tool
  call (often just `</parameter></function></tool_call>`) is re-prompted
  instead of shown as the answer. Hermes v2026.9 started showing such
  reasoning as the final reply, and v2026.8 re-prompted. A model that keeps
  doing it is re-prompted at most twice per turn, then Hermes behaves as
  before. Reasoning-only replies without tool-call tags are unchanged. See
  `compose/hermes/patch-reasoning-only-tool-markup.py`.
- **Hermes**: a `browser_exec` result gains a `hint` field when the code
  imported the browser helpers from a module that does not exist, or ran and
  printed nothing. Results that print output are unchanged.
- **Hermes**: the `browser-login` skill (1.2.0) covers the browser-use
  backend, where `browser_exec` and the vault tools are the only browser
  tools, with working sign-in code. It lists the form's inputs before typing,
  and says what to do when a login was saved from a page without the form.
- **Hermes**: when `browser_vault_save_login` stores a login but the page has
  no form to fill, its `next` now says to open the sign-in form and call
  `browser_vault_fill` with the returned handle.
- **Hermes**: a reply that asks the user for a password in chat is re-prompted
  toward the vault tools instead of shown (at most twice per turn, shared
  with the fabricated-result check).
- **Gateway contract**: `session.create` and `session.resume` declare
  `history_budget`, and `message.complete` / `session.context_breakdown`
  declare `history_budget`, `history_tokens`, `history_first_row_id` and
  `history_summarised`. Hermes v2026.9 refused `history_budget` with `4000`
  and logged a contract warning for the reply fields.

### Behaviour changes

- **`history_budget` takes effect.** On v2026.9.14 the gateway refused it, so
  a client that retries without it (Boundary does) ran every session with no
  budget. Sessions that ask for a budget now get a windowed prompt and the
  `history_*` horizon fields.
- **`browser_exec` prints a trailing expression.** Code that ends in a bare
  expression (`js(...)`, `page_info()`) returns its value when it is not
  `None`, as a Python prompt does. Code ending in `print(...)` or a statement
  is unchanged.

## v0.3.0 — 2026-10-06

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`.
2. `docker compose up -d --build llm-proxy`. The proxy code is baked into the
   locally built `${IMAGE_PREFIX}-llm-proxy:local` image, so a plain `up -d`
   keeps the old proxy.
3. `docker compose up -d`. The Hermes bootstraps run again and set the new
   stale-stream timeout and auto-title settings on every profile.
4. Optional: delete `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `FAST_LLM`,
   `SMART_LLM`, `STRATEGIC_LLM` and `EMBEDDING` from `.env` (see below).

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `LLM_STREAM_IDLE_TIMEOUT` | `LLM_TIMEOUT` | Longest silent gap in a streamed reply before llm-proxy drops it |
| `HERMES_STREAM_RETRIES` | `1` | Hermes reconnects per failed stream (was Hermes' default, 2) |
| `HERMES_PROXY_STALE_TIMEOUT` | 300 local / 180 cloud | `agent.local_stream_stale_timeout` on every bootstrapped profile (was 900) |
| `HERMES_BROWSER_AUTO_TITLE` | `auto` | WebUI auto-titling: off for a local upstream, on for a cloud one |
| `LLM_PROXY_AWAIT_PUSH` | empty | llm-proxy routes that answer 503 until an admin push names them |

### Removed `.env` keys

`setup.sh --ollama` no longer writes `OPENAI_BASE_URL`, `OPENAI_API_KEY`,
`FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` or `EMBEDDING`, and `.env.example`
no longer lists them. Compose sets them for `gpt-researcher` from `LLM_MODEL`
and `EMBEDDING_MODEL` and never read them from `.env`. You can delete them
from an existing `.env`.

### Behaviour changes

These change defaults. Set the keys above to get the old behaviour back.

- Hermes reconnects a failed stream once instead of twice.
- Bootstrap sets `agent.local_stream_stale_timeout` on every Hermes profile
  that it points at llm-proxy (default, `api-server`, `browser`, and any
  custom profile), sized from `LLM_BINDING_HOST`. Upstreams pushed later through the admin API are not
  seen; re-run the bootstraps after changing it.
- With a local upstream, the `browser` profile stops auto-titling sessions.
  The title call used the same model and ran ahead of the reply.

### Changes

- **llm-proxy** admin responses (`GET`/`POST /admin/upstreams`) gain a
  per-route `pushed` flag and a top-level `proxy` object with `boot_id` and
  `started_at`. `boot_id` changes on every start, so an admin client can tell
  the proxy restarted. Existing fields are unchanged.
- **llm-proxy** can hold routes until an admin client pushes them
  (`LLM_PROXY_AWAIT_PUSH`). A held route answers `503` with the reason
  instead of reaching the upstream without a key. Off by default.
- **docs/extending.md** documents starting the proxy with no key, the admin
  API as a stable interface, and the Hermes settings a downstream app should
  keep if it writes profile config itself.
- **llm-proxy** forwards each upstream chunk as it arrives. It used to hold
  small SSE events until 8 KB had collected, so a slow model looked stalled.
- **llm-proxy** drops the client connection when the upstream fails after the
  reply has started. It used to write a `502` status line into the middle of
  the chunked body. Failures before the reply starts are still a plain `502`.
- **llm-proxy** closes the upstream request when the client disconnects, even
  during prefill when nothing is being written. A dropped request no longer
  keeps a model server that runs one request at a time busy.

## v0.2.0 — 2026-10-06

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`.
2. If you overrode `lightrag` to use the published image, you can drop that
   override. If your `.env` sets `LIGHTRAG_BASE_IMAGE`, it still works. The
   new name is `LIGHTRAG_IMAGE`.
3. `docker compose up -d`. `lightrag` switches from the locally built
   `assistant-lightrag:local` to `ghcr.io/hkuds/lightrag:v1.4.9.3`, which is
   pulled on first start. You can then remove `assistant-lightrag:local`.

### Host requirements

- **python3 3.9 or newer** is now checked by `setup.sh` (it was already used,
  without a check). On macOS without the Command Line Tools, `/usr/bin/python3`
  is a stub. Run `xcode-select --install` or install Python first.
- **ssh-keygen** is now checked by `setup.sh` (used since v0.1.0 for the worker key).

### New optional `.env` keys

All default to the previous behaviour.

| Key | Default | Purpose |
|---|---|---|
| `CONTAINER_PREFIX` | empty | Prefix for every `container_name` |
| `IMAGE_PREFIX` | `assistant` | Built images are `${IMAGE_PREFIX}-<service>:local` |
| `NEO4J_HTTP_HOST_PORT` | `7474` | Host port for the Neo4j browser |
| `NEO4J_BOLT_HOST_PORT` | `7687` | Host port for Neo4j Bolt |
| `N8N_HOST_PORT` | `5678` | Host port for n8n (also used in `WEBHOOK_URL`) |
| `LIGHTRAG_IMAGE` | `ghcr.io/hkuds/lightrag:v1.4.9.3` | Replaces `LIGHTRAG_BASE_IMAGE` |
| `LLM_BINDING_API_KEY_FILE` | empty | Read the chat key from a file |
| `LIGHTRAG_LLM_BINDING_API_KEY_FILE` | empty | Read the LightRAG extraction key from a file |
| `EMBEDDING_BINDING_API_KEY_FILE` | empty | Read the embedding key from a file |

### Changes

- **llm-proxy** reads keys from `*_API_KEY_FILE` when set, and re-reads them on
  `SIGHUP` or `POST /admin/reload`. You can rotate a key without recreating the
  container, and it stays out of `docker inspect`.
- **LightRAG** runs the published image. The local patch build no longer
  built against v1.4.9.3.
- **Signal and Buzz profile syncs** edit `config.yaml` through the Hermes image
  instead of `docker compose exec hermes`. They now work before the first start
  and under any compose project name. They no longer append a second
  `platforms:` key, and they merge the duplicate keys left by earlier runs.
- **`make doctor`** fails when a nested `/opt/projects/<slug>` bind on `hermes`
  has no host folder.
- **`reset-neo4j-password.sh`** finds Neo4j through compose instead of by
  container name.
- **Docs:** new `docs/extending.md` covers second copies, overlay mounts,
  stable container paths, worker mounts and key rotation.

## v0.1.0 — 2026-10-06 (commit 259fd1f)

The first tagged release. It covers the work since `edf2465`.

### New services

| Service | Profile | Notes |
|---|---|---|
| `llm-proxy` | `core` | Holds the real model keys. Every client calls it with the placeholder `local-llm`. Admin API on `127.0.0.1:${LLM_PROXY_ADMIN_PORT:-4001}`. |
| `hermes-worker` | `core` | SSH target for Hermes shell and file tools when `HERMES_TERMINAL_BACKEND=ssh` (the default). Defined in `compose/hermes/terminal-ssh.yml`. |
| `opencode-mcp` | `coding` | Lets Hermes delegate `coding_*` tasks to OpenCode. |
| `caldav-mcp` | `calendar` | CalDAV calendars over MCP, with multiple accounts. |
| `signal-cli` | `signal` | Linked-device Signal for Hermes. |

### Required `.env` keys

`setup.sh` fills in any of these that are missing. It generates the secrets and prompts for or defaults `OPENCODE_WORKSPACE_HOST`.

- `LLM_PROXY_ADMIN_TOKEN`
- `LIGHTRAG_API_KEY`
- `OPENCODE_SERVER_PASSWORD`, `OPENCODE_WORKSPACE_HOST`
- `HERMES_DASHBOARD_PASSWORD`, `HERMES_DASHBOARD_AUTH_SECRET` (already required before)

### Host requirements

- `ssh-keygen`, to create the worker key under `compose/hermes/worker/ssh/`.
- `python3` (3.9+), for the setup and sync scripts.

### Notable changes

- Hermes shell and file tools run on `hermes-worker` by default. Hermes mounts
  `/opt/projects` read-only. Project folders and coding or context roots
  belong on the worker.
- `ASSISTANT_DATA_ROOT` moves projects, voice, memory and LightRAG inputs
  outside the repo.
- `sources.yaml` project registry, plus `CODING_EXTRA_ROOTS` and `CONTEXT_EXTRA_ROOTS`.
- Independent agents replace Buzz employees.
- Browser-harness sockets moved off the `data/hermes` bind mount.
