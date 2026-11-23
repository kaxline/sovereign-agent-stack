[← Back to README](../README.md)

# OpenCode agent

Isolated AI coding agent with a browser UI and an OpenAPI server, backed by your local model server.

[OpenCode](https://opencode.ai) runs as an isolated AI coding agent, serving a browser UI and an OpenAPI server on the same port, under the **`coding`** profile. It is on by default: setup writes `OPENCODE_MCP_ENABLED=1` when `.env` doesn't have the flag, and asks for `OPENCODE_WORKSPACE_HOST`. To leave it out, set `OPENCODE_MCP_ENABLED=0` and run `make up`. `scripts/addon.sh enable opencode` and `disable opencode` set the same flag.

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

The Hermes terminal backend (`hermes-worker`, or `hermes` with the local backend) already hides this checkout's `data/`: it mounts the empty read-only `compose/hermes/worker/masked` over `$ASSISTANT_STACK_DIR/data`, which `make ensure-local` records. That covers Hermes' `state.db`, profile `.env` files and `SOUL.md` files, for that service only. Another stack checkout under a coding root isn't covered; `make doctor` names any it finds within four levels, and you hide each one the same way under that service in `docker-compose.override.yml`.

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

With the `coding` profile, Hermes can run coding jobs through OpenCode without opening the OpenCode UI. `OPENCODE_MCP_ENABLED=1` registers an `opencode` MCP server on the **default** and **browser** profiles (not `api-server`).

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
# then recreate so the binds and HERMES_WRITE_SAFE_ROOT apply:
docker compose up -d --force-recreate hermes-worker hermes opencode opencode-mcp
# leave out hermes-worker when HERMES_TERMINAL_BACKEND=local
```

`make coding-root-remove DIR=...` takes an extra root out of `CODING_EXTRA_ROOTS` and drops its binds from both services; recreate the same containers afterwards. The folder stays, and so do any shadow mounts you added under it.

#### Synced roots

A client that keeps a copy of one of its own folders on this host (Boundary running the stack on a remote host over SSH) uses a synced root: a folder the stack creates and binds at the client's path, which doesn't exist on the host.

```bash
scripts/coding-root.sh add --container-path /Users/you/code/app --id ID --json
scripts/coding-root.sh list --json [--no-size]
scripts/coding-root.sh remove --id ID [--delete-folder] --json
```

`add` creates `<data root>/coding-roots/ID/` with mode `2775`, binds it at `--container-path` on the terminal service and `opencode`, adds the container path to `CODING_EXTRA_ROOTS` and records the pair in `<data root>/coding-roots/roots.json`. It prints the host folder and the services to recreate, the same four as above. A second `add` with the same ID and path changes nothing and prints the existing root; the same ID with another path is refused. The container path must be absolute, without a trailing slash, `..`, `:` or `,`, and is refused when it is inside or above the primary root, another coding root or a context root, inside the stack folder or the data root, or under a path the containers use (`/opt`, `/etc`, `/usr`, `/tmp`, `/var`, `/home/opencode` and the like).

On Linux the folder belongs to `HERMES_UID:HERMES_GID`, so the coding agent and the user who runs `add` (the SSH user the client syncs as) can both write it. `add` refuses with `owner_mismatch` when that user isn't `HERMES_UID`. `remove --id` takes the root out of `CODING_EXTRA_ROOTS`, the binds and the registry; the folder stays unless `--delete-folder`. An unknown ID exits 0 with `"removed": false`. `scripts/backup.sh` leaves `coding-roots/` out: the client holds the original.

`make up` re-projects the roots onto the terminal service on every start and reads the registry for synced ones, so it never binds the container path at its own path on the host.

#### Changes in a synced root

A client that syncs a synced root asks which files changed on the host since it last looked:

```bash
scripts/coding-root.sh changes --id ID [--since CURSOR] [--exclude-dir NAME]... --json
```

Nothing watches the folder between calls. Each call stats every entry, compares `(size, mtime_ns, ctime_ns, inode)` with the manifest the cursor names, hashes (SHA-256) only the files whose stat moved, and stores a new manifest under a new cursor at `<data root>/coding-roots/ID.manifest/CURSOR.json.gz`. The two newest manifests are kept, so a client whose last reply was lost can repeat the call with the cursor before it. ctime and the inode catch a rewrite that keeps the size and lands within the mtime's resolution.

```json
{"phase":"done","id":"ID","cursor":"9f…","since":"4c…","reset":false,"full":false,
 "files":[{"path":"src/app.ts","size":812,"mtimeNs":1795440819781423576,"sha256":"98ea…"},
          {"path":"current","size":7,"mtimeNs":1795440819787413571,"link":true,"target":"v2/main"}],
 "deleted":[{"path":"src/old.ts"}],"unreadable":[]}
```

- Without `--since`, `files` lists every file and `full` is true. With a cursor the stack no longer has, it does the same and sets `"reset": true`. With a known cursor, `files` holds what was created or changed and `deleted` what is gone. A rename is a delete and a create.
- Paths are relative to the root and `/`-separated. Symbolic links are listed with `link: true` and their `target`, without a hash, and are not followed. Other special files (sockets, FIFOs) are left out.
- `--exclude-dir NAME` skips every directory with that name, at any depth. Pass the same names on every call: a file the manifest holds that a new exclude now hides shows as deleted. Boundary should pass `node_modules`, `.git` (unless it syncs it) and its build output folders.
- A file written while it is hashed is listed with its new size and mtime and `"sha256": null`, and listed again with its hash on the next call.
- `unreadable` names entries that couldn't be listed or read (permissions, or a name that isn't UTF-8). Such an entry keeps its last listed state, so a folder that is briefly unreadable doesn't show its files as deleted; a name that isn't UTF-8 is never listed.
- An unknown ID or a malformed cursor is `bad_input` (exit 2); a missing folder is `command_failed` (exit 4). Without `--json` it prints `created`, `changed`, `listed`, `deleted` or `unreadable` and a path per line, then `cursor<TAB>CURSOR`.

`remove --id ID --delete-folder` deletes the manifests too. Measured on macOS (APFS, 10 cores, host python3) with `scripts/test-coding-root-changes.py --timing`: a full listing of 50,000 small files with no manifest took 1.8 s end to end, and 0.4 s once a manifest existed. Linux hosts haven't been measured.

#### Applying a root change

`--apply` on any `add` or `remove` form recreates the four services as well, from the stack folder with `docker compose up -d --wait --force-recreate`, so `COMPOSE_FILE`, the project name and the override resolve as they do for `make up`. Recreating `opencode-mcp` loses its task table, so a coding task it was running can't be resumed or fetched with `coding_get_task_result`, and recreating `hermes` ends any chat turn in progress. Before it changes anything, `--apply` asks for both:

- running coding tasks, from `opencode-mcp`'s `GET /tasks` (compose network only, like `/health`): `{"ok": true, "tasks": [{task_id, status, directory, started_at}]}` for tasks that are pending or running, with `started_at` in UTC ISO 8601;
- live chat turns, from the dashboard gateway's `session.live_turns` ([turn liveness](hermes.md#turn-liveness)), which `scripts/lib/live_turns.py` calls from inside the `hermes` container: `{session_id, status, turn_started_at, idle_s}` per turn, nothing else.

If either list is non-empty, or a running `opencode-mcp` doesn't answer, `--apply` stops with error `busy` (exit 1), writes nothing, and adds `"tasks"`, `"liveTurns"` and `"liveTurnsReason"` to the error line. A stopped `opencode-mcp` counts as no tasks. Live turns that can't be read don't block: `liveTurns` is `null` and `liveTurnsReason` says why (`hermes is not running`, or `hermes did not answer session.live_turns` for a gateway without the method). `--force` recreates anyway. The done line then carries `"recreated"` (the services, or `[]` when the change was a no-op such as an unknown ID or an already covered path), `"interrupted": {"tasks": [...], "liveTurns": [...]}`, what was running when the recreate started, and `"liveTurnsReason"`:

```json
{"phase":"done","id":"ID","hostPath":"…/coding-roots/ID","containerPath":"/Users/you/code/app","created":true,
 "recreate":["hermes-worker","hermes","opencode","opencode-mcp"],
 "recreated":["hermes-worker","hermes","opencode","opencode-mcp"],"interrupted":{"tasks":[],"liveTurns":[]},
 "liveTurnsReason":null}
```

A repeated synced `add --apply` recreates again, so a client can make the change first and apply it later. If compose fails, the change stays written and the error is `compose_failed` with compose's last lines in `logTail`. The plain `add DIR` and `remove DIR` forms print `{"phase":"done","path":DIR,"changed":true|false,"recreate":[...]}` with `--json`.

OpenCode runs as `HERMES_UID:HERMES_GID`, not root, so on a Linux host the files it writes in a coding root belong to the user who installed the stack, as the files `hermes-worker` writes do. Its entrypoint starts as root only to give that uid a passwd entry and hand it the state volumes, then drops to it.

If the user asks to work outside those roots, `coding_start_task` returns `needs_access: true` with a `hint`. Hermes follows the hint: it asks the user to confirm access and gives them `make coding-root-add DIR=...` and the containers to recreate. It does not remount Docker itself.

An app that embeds the stack and grants roots in its own UI sets `CODING_ROOTS_APP_MANAGED=1`. The hint then says the app will ask the user to grant access to the path, and Hermes names the folder in one sentence and ends the turn without suggesting commands. `CONTEXT_ROOTS_APP_MANAGED=1` does the same for missing context roots. See [Extending the stack](extending.md#app-managed-roots).

### Project folder aliases

A project folder can be visible to Hermes at `/opt/projects/<slug>` and also be a host folder under a coding root. `CODING_PATH_ALIASES` lists those pairs, separated by `;`:

```bash
CODING_PATH_ALIASES=/opt/projects/foo=/Users/you/code/foo;/opt/projects/bar=/srv/bar
```

`coding_start_task` translates an aliased path, or any path under it, to the host path before checking the roots, so Hermes can pass the project's own path. `coding_list_roots` returns the pairs as `aliases`, and the Hermes system prompt lists them when set. An alias does not grant access: the host path must still be under a coding root. Malformed entries are logged and ignored. Recreate `opencode-mcp` and `hermes` after changing it.

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
make hermes-reload
docker compose exec hermes hermes mcp list
docker compose exec hermes hermes -p browser mcp list
docker compose exec hermes hermes -p api-server mcp list   # must NOT list opencode
```
