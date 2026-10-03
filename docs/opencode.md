[← Back to README](../README.md)

# OpenCode agent

Isolated AI coding agent with a browser UI and an OpenAPI server, backed by your local model server.

[OpenCode](https://opencode.ai) runs as an isolated AI coding agent, serving a browser UI and an OpenAPI server on the same port. Enable it with the **`coding`** profile, and keep `core` around for SearXNG MCP:

```bash
./scripts/setup.sh --coding
# COMPOSE_PROFILES=core,coding
```

It calls `llm-proxy` (`http://llm-proxy:4000/v1`) with the placeholder `local-llm`. The proxy holds `LLM_BINDING_API_KEY` and forwards to `LLM_BINDING_HOST`. Setup copies tracked `opencode/opencode.json` to gitignored `opencode.local.json` (compose mounts the local file) and points every provider `baseURL` and `apiKey` at the proxy. Edit the local overlay for the provider id and model ids. The tracked template stays a placeholder.

## First-time setup

1. In `.env`, set `OPENCODE_SERVER_PASSWORD` (defaults are in `.env.example`).
2. Start the stack: `docker compose up -d` (or just `docker compose up -d opencode`).
3. Open `http://localhost:4096` and sign in with `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`.
4. Confirm the default model in `opencode/opencode.local.json` matches what your server is serving (same id as `LLM_MODEL` in `.env` is a good convention).

### Example: LM Studio

If you use [LM Studio](https://lmstudio.ai) on the host:

1. Enable **Serve on Local Network** and note the port (often `1234`).
2. Set `LLM_BINDING_HOST` in `.env` to `http://host.docker.internal:1234/v1` (the proxy forwards there). In `opencode.local.json`, list the model id LM Studio shows (OpenCode model ref is `local/<model-id>`). Leave `baseURL` at `http://llm-proxy:4000/v1` and `apiKey` at `local-llm`.
3. Keep `LLM_MODEL` in `.env` aligned with that model id.

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

## Hermes delegation

With the `coding` profile, Hermes can run coding jobs through OpenCode without opening the OpenCode UI. Setup sets `OPENCODE_MCP_ENABLED=1`, which registers an `opencode` MCP server on the **default** and **browser** profiles (not `api-server`).

| Piece | Role |
|---|---|
| `opencode` | Coding harness (LSP, edits, shell, git-aware sessions) |
| `opencode-mcp` | Thin SSE MCP bridge (`coding_*` tools) |
| Hermes | User-facing chat; `delegate-coding` skill routes locate/diagnose and implement/refactor/test work |

### Coding roots

Hermes and OpenCode share the same absolute host paths:

| Root | Source |
|---|---|
| Primary | `OPENCODE_WORKSPACE_HOST` (compose bind on both services) |
| Extras | `CODING_EXTRA_ROOTS` + matching volumes in `docker-compose.override.yml` |

```bash
make coding-root-list
make coding-root-add DIR=/absolute/path/to/repo
# then recreate so binds apply:
docker compose up -d --force-recreate hermes-worker opencode opencode-mcp
# recreate hermes instead of hermes-worker when HERMES_TERMINAL_BACKEND=local
```

If the user asks to work outside those roots, Hermes should ask them to confirm access and give them `make coding-root-add DIR=...` — it does not remount Docker itself.

**Not the same as context roots.** `CONTEXT_EXTRA_ROOTS` (`make context-root-add`) mounts notes into Hermes **read-only** and does not grant OpenCode or write-sandbox access. Use coding roots for git repos you want the coding agent to edit; use context roots for vaults the project INDEX should open. See [Projects](projects.md#extra-source-directories).

### Headless permissions

Tracked `opencode/opencode.json` sets `permission` so delegated sessions do not block on UI approval prompts (`*` allow, `question` deny). If your gitignored `opencode.local.json` predates that block, merge the `permission` object from the template (or recreate the local file from the template and re-apply your model settings).

### Smoke test

```bash
make opencode-smoke-fixture
# or: ./scripts/opencode-smoke-fixture.sh
```

That creates `$OPENCODE_WORKSPACE_HOST/opencode-smoke` with a failing `greet` stub and pytest.

In Hermes WebUI:

> In `$OPENCODE_WORKSPACE_HOST/opencode-smoke`, implement `greet(name: str) -> str` returning `Hello, {name}!`, add/fix the pytest so `greet("Ada") == "Hello, Ada!"`, and run the tests until they pass.

Expect Hermes to call `coding_start_task` / `coding_wait_for_task` / `coding_get_task_result`, and for files plus a passing test run on the host. Optionally ask for a path outside coding roots and confirm Hermes prompts for `make coding-root-add`.

To check locate routing (bare repo name, not `/opt/projects/<guess>`), ask Hermes to look in `opencode-smoke` for the `greet` stub without implementing anything. It should call `coding_list_roots` and search the host path. The scripted `coding-locate` eval (`make tool-eval-reset` then run/score that case) is the same hook.

After enabling coding or changing MCP registration:

```bash
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
docker compose exec hermes hermes mcp list
docker compose exec hermes hermes -p browser mcp list
docker compose exec hermes hermes -p api-server mcp list   # must NOT list opencode
```
