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
5. Optional, for mail: `./scripts/setup.sh --mail`, fill in
   `mail-index.toml`, `make mail-index-doctor`, `make mail-index`, `make up`,
   re-run `hermes-api-bootstrap` and `hermes-browser-bootstrap`, restart
   `hermes`. See [docs/email.md](docs/email.md). Re-running setup without
   `--mail` adds `MAIL_MCP_ENABLED=0` and starts nothing.
6. Optional, for email drafts: `./scripts/setup.sh --mail-drafts`, `make up`,
   re-run both bootstrap services, restart `hermes`, and keep
   `make mail-drafts-watch` running on the host. Re-running setup without
   `--mail-drafts` adds `MAIL_DRAFTS_ENABLED=0` and starts nothing.
7. With mail or drafts enabled: rebuild the locally built mail images, since
   `make up` doesn't rebuild them: `docker compose up -d --build mail-mcp`
   (and `mail-drafts-mcp` with drafts). An old `mail-mcp` has no
   `/watermark`, and `mail-triage` then fails on every tick. Then re-run both
   bootstrap services and recreate `hermes`, then `make doctor`. Cron jobs without their own tool list stop
   getting the mail tools; doctor lists any job that still can read mail
   alongside other tools.
8. `docker compose up -d --build llm-proxy` for the new output and total-time
   limits (the proxy is a locally built image), then re-run both bootstrap
   services so every profile gets `model.max_tokens`. Profiles your app
   creates later need it written too (docs/extending.md).
9. With mail enabled, for the mail index's schema 2: first
   `docker compose up -d --build mail-mcp` (the new image reads schema 1 and
   2; the old one refuses 2), then `make mail-apps` and reinstall the reader
   (and drafts) app, then run `make mail-index` once. The run upgrades the
   index in place: ids are kept and nothing is fetched again, so
   `mail-triage` carries on where it was. Drafts requests queued before the
   upgrade still work.
10. Image pins moved (see Changes). `setup.sh` never rewrites an image key
    that is already in `.env`, so update any of `VIKUNJA_PROXY_IMAGE`,
    `SIGNAL_CLI_IMAGE` and `CALDAV_MCP_IMAGE` you have set, or delete the
    line to take the new default. Then `docker compose up -d`.
11. With `rag` enabled, LightRAG moves from v1.4.9.3 to v1.5.7 (update
    `LIGHTRAG_IMAGE` if it is in `.env`). Back up `rag_storage` and the
    `neo4j_data` volume first. On a Linux host the new entrypoint runs
    `chown -R 1000:1000` on the mounted `rag_storage` and `inputs` before
    dropping to uid 1000, so those host directories change owner. To keep
    them owned by your uid, set `user:` on `lightrag` in an override; the
    entrypoint then skips the chown, and the directories must be writable by
    that uid.

### New services

