# Extending the stack downstream

How another app can run this stack under its own names, ports and data paths,
and which parts of `docker-compose.yml` it can rely on across upgrades.

Pin a release tag, not a branch. [CHANGELOG.md](../CHANGELOG.md) lists each
release's new services, required `.env` keys and new host requirements.

## Run a second copy

Docker container names and image tags are global. Set these in the copy's
`.env`. Both `docker compose` and every setup script read them from there:

| Key | Default | Effect |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | repo folder name | Network, volume and default container names |
| `CONTAINER_PREFIX` | empty | Prepended to every `container_name` (`boundary-` → `boundary-hermes`) |
| `IMAGE_PREFIX` | `assistant` | Built images become `${IMAGE_PREFIX}-<service>:local` |
| `*_PORT`, `*_HOST_PORT` | see `.env.example` | Every published host port is a variable |

Put the project name in `.env` (or export it), not only on the command line.
`docker compose -p <name>` is not seen by `scripts/setup.sh`, the `sync-*`
scripts or `make`. They run plain `docker compose`, which falls back to the
folder name. For the same reason, list extra compose files in `COMPOSE_FILE`
rather than with `-f`:

```bash
COMPOSE_PROJECT_NAME=boundary-assistant
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:/path/to/boundary.yml
CONTAINER_PREFIX=boundary-
IMAGE_PREFIX=boundary-assistant
```

Services reach each other by service name (`http://llm-proxy:4000`,
`hermes-worker` over SSH), so renamed containers need no other changes.

`scripts/setup.sh` is safe to re-run over an existing `.env` and `data/`. It
keeps existing values, generates any secret that is missing or still a
placeholder, and leaves `data/` in place. It does not copy other new keys from
`.env.example` into an existing `.env`. The changelog lists any new key a
release requires.

## Add or change mounts

Compose merges `volumes` lists from overlay files by **container path**. A
downstream overlay should list only its own mounts:

- To add a mount, use a new container path.
- To move a data directory, reuse the same container path with a different
  host path. Your entry replaces the upstream one.
- Do not use `!override` or `!reset` on a `volumes` list. Doing so drops every
  mount upstream adds later, including the patch files under `/bootstrap/` and
  `/etc/cont-init.d/`, and Hermes starts without them.

These container paths are stable. They change only with a note in the changelog:

| Service | Container path | Holds |
|---|---|---|
| `hermes` | `/opt/data` | Hermes home: config, profiles, sessions, `.env` |
| `hermes`, `hermes-webui` | `/opt/projects` | Project library (read-only on `hermes` under ssh) |
| `hermes-worker`, `hermes-webui` | `/opt/projects`, `/opt/voice`, `/opt/memory` | Projects, voice samples, curated memory |
| `hermes`, `hermes-worker` | `/opt/skills` | Bundled skills (read-only) |
| `lightrag` | `/app/data/inputs`, `/app/data/rag_storage` | Corpus inputs and index |

Most data paths also follow `ASSISTANT_DATA_ROOT` ([data-dir.md](data-dir.md)),
which is usually simpler than overriding mounts.

### Extra worker mounts

With `HERMES_TERMINAL_BACKEND=ssh` (the default), shell and file tools run on
`hermes-worker`, so project folders and coding roots go there. Hermes itself
gets read-only views. Two ways to add them:

- `CODING_EXTRA_ROOTS` / `CONTEXT_EXTRA_ROOTS` in `.env`, then
  `make ensure-local`. `sync-terminal-backend.sh` writes the binds into
  `docker-compose.override.yml` on whichever service is active.
- Your own overlay, mounting each host path at the **same path** on
  `hermes-worker` (read-write) and, if Hermes needs to list it, on `hermes`
  (`:ro`). Same-path mounts keep paths in `INDEX.md` and tool output valid on
  both sides.

### Nested binds under `/opt/projects`

Under ssh, `hermes` mounts `/opt/projects` read-only. Docker cannot create a
mount point inside a read-only mount, so binding a host folder at
`/opt/projects/<slug>` fails unless `<projects dir>/<slug>` already exists on
the host. Create it first:

```bash
mkdir -p "${ASSISTANT_DATA_ROOT:-./data}/projects/<slug>"
```

`make doctor` fails on a nested bind whose host folder is missing. A context
root (`make context-root-add`) avoids the nested bind entirely and is the
better fit for a folder that lives elsewhere on disk.

## Model keys

`llm-proxy` holds the real upstream keys. Clients send the placeholder
`local-llm`. A key can reach the proxy three ways:

- `*_API_KEY` in `.env`. This is the default. The key is in the container's
  environment and shows up in `docker inspect`.
