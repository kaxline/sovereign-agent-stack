# Changelog

Each release lists what a downstream install needs to know before moving its
pin: new services, required `.env` keys, new host requirements and upgrade
steps. Pin a tag, not a branch. See [docs/releasing.md](docs/releasing.md) for
how releases are cut and [docs/extending.md](docs/extending.md) for running
the stack under another app.

## Unreleased

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `LLM_STREAM_IDLE_TIMEOUT` | `LLM_TIMEOUT` | Longest silent gap in a streamed reply before llm-proxy drops it |
| `HERMES_STREAM_RETRIES` | `1` | Hermes reconnects per failed stream (was Hermes' default, 2) |
| `HERMES_PROXY_STALE_TIMEOUT` | 300 local / 180 cloud | `agent.local_stream_stale_timeout` on every bootstrapped profile (was 900) |
| `HERMES_BROWSER_AUTO_TITLE` | `auto` | WebUI auto-titling: off for a local upstream, on for a cloud one |

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