- `vikunja` (new **`todo`** profile, off by default): [Vikunja](https://vikunja.io)
  2.7.0 (AGPL-3.0) with its built-in MCP server. No host port. It joins
  `default` and the internal `db` network and keeps its data in
  schema `vikunja` of the shared Postgres. `todo` is added to
  `x-db-profiles`, so it starts `postgres-db`, `db-provision`, and the SQL MCP
  sidecars too. The SQL MCP is still registered only with `SQL_MCP_ENABLED=1`.
- `vikunja-proxy` (`todo` profile): `nginx:1.29.1-alpine` on `127.0.0.1:3456`
  in front of `vikunja`. Passes everything through and serves `/login-local`,
  which signs the browser in as the stack-managed account. About 4 MiB of RAM,
  capped at half a CPU.
- `mail-mcp` (new **`mail`** profile, off by default): read-only MCP over
  the host-built mail index, built locally from `compose/mail-mcp`
  (`python:3.11-slim`, `mcp==1.30.0`). No host port. Mounts only
  `MAIL_INDEX_DIR`, read-only, with a read-only root filesystem, all
  capabilities dropped and `no-new-privileges`. Runs as `HERMES_UID:HERMES_GID`.
- `mail-drafts-mcp` (new **`mail-drafts`** profile, off by default): MCP with
  `mail_draft_create` and `mail_draft_status`, built locally from
  `compose/mail-drafts-mcp` (`python:3.11-slim`, `mcp==1.30.0`). It only
  queues requests in `MAIL_DRAFTS_DIR`; it doesn't mount the mail index and
  can't send. No host port, read-only root filesystem, all capabilities
  dropped, `no-new-privileges`, runs as `HERMES_UID:HERMES_GID`.

### Required `.env` keys

Only with `todo` enabled.

- `VIKUNJA_SECRET`: `setup.sh` generates it for every install. If it is empty,
  Vikunja picks a new one each start and everyone is logged out.
- `VIKUNJA_MCP_TOKEN_RW` and `VIKUNJA_MCP_TOKEN_RO`: written by
  `make todo-init`. Without them the bootstrap logs a warning and registers
  nothing.
- `VIKUNJA_USERNAME` and `VIKUNJA_PASSWORD`: written by `make todo-init`.
  Without them `/login-local` shows an error and asks you to run it.

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `VIKUNJA_MCP_ENABLED` | `0` | `1` registers `mcp_servers.todos` on Hermes. `setup.sh --todo` sets it |
| `VIKUNJA_PORT` | `3456` | Loopback host port for the web UI |
| `VIKUNJA_PUBLIC_URL` | `http://localhost:${VIKUNJA_PORT}/` | Base URL in Vikunja's links and MCP page |
| `VIKUNJA_ENABLE_REGISTRATION` | `false` | Allow sign-up from the web UI |
| `VIKUNJA_SESSION_TTL` | `315360000` | Sign-in lifetime in seconds (10 years) |
| `VIKUNJA_PROXY_IMAGE` | `nginx:1.29.1-alpine` | `vikunja-proxy` image |
| `VIKUNJA_FILES_DIR` | `./data/vikunja/files` | Attachments bind mount |
| `VIKUNJA_IMAGE` | `vikunja/vikunja:2.7.0` | Vikunja image (MCP needs 2.7.0+) |
| `VIKUNJA_MCP_URL` | `http://vikunja:3456/api/v2/mcp` | Endpoint the bootstrap registers |
| `VIKUNJA_MCP_TIMEOUT` / `VIKUNJA_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `vikunja` |
| `MAIL_MCP_ENABLED` | `0` | `1` registers `mcp_servers.mail` on the default and browser profiles. `setup.sh --mail` sets it |
| `MAIL_INDEX_DIR` | `./data/mail` | Host folder holding the index; mounted read-only into `mail-mcp` |
| `MAIL_INDEX_FILE` | `index.sqlite` | Index file name inside `MAIL_INDEX_DIR` |
| `MAIL_MCP_URL` | `http://mail-mcp:8000/mcp` | Endpoint the bootstrap registers |
| `MAIL_MCP_TIMEOUT` / `MAIL_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `mail` |
| `MAIL_DRAFTS_ENABLED` | `0` | `1` registers `mcp_servers.drafts` on the default and browser profiles. `setup.sh --mail-drafts` sets it |
| `MAIL_DRAFTS_DIR` | `./data/mail-drafts` | Host folder for queued requests and their status; mounted into `mail-drafts-mcp` |
| `MAIL_DRAFTS_MCP_URL` | `http://mail-drafts-mcp:8000/mcp` | Endpoint the bootstrap registers |
| `MAIL_DRAFTS_MCP_TIMEOUT` / `MAIL_DRAFTS_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `drafts` |
| `LLM_DEFAULT_MAX_TOKENS` | `16384` | llm-proxy adds `max_tokens` to chat/completions requests that set no output limit. `0` = off |
| `LLM_STREAM_MAX_SECONDS` | `3600` | Longest one llm-proxy request may take, start to finish. `0` = no limit |
| `HERMES_MAX_TOKENS` | `16384` | `model.max_tokens` bootstrap writes to proxy profiles that have none. `0` = write nothing |

### Behaviour changes

These change defaults, to stop a model that never stops generating (a local
model ran one reply for 13 hours with no `max_tokens`; every timeout watched
only for silence). Set the keys above to `0` to get the old behaviour back.

- llm-proxy adds `max_tokens: 16384` to generation requests on the chat and
  LightRAG routes that set none of `max_tokens`, `max_completion_tokens` or
  `max_output_tokens`. A request with its own limit is unchanged.
- llm-proxy drops any request still running after 3600s, the same way as an
  idle stream (dead connection, upstream closed).
- Bootstrap sets `model.max_tokens=16384` on every proxy profile that has no
  value. Existing values, including `browser`'s 1024, are kept.

### Changes

- **Image pins**: security and client-age bumps. `vikunja-proxy` moves from
  `nginx:1.29.1-alpine` (an end-of-life mainline release) to
  `nginx:1.30.6-alpine`. `signal-cli` moves from `signal-cli-jre:v0-14-7-2`
  to `v0-14-9-2`, ahead of Signal retiring older clients; the linked account
  store is read as before. `caldav-mcp` moves to `ghcr.io/gelse/caldav-mcp:0.1.1`.
  The old default, `v0.1.0`, is not a published tag (upstream tags have no
  `v`), so that pin never pulled. Upstream publishes linux/amd64 only, so on
  Apple Silicon the `calendar` profile needs
  `DOCKER_DEFAULT_PLATFORM=linux/amd64` or a `platform:` override.
- **LightRAG** v1.4.9.3 → v1.5.7, for the advisories fixed in 1.5.3–1.5.7
  (guest-token auth bypass, Cypher injection, upload path traversal). The
  server runs as uid 1000 instead of root. Without an API key, `/health`
  now returns only status fields (`status`, `core_version`,
  `pipeline_busy` and the like); `configuration`, queue and worker details
  need `X-API-Key`. The compose healthcheck and the `check_lightrag_health`
  MCP tool still pass. Entity and relation deletes moved from
  `/documents/delete_entity` and `/documents/delete_relation` to
  `/graph/entity/delete` and `/graph/relation/delete`.
- **lightrag-mcp**: `delete_by_entities`, `delete_by_doc_ids`,
  `create_entities`, `edit_entities`, `create_relations`, `edit_relations`
  and `merge_entities` call routes LightRAG does not serve (`/entities/…`,
  `/relations/…`, `/merge`, `DELETE /documents/{id}`), on v1.4.9.3 and
  v1.5.7 alike. LightRAG answers 404 and lightrag_mcp 0.1.1 reports
  `"status": "success"` with a `null` result. Queries, inserts, uploads,
  document listing and health work.
- **Hermes bootstrap**: registers `mcp_servers.todos` (named for what users
  call it; an earlier `mcp_servers.vikunja` entry is removed) with an
  `Authorization: Bearer ${VIKUNJA_MCP_TOKEN}` header. The read-only token goes
  to `api-server`, the read/write token to default and `browser`, each into
  that profile's `.env`. With the profile off or the token missing, it removes
  the entry and the token.
- **`living-todos` skill**: uses the Vikunja tools when they are listed, and
  the markdown file otherwise. The Hermes system prompt says the same.
- **`make todo-init`** (`scripts/todo.sh`): asks nothing. It creates the
  Vikunja account (or adopts the only existing one) with a generated password
  kept in `.env`, mints both tokens, and recreates `vikunja-proxy`.
  Re-running it rotates the tokens. An adopted account's old password stops
  working. Both tokens add kanban on top of Vikunja's *Typed* preset: both
  read project views and buckets, and the read/write token also creates,
  renames and deletes buckets and moves tasks between them. Tokens minted
  before this change have no bucket access; re-run `make todo-init`, both
  bootstrap services, and restart `hermes`.
- **`make hermes-reload`**: runs both Hermes profile bootstraps and recreates
  `hermes`, the three steps most config changes need. `make hermes-upgrade`
  uses it, and the docs, `make doctor` hints, and `make todo-init` point to it.
- **`make todo-open`**: opens `http://localhost:3456/login-local`, signed in.
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
- **Mail indexer** (host-side, opt-in): `scripts/mail-index.py` indexes the
  accounts, mailboxes and date window named in a `mail-index.toml` config
  (copy `mail-index.toml.example`) into a local SQLite file with full-text
  search. By default it reads through Mail.app scripting, which needs the
  Automation permission for Mail and not Full Disk Access. With
  `source = "store"` it reads a snapshot of Mail's files instead, which is
  faster but needs Full Disk Access and a `store` path (there's no default).
  It never writes to Mail and prints counts and timings only. Bodies and
  attachment names are separate opt-ins. Exit code 4 means macOS refused the
  permission; 5 means a call to Mail failed or timed out. `make mail-index`,
  `make mail-index-doctor`, `make mail-index-mailboxes`,
  `make mail-index-test`, `make mail-script-test` (needs node). See
  [docs/email.md](docs/email.md).
- **Mail MCP** (`mail` profile): `mail_status`, `mail_search`, `mail_get` and
  `mail_thread` over the index. Every result carries a notice that message
  text is untrusted, and bodies come between markers that a message can't
  forge. Registered on the default and browser profiles only. With the
  profile off, or on `api-server`, the bootstrap removes any entry pointing
  at it. `make doctor` gets a mail section (profile, flag, index present,
  folder mode, per-profile registration, `api-server` not registered).
  `make mail-mcp-test`.
- **Mail drafts** (`mail-drafts` profile): Hermes can queue email drafts, and
  `scripts/mail-drafts.py` on the host saves them to Mail's Drafts through
  scripting. Nothing is ever sent; you send drafts from Mail. Replies thread
  through Mail's own reply. Requests are checked twice (in the container and
  on the host): plain addresses only, at most 20 recipients, one-line
  subjects, no attachments, a sender only from your own Mail accounts, and
  at most 20 drafts an hour. Registered on the default and browser profiles
  only. `make mail-drafts`, `make mail-drafts-watch`,
  `make mail-drafts-doctor`, `make mail-drafts-test`; `make doctor` gets a
  drafts section.
- **Mail helper apps** (macOS, opt-in): `make mail-apps` builds
  *Assistant Mail Reader* and *Assistant Mail Drafts*, each a small signed
  app carrying only its own helper's scripts, so each holds its own
  Automation permission (and Full Disk Access only for the reader, only with
  `source = "store"`) instead of your terminal or `python3`.
  `macos/mail-apps/install.sh` copies one to `~/Applications` and loads a
  LaunchAgent: the reader every 15 minutes, the drafts helper continuously.
  Needs `swiftc` and Python 3.11+. `make mail-apps-test`. Build output goes
  to the gitignored `build/`.
- **Mail MCP**: `mail_search` takes `after_id`, `mail_status` reports
  `newest_id`, and `GET /watermark` returns `newest_id=<n>`, which changes
  only when newer mail is indexed and holds no message content.
- **Mail triage cron** (opt-in): `make mail-triage-cron` creates a
  `mail-triage` Hermes cron job on the default profile with the mail tools
  only, which runs only when newer mail is indexed (`--monitor-url` on the
  watermark) and reports to Signal when `SIGNAL_HOME_CHANNEL` is set.
  `make mail-triage-cron-show`, `make mail-triage-cron-remove`,
  `make mail-cron-test`. See [docs/email.md](docs/email.md#proactive-review-cron).
- **Hermes bootstrap**: while mail or drafts is registered on a profile, the
  bootstraps set `platform_toolsets.cron` to `hermes-cron` plus every other
  MCP server, so cron jobs without their own tool list no longer get the mail
  tools. They keep everything else. A `platform_toolsets.cron` you wrote is
  kept, minus mail and drafts. The list is removed again when mail and drafts
  are both off. MCP servers added by hand reach cron jobs after the next
  bootstrap run. `make doctor` flags any cron job that can read mail
  alongside other tools.
- **Mail indexer**: deleted text is overwritten. The index turns on SQLite's
  `secure_delete` and FTS5's `secure-delete` (SQLite 3.42+), so a message
  that leaves scope, or a body blanked by turning `bodies` off, is gone from
  the file at once. It's compacted once for older indexes, after removals on
  older SQLite, and when over a quarter of it is free space. `run` reports
  `compacted`.
- **Mail indexer**: in `mailboxes` patterns, `*` now matches within one
  level and `**` matches any depth. `*` used to match nested mailboxes too,
  so a config using `*` or `Name/*` may index fewer mailboxes; use `**` to
  keep them. `run` and `doctor` report `mailboxes_missing` for exact names
  that weren't found.
- **Mail indexer (script source)**: mailboxes are looked up by path instead
  of walking the account's whole mailbox tree for every lookup, and accounts
  whose `mailboxes` are all exact names aren't listed at all. On accounts
  with many mailboxes this removes tens of seconds per run and per batch of
  50 messages. The drafts helper's reply lookup gets the same change.
- **Mail indexer**: saves progress every 50 messages, so a long first run
  that's cut short resumes on the next run instead of starting over.
- **Mail providers**: sources are now chosen per account
  (`[[accounts]] source = …`), and one index can mix them. See
  [docs/email.md](docs/email.md#providers).
  - Index schema 2 numbers messages itself, counting up and never reusing
    an id, and keeps each source's own id as `source_id`. `newest_id` and
    `after_id` now mean "indexed after" across every account. Results gain a
    `reply_ref`, which `mail_draft_create` takes as `reply_to`; the older
    `{id, account, mailbox}` form still works.
  - New `imap` source (opt-in): reads an IMAP account with `EXAMINE` and
    `BODY.PEEK[]`, so nothing on the server changes. The password lives in
    the Keychain (`macos/mail-apps/install.sh password <id>`) and the mail
    apps' launcher hands it to the helper on stdin; terminal runs use
    `password_env`. Proton works through Proton Mail Bridge.
  - Drafts for IMAP accounts are appended to the account's Drafts folder
    (`mail-drafts.py --config`, `--no-mail-app` without Mail.app). Still no
    way to send.
  - Message bodies are stored with LF line endings whatever the source.
  - `make mail-imap-test` checks the IMAP source and drafts against a
    throwaway GreenMail server (`greenmail/standalone:2.1.3`, pulled from
    Docker Hub on first run) loaded with the synthetic mail. It's skipped
    when Docker isn't running.
- **Docs**: [docs/email.md](docs/email.md) records the design for giving Hermes
  read access to Apple Mail, why scripting Mail.app is the default over Full
  Disk Access, and a survey of existing Apple Mail MCP servers.
  `scripts/make-synthetic-mail.py` writes a fake Mail store for developing
  the reader, and `make synthetic-mail-test` checks it.
- **iMessage indexer** (host-side, not wired into Hermes yet): `imessage/`
  holds a self-contained, stdlib-only reader for Messages.app history.
  `make messages-index` reads `chat.db` read-only into a scoped SQLite + FTS
  index; `make messages-index-doctor` and `make messages-index-chats` help
  write `messages-index.toml` (gitignored; template in
  `imessage/messages-index.toml.example`). It needs Full Disk Access and has
  been tested only against synthetic data. `make messages-index-test` runs
  its tests. See [imessage/README.md](imessage/README.md).
- **iMessage watcher and app** (host-side, not wired into Hermes yet):
  `make messages-watch` keeps the index current and, with a `[notify]`
  table, raises metadata-only new-message events to an outbox folder or a
  signed HTTP endpoint (Hermes generic-webhook HMAC V2). `make messages-app`
  builds *Assistant Messages Reader*, a sealed macOS app that holds Full Disk
  Access instead of your terminal, and `imessage/macos/install.sh` loads a
  LaunchAgent that runs the watcher. New host requirement only if you use
  it: grant the app Full Disk Access. Tests: `make messages-watch-test`,
  `make messages-app-test`.
- **Tests**: `imessage/make-synthetic-messages.py` writes a fake Messages.app
  store (`chat.db`, attachments, Contacts) for developing the iMessage reader,
  and `make synthetic-messages-test` checks it. See
  [imessage/README.md](imessage/README.md#synthetic-store).
- **Docs**: [docs/messages.md](docs/messages.md) records the design for giving
  Hermes read access to iMessage history and new-message events, and the
  open-source readers evaluated for it. Nothing reads messages yet.
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
