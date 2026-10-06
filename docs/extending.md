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
`local-llm`. To change a model or key without recreating anything:

- `POST /admin/upstreams` on `127.0.0.1:${LLM_PROXY_ADMIN_PORT}` with
  `Authorization: Bearer $LLM_PROXY_ADMIN_TOKEN` and a body such as
  `{"chat": {"url": "...", "api_key": "..."}}` (routes: `chat`, `lightrag`,
  `embed`). This is in memory only, and a restart reverts to the configured keys.
- Or mount a key file and set `LLM_BINDING_API_KEY_FILE`
  (`LIGHTRAG_LLM_BINDING_API_KEY_FILE`, `EMBEDDING_BINDING_API_KEY_FILE`) to
  its container path. The file wins over the matching `*_API_KEY`. After
  rewriting it, send `docker compose kill -s HUP llm-proxy` or
  `POST /admin/reload`. The key never appears in `.env` or `docker inspect`.

LightRAG, GPT Researcher, OpenCode and Hermes all call the proxy, so they pick
up the new upstream on their next request.
