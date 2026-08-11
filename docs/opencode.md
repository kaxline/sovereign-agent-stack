[← Back to README](../README.md)

# OpenCode agent

Isolated AI coding agent with a browser UI and an OpenAPI server, backed by your local model server.

[OpenCode](https://opencode.ai) runs as an isolated AI coding agent, serving a browser UI and an OpenAPI server on the same port. Enable it with the **`coding`** profile, and keep `core` around for SearXNG MCP:

```bash
./scripts/setup.sh --coding
# COMPOSE_PROFILES=core,coding
```

It uses your local OpenAI-compatible model server (or Ollama) via `host.docker.internal`. Setup copies tracked `opencode/opencode.json` to gitignored `opencode.local.json` (compose mounts the local file). Edit the local overlay to set your provider id, `baseURL`, and model ids — the tracked template uses placeholders only.

## First-time setup

1. In `.env`, set `OPENCODE_SERVER_PASSWORD` (defaults are in `.env.example`).
2. Start the stack: `docker compose up -d` (or just `docker compose up -d opencode`).
3. Open `http://localhost:4096` and sign in with `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`.
4. Confirm the default model in `opencode/opencode.local.json` matches what your server is serving (same id as `LLM_MODEL` in `.env` is a good convention).

### Example: LM Studio

If you use [LM Studio](https://lmstudio.ai) on the host:

1. Enable **Serve on Local Network** and note the port (often `1234`).
2. In `opencode.local.json`, point the `local` provider `baseURL` at `http://host.docker.internal:1234/v1` and list the model id LM Studio shows (OpenCode model ref is `local/<model-id>`).
3. Keep `LLM_MODEL` / `OPENAI_BASE_URL` in `.env` aligned with the same server.

## Host workspace

OpenCode bind-mounts a host directory into the container **at the same absolute path**. Set `OPENCODE_WORKSPACE_HOST` in `.env` to the path you want it to use; the value must be absolute, must not be `/`, and must not end in a slash:

```bash
# Parent folder of many projects:
OPENCODE_WORKSPACE_HOST=/Users/you/code

# Or a single project root:
OPENCODE_WORKSPACE_HOST=/Users/you/code/my-project
```

Mirroring the path means every path the agent prints, stores in a session, or resolves as a git worktree is the same path you would type in your own terminal. Nothing has to be translated between `/workspace` and the host.

After changing `.env`, recreate the container:

```bash
docker compose up -d opencode
```

In the web UI, click **Open project** and enter the absolute path of a repo (Tab completes). The picker starts at the server's home directory (`/root`), not your code, so type the full path the first time; after that it shows up under recent projects. Changes made by OpenCode appear on your host immediately.

Config, sessions, and credentials persist in `opencode_config` and `opencode_data` volumes across restarts. If you previously used the old `opencode_workspace` named volume, you can remove it with:

```bash
docker volume rm assistant_opencode_workspace 2>/dev/null || true
```

### Why the image is built locally

The upstream OpenCode image ships without a `git` binary, and OpenCode shells out to `git rev-parse` to find a directory's worktree root. Without it, every directory resolves to the single built-in `global` project, which costs you the per-repo project list, the per-repo session history, and any useful VCS diffs. [compose/opencode/Dockerfile](../compose/opencode/Dockerfile) is a thin layer over the upstream image that adds `git` and `openssh-client`, and marks bind-mounted repos as safe directories so git does not reject them for dubious ownership.

Confirm project detection is working:

```bash
curl -s -u "$OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD" \
  "http://localhost:4096/project?directory=$OPENCODE_WORKSPACE_HOST/my-project"
```

A project whose `worktree` is the repo path means git resolution succeeded. An `id` of `global` with a `worktree` of `/` means it failed.

### Hiding secrets from the agent

OpenCode has full read/write access to everything under the mounted path. Pointing it at a parent folder is convenient, but it also exposes every `.env` in every project below that folder, this repo's included if it lives there.

Neutralize them by mounting a blank read-only file over each secret. The paths are machine-specific, so they belong in `docker-compose.override.yml`, which Compose loads automatically and which is gitignored. Copy the template and edit the paths:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up -d opencode
```

Each entry maps the zero-byte [compose/opencode/blank](../compose/opencode/blank) over one secret:

```yaml
services:
  opencode:
    volumes:
      - ./compose/opencode/blank:/Users/you/code/my-project/.env:ro
```

The agent then reads an empty file, and since the mount is read-only it cannot overwrite the real one either. Find candidates under your mount with:

```bash
find "$OPENCODE_WORKSPACE_HOST" -name '.env' -not -path '*/node_modules/*'
```

Two caveats come with this. The list is a point-in-time snapshot: a new `.env` in a new project stays exposed until you add it, so re-run that `find` whenever you add repos. And a shadowed `.env` really is empty as far as the agent is concerned, which means OpenCode cannot debug anything that depends on real values. `.env.example` stays readable, so it can still see the expected shape.

## Web search and deep research (MCP)

OpenCode is pre-configured with two MCP sidecars in `opencode/opencode.local.json`:

- **searxng** (`mcp-searxng`) — fast web search: `searxng_web_search`, `web_url_read`
- **gptr** (`gptr-mcp`) — deep research: `deep_research`, `quick_search`, `write_report`, etc.

MCP tools load when the agent invokes them during a session.

## API access

The same port exposes the OpenAPI server for programmatic use (e.g. future n8n workflows):

| Endpoint | Description |
|---|---|
| `GET /global/health` | Health check |
| `GET /doc` | OpenAPI 3.1 spec |
| `POST /session` | Create a new session |
| `POST /session/{id}/message` | Send a prompt |

Inside the compose network, use `http://opencode:4096` with HTTP basic auth.

```bash
curl -u opencode:$OPENCODE_SERVER_PASSWORD http://localhost:4096/global/health
```
