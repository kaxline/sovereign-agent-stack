# Changelog

Each release lists what a downstream install needs to know before moving its
pin: new services, required `.env` keys, new host requirements and upgrade
steps. Pin a tag, not a branch. See [docs/releasing.md](docs/releasing.md) for
how releases are cut and [docs/extending.md](docs/extending.md) for running
the stack under another app.

## Unreleased

### Upgrade steps

1. `docker compose up -d --force-recreate hermes` so cont-init applies the
   new overlay. No `.env` changes.

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