- A key file (`LLM_BINDING_API_KEY_FILE` and the `LIGHTRAG_LLM_BINDING_` and
  `EMBEDDING_BINDING_` versions) set to a container path such as
  `/run/secrets/llm_key`. The file wins over the matching `*_API_KEY`. After
  rewriting it, send `docker compose kill -s HUP llm-proxy` or
  `POST /admin/reload`. The key is never in `.env` or `docker inspect`, and
  it survives a proxy restart.
- The admin API (below). The key lives only in the proxy's memory.

LightRAG, GPT Researcher, OpenCode and Hermes all call the proxy, so they pick
up a new upstream on their next request.

### Admin API

The admin listener is published on `127.0.0.1:${LLM_PROXY_ADMIN_PORT}` only.
Every request needs `Authorization: Bearer $LLM_PROXY_ADMIN_TOKEN`.

| Request | Effect |
|---|---|
| `GET /admin/upstreams` | Current table, without keys |
| `POST /admin/upstreams` | Replace the fields you send, in memory only |
| `POST /admin/reload` | Re-read key files; returns `{"reloaded": [...], "upstreams": {...}}` |

The routes are `chat` (`/v1`), `lightrag` (`/lightrag/v1`) and `embed`
(`/embed/v1`). A POST body names any of them, each with `url`, `api_key` or
both. A field you leave out keeps its value, and a route you leave out is not
touched:

```json
{"chat": {"url": "https://openrouter.ai/api/v1", "api_key": "sk-..."},
 "lightrag": {"url": "https://openrouter.ai/api/v1", "api_key": "sk-..."}}
```

Both upstream requests return the table:

```json
{"chat": {"url": "https://openrouter.ai/api/v1", "api_key_set": true, "pushed": true},
 "lightrag": {"url": "https://openrouter.ai/api/v1", "api_key_set": true, "pushed": true},
 "embed": {"url": "http://host.docker.internal:1234/v1", "api_key_set": false, "pushed": false},
 "proxy": {"boot_id": "9f2c...", "started_at": 1791331200}}
```

- `pushed` is true once an admin POST has named the route since the proxy
  started, even with an empty key.
- `boot_id` changes every time the proxy process starts. When it differs from
  the one your last push returned, the proxy restarted and lost what you
  pushed. Use it to decide when to push again. `api_key_set` cannot tell you,
  because a local server legitimately has no key.
- An empty `api_key` means the upstream needs none. The proxy then sends no
  `Authorization` header at all.
- A `lightrag` route with an empty `url` uses the `chat` route.

These paths, route names and fields are a stable interface. New fields may be
added. Removing or renaming one, or changing what it means, gets a changelog
entry under a new release before it ships.

### Start without a key

A downstream app can keep every key out of `.env` and push it at runtime.
This is supported:

- Leave `LLM_BINDING_API_KEY=` and `LIGHTRAG_LLM_BINDING_API_KEY=` blank and
  set no `*_FILE`. The proxy starts with no key.
- `/health` returns 200 whether or not any route has a key, so a compose
  `depends_on: service_healthy` gate still opens.
- Push the upstream with `POST /admin/upstreams`, and push again whenever
  `boot_id` changes.
- Set `LLM_PROXY_AWAIT_PUSH=chat,lightrag` (any of `chat`, `lightrag`,
  `embed`). Until an admin POST names a listed route, requests to it get a
  `503` saying the admin client has not pushed it yet, instead of going
  upstream without a key and coming back as the provider's `401`. List only
  routes your app pushes. A listed route needs its own push, even a
  `lightrag` route that would fall back to `chat`.

While your app is not running, nothing re-pushes after a restart (Docker
restart, reboot, crash), and listed routes answer 503 until it is back. If
that gap matters, use a key file instead. It is read again on every start.

### Configuring Hermes yourself

The Hermes bootstraps point every profile at the proxy. If your app also
writes Hermes profile config, keep it consistent with what bootstrap sets:

- `model.provider=custom`, `model.base_url=http://llm-proxy:4000/v1`,
  `model.api_key=local-llm`. Set both `model.default` and `model.model`, as
  `scripts/model-use.sh` does.
- `agent.local_stream_stale_timeout`: Hermes treats `llm-proxy` as a local
  server whatever is behind it. Bootstrap sets 300 for a local upstream and
  180 for a cloud one, judged from `LLM_BINDING_HOST` in `.env`. It cannot
  see an upstream pushed through the admin API, so either write the upstream
  URL (not the key) to `LLM_BINDING_HOST` too, or set this yourself.
- Leave `agent.tool_use_enforcement` at `auto` and `agent.execution_guidance`
  unset. Forcing them on helps some chatty models but makes weaker or
  quantized ones fake tool calls in text more often
  ([hermes.md](hermes.md)).
