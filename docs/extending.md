# Extending the stack downstream

How another app can run this stack under its own names, ports and data paths,
and which parts of `docker-compose.yml` it can rely on across upgrades.

Pin a release tag, not a branch. [CHANGELOG.md](../CHANGELOG.md) lists each
release's new services, required `.env` keys and new host requirements.
[contracts/](contracts/README.md) describes each category the stack is built
from (models, tools, agent, credentials, secrets, storage, search) as an
interface and its current implementation.

## Host tools

`scripts/setup.sh`, `scripts/crm.sh init` and `scripts/todo.sh init` need
bash, the tools macOS and Linux ship in `/bin` and `/usr/bin` (`grep`, `sed`,
`awk`, `curl`, `openssl`, `ssh-keygen`), and the container engine. They need
no `python3`, `node` or `make` on the host: their Python runs in the pinned
Hermes image, and the n8n credentials step runs Node in the n8n image. Both
images are pulled anyway, so the first such step may spend a few minutes
pulling. Run the scripts with `bash` directly, as the `make` targets do.

Each container runs as the calling user, with the repo mounted at its own host
path. `STACK_PYTHON=host` runs the host's `python3` instead and `N8N_NODE=host`
the host's `node`, for development and tests. `scripts/test-no-host-python.sh`
checks the first-run scripts for host `python3` and `node` calls, and runs the
container path with them removed from `PATH`.

Other scripts still use the host's `python3`: `make doctor`, `coding-root.sh`
and `context-root.sh`, the mail and Messages tools, Buzz agent management, and
the eval and corpus scripts.

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
| `hermes` | `/opt/uv-cache` | Shared uv cache (`UV_CACHE_DIR`), named volume `hermes_uv_cache`; `HERMES_UV_CACHE_DIR` swaps in a host path |
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

If the same folder is also mounted at its host path as a coding root, list
the pair in `CODING_PATH_ALIASES` so Hermes can delegate coding work in that
project without guessing the host path. See
[OpenCode](opencode.md#project-folder-aliases).

### App-managed roots

An app that adds coding or context roots from its own UI should set these in
its `.env`, so Hermes does not tell users to run `make` targets they cannot
run:

| Key | Effect when `1` |
|---|---|
| `CODING_ROOTS_APP_MANAGED` | The `needs_access` hint from `coding_start_task` says the app will ask the user to grant access to the path. Hermes names the folder and ends the turn. |
| `CONTEXT_ROOTS_APP_MANAGED` | The Hermes system prompt says the app will ask when a context root is missing, instead of naming `make context-root-add`. |

Both are empty by default, and a developer stack keeps the `make` text. The
`needs_access` result still carries `path` and `roots` for the app to build
its prompt from. Recreate `opencode-mcp` and `hermes` after changing them.

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
| `POST /admin/reload` | Re-read key files and the fallbacks file; returns `{"reloaded": [...], "upstreams": {...}}` |

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
{"chat": {"url": "https://openrouter.ai/api/v1", "api_key_set": true, "pushed": true, "fallbacks": []},
 "lightrag": {"url": "https://openrouter.ai/api/v1", "api_key_set": true, "pushed": true, "fallbacks": []},
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
- A `lightrag` route with an empty `url` uses the `chat` route, fallbacks
  included.

These paths, route names and fields are a stable interface. New fields may be
added. Removing or renaming one, or changing what it means, gets a changelog
entry under a new release before it ships.

### Fallback upstreams

The `chat` and `lightrag` routes can each carry an ordered list of fallbacks,
tried after the primary. The proxy moves to the next one when an upstream
refuses the connection, cannot be reached within `LLM_CONNECT_TIMEOUT`
(default 10s), or answers 429, 500, 502, 503 or 504. When the last one fails
too, the client gets that failure: its status, or a 502 if it was unreachable.
`LLM_CONNECT_TIMEOUT` applies only to an upstream with another after it. The
last one, like a route with no fallbacks, can take up to `LLM_TIMEOUT` to
connect.

The proxy does not move on when an upstream accepts the connection and then
sends no headers within `LLM_TIMEOUT`. That wait covers prompt prefill, so it
runs out on a slow model as often as on a broken one. Once the response has
started, nothing is retried, and a failure drops the stream as before.

Each entry has `url`, and optionally `api_key` and `model`. An empty or
missing key means the upstream needs none. With `model` set, the request's
`model` is replaced for that upstream. Without it the client's model name is
sent as is, which a different provider will usually reject with a 4xx, and a
4xx is not retried. `embed` has no fallbacks: vectors from another embedding
model would land in the same LightRAG index and break retrieval.

Set them with the admin API or a file. In a POST, `fallbacks` replaces the
route's whole list, `[]` clears it, and leaving it out keeps it. A bad list
gets a 400 and changes nothing. `GET` shows each entry's `url`, `model` and
`api_key_set`, never the key.

```json
{"chat": {"fallbacks": [{"url": "https://openrouter.ai/api/v1", "api_key": "sk-...",
                         "model": "qwen/qwen3-32b"}]}}
```

`LLM_PROXY_FALLBACKS_FILE` names a container path to a JSON file of the same
lists, `{"chat": [...], "lightrag": [...]}`. Mount it the way you would a key
file. It is read at start, and again on SIGHUP and `POST /admin/reload`; a
route the file leaves out then has no fallbacks. An unreadable or invalid
file is logged and leaves the current lists in place (at start, none).
Reloading the file is not a push, so it does not release a route waiting
under `LLM_PROXY_AWAIT_PUSH`.

Every proxied response carries `X-LLM-Proxy-Upstream: <route>;<index>`, where
index 0 is the primary and 1 the first fallback. When the upstream sends that
header itself, its value is forwarded instead. The proxy log shows the same on
each request line (`POST /v1/chat/completions 200 upstream=chat;1`) and logs
each attempt it moved past, with the upstream's host and the reason.

A cloud fallback behind a local model sends prompts and context to that
provider whenever the local server is down.

### Per-model request defaults

`LLM_PROXY_MODEL_DEFAULTS_FILE` names a JSON file of request fields per model
id, which the proxy adds to generation requests that leave them unset. Compose
points it at `model-defaults/local.json`; an empty value turns it off.
`GET /admin/model-defaults` shows what is loaded, and SIGHUP and
`POST /admin/reload` re-read it. See [Model defaults](model-defaults.md).

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
- `model.max_tokens`: bootstrap writes 16384 (`HERMES_MAX_TOKENS`) where a
  profile has none. llm-proxy adds the same default to requests that arrive
  without one (`LLM_DEFAULT_MAX_TOKENS`), so a runaway generation stops
  either way, but setting it in the profile lets Hermes size its own retries.
- `model.context_length`: Hermes cannot read the window from a placeholder
  model name and falls back to 256k. Set it to the context length the model
  server actually loads, so compression starts in time.
- **Profiles your app creates after bootstrap** get none of the settings
  above. Write them yourself, or re-run both bootstrap services after
  creating the profile.
- **Kanban tasks**: Hermes has no board-wide runtime cap, and a worker keeps
  heartbeating during a long model call, so the dispatcher sees it as
  healthy. Set `max_runtime_seconds` on every task a local model runs. When
  the dispatcher kills the worker, llm-proxy closes the upstream request, so
  the model server stops too.
- Leave `agent.tool_use_enforcement` at `auto` and `agent.execution_guidance`
  unset. Forcing them on helps some chatty models but makes weaker or
  quantized ones fake tool calls in text more often
  ([hermes.md](hermes.md)).

## API base URLs

`http://localhost:8643/v1` (api-server profile) works under either
[gateway topology](hermes.md#gateway-topology). Under
`HERMES_GATEWAY_TOPOLOGY=multiplex`, the same API is also at
`http://localhost:8642/p/api-server/v1`, with the same key; 8643 forwards
there. Upstream Hermes is moving to the multiplexed layout and will drop the
per-profile listeners, so an app can move its base URL to the `/p/api-server`
form once its install runs `multiplex`. 8643 stays until a release announced
here removes it. The dashboard's `/api/ws` on 9119 does not change.

## Gateway turn liveness

A client on the dashboard gateway WebSocket can watch a running turn with the
`turn.heartbeat` event and ask about one session with `session.live_status`.
`gateway.capabilities` says whether both are on. Fields and semantics are in
[hermes.md](hermes.md#turn-liveness).

This is an overlay on the pinned Hermes image (`patch-gateway-liveness.py`). The
event, method and fields stay stable across pin moves. Changes are additive.

## Speaker names

`speaker` on `prompt.submit` names the agent answering a turn in a session
several agents share. Its assistant rows carry `display_metadata.speaker` in
`session.history`, and later turns show the model who wrote each reply.
`gateway.capabilities` reports `turn_speaker`. Fields, labels and error codes
are in [hermes.md](hermes.md#speaker-names).

This is an overlay on the pinned Hermes image (`patch-turn-speaker.py`). The
field, the `display_metadata.speaker` shape, the error codes and the capability
stay stable across pin moves. Changes are additive.

## Kanban workspaces

The dispatcher builds a task's workspace in the `hermes` container. With the
default `HERMES_TERMINAL_BACKEND=ssh`, `/opt/projects` is read-only there and
your coding roots aren't mounted, so a `worktree` task on one of those repos
can't start, and neither can a task linked to a project whose repo is there.
File such tasks with `workspace_kind="scratch"`; the worker's terminal runs
on `hermes-worker`, where those repos are writable.

An overlay (`patch-kanban-guards.py`) checks this when a task is filed.
`kanban_create` returns an error and `POST /tasks` returns 400, with the
reason and the fix in the message, for example:

```text
workspace_kind='worktree' needs a workspace_path or a project, and board 'default' has no default_workdir. Pass workspace_kind='scratch', or a project or path in a git repo this container can write to.
```

A task filed before the overlay, or whose repo changed after filing, still
fails when the dispatcher claims it. If the error can't heal (no path, a path
that isn't absolute or isn't in a git repo, or a repo the container can't
write to), the task is blocked on that first claim and the `gave_up` event's
payload has:

| Field | Value |
|---|---|
| `trigger_outcome` | `spawn_failed` |
| `error` | `workspace: ` followed by the reason |
| `sticky` | `true`: only an unblock releases it |
| `retryable` | `false` |

A give-up for a revoked credential or missing model (`trigger_outcome`
`crashed`, `terminal_provider: true`) also has `retryable: false`. Other
give-ups leave `retryable` out; treat a missing key as retryable. When the
task was filed by another task's worker, the creator task also gets a comment
from `kanban-dispatcher` that names the task and its error.

`HERMES_KANBAN_GUARDS=0` turns all of this off. The error text, payload fields
and comment author stay stable across pin moves. Changes are additive.

### Changing a task's workspace

A task that isn't running can get a new workspace without being filed again,
so it keeps its id, runs, comments, attachments and links. Send any of these
on `PATCH /tasks/{id}`:

| Field | Effect |
|---|---|
| `workspace_kind` | `scratch`, `dir` or `worktree`. A new kind drops the stored `workspace_path` unless the PATCH sets one; `scratch` also drops the project link unless the PATCH sets `project_id` |
| `workspace_path` | Absolute path for `dir` or `worktree` |
| `clear_workspace_path` | `true` clears the path. Not together with `workspace_path` |
| `project_id` | Project id or slug; its repo becomes the worktree. `""` unlinks |

The new workspace is checked the way filing checks it. The workspace step
runs before the rest of the PATCH, so a refused change applies none of it.

| Status | Meaning |
|---|---|
| `200` | Changed, or nothing to change. An `edited` event has `fields`, `from` and `to` |
| `400` | The workspace can't be built, the project is unknown, or both path fields were sent |
| `404` | Unknown task |
| `409` | The task is running |

A blocked task stays blocked. Unblock it in the same PATCH (`"status":
"ready"`) or after it. To rerun a task that gave up on its worktree in a
scratch workspace, the body is:

```json
{"workspace_kind": "scratch", "status": "ready"}
```

Orchestrator profiles (the `kanban` toolset named in the profile) get
`kanban_update`, which takes `task_id`, `workspace_kind`, `workspace_path` and
`project_id` (`""` clears the last two), and `kanban_archive`, which refuses a
running task. Like `kanban_unblock`, dispatcher-spawned workers don't see
either. These are an overlay (`patch-kanban-edit.py`); the fields, tool names
and arguments stay stable across pin moves.

### Telling the session that filed a task

A dashboard session that calls `kanban_create` is subscribed to that task.
When the task completes, blocks, gives up, crashes or times out, the session
gets the event as a turn once it is idle, so the agent can act on it. A
`gave_up` turn reads, for a workspace error:

```text
✖ @default Kanban t_5727c92a gave up: it can't start until its workspace or configuration changes
Error: workspace: task t_5727c92a has workspace_kind=worktree but no workspace_path, ...
To repair it: kanban_update(task_id='t_5727c92a', workspace_kind='scratch'), then kanban_unblock(task_id='t_5727c92a').
```

Other give-ups say how many attempts failed and point to `kanban_show`,
`kanban_unblock` and `kanban_archive`. Only a profile with the `kanban` toolset
has those tools.

Turns run only while the session is open in the gateway with its agent built:
after `session.create`, or `session.resume` without `lazy`. While it isn't,
events wait, and the next resume delivers each one once. After the user
presses Stop, they wait for the user's next prompt. The stack never opens a
session by itself, so to have the agent repair a task without the owner,
resume the filing session (the task's `session_id`, with its `profile`) when
`/events` shows a `gave_up` for it. The resume can stay out of view.

Context compression gives a session a new key, and Hermes keys these
subscriptions on the old one. An overlay (`patch-kanban-wake.py`) lets the
session claim them under its new key, so tasks filed before a compression
still reach it. Without it, those events are not delivered.

## Dashboard links

Three overlays let an app send the user to the dashboard's board: a one-time
sign-in link, a link to one task, and a mode without the dashboard's own
navigation. The URLs, parameters and code format stay stable across pin
moves. Changes are additive.

### Sign-in links

`GET /auth/handoff?code=<code>` exchanges a code for a dashboard session and
redirects to the code's `next` path (`patch-dashboard-handoff.py`). The
session is the one a password sign-in gets from the `basic` provider, so it
refreshes and survives a restart the same way. The app never sends the
dashboard password, and no page sees it.

The code is signed with `HERMES_DASHBOARD_HANDOFF_SECRET` from `.env`, which
`setup.sh` generates. Read it the way you read `HERMES_DASHBOARD_PASSWORD`.
Without it, or with a value under 32 characters, the route answers 404.

```text
code      = <payload>.<signature>
payload   = base64url(JSON {"v": 1, "nonce": <16-128 chars>, "exp": <unix seconds>, "next": <path>})
signature = base64url(HMAC-SHA256(key = secret as UTF-8 bytes, message = payload as ASCII))
```

Both parts are base64url without `=` padding. `exp` is at most 60 seconds
ahead (5 seconds of clock difference are allowed). Each nonce is accepted once.
In Node:

```js
const b64 = (buf) => Buffer.from(buf).toString("base64url");
const payload = b64(JSON.stringify({
  v: 1, nonce: crypto.randomBytes(18).toString("base64url"),
  exp: Math.floor(Date.now() / 1000) + 30, next: "/kanban?task=t_5727c92a",
}));
const code = `${payload}.${b64(crypto.createHmac("sha256", secret).update(payload).digest())}`;
const url = `${dashboard}/auth/handoff?code=${encodeURIComponent(code)}`;
```

| Code | Redirect |
|---|---|
| Valid | `next`, signed in. `next` goes through the same check as a password sign-in: a path on the dashboard outside `/login`, `/auth/` and `/api/`, else `/` |
| Used or expired | `/login?next=<next>` |
| Bad signature, malformed, or `exp` too far ahead | `/login` |

Responses carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
The code appears in the dashboard's access log, which is why it works once.
`make dashboard-open NEXT=/kanban` mints one from `.env` and opens it.

An app that loads the board in its own window can skip the link: sign in from
the main process with `POST /auth/password-login` and copy the session cookies
into that window's session.

### Task links

`/kanban?task=<id>` opens the board with that task's drawer open and its card
scrolled into view (`patch-dashboard-board.py`). Add `&board=<slug>` for a
task on a board other than the one the dashboard last showed; the dashboard
keeps its remembered board. An archived task opens too. An unknown id opens
the board as usual and drops `task` from the URL, as closing the drawer does.

### Without the dashboard's navigation

`?embed=1` on any dashboard URL hides the sidebar, the mobile header and the
page header. It lasts for the tab (`sessionStorage`), across navigation and
reloads, until `?embed=0`. Banners such as the memory-pressure warning stay.
The board's own controls, drawer and drag-and-drop are unchanged. Every link
the board renders that leaves it (a comment's links, the docs link) already
has `target="_blank"`.

Hiding the navigation doesn't limit what the session can reach. An app that
shows only the board should refuse navigation away from `/kanban` in that
window (`will-navigate` and `setWindowOpenHandler` in Electron) and open those
URLs in the system browser.

## Keys a new profile leaves out

`profiles.create` on the gateway mirrors the launch profile's `.env` into the
new profile by default (`mirror_credentials`), minus channel bot tokens. That
gives it a model, and also every password saved for an MCP server, such as the
bootstrap's `CALDAV_<SLUG>_PASSWORD` keys and anything stored with
`mcp.servers.set_api_key`.

Pass `mirror_exclude`, a list of glob patterns matched case-sensitively against
key names, to leave keys out:

```json
{"name": "researcher", "mirror_credentials": true,
 "mirror_exclude": ["BOUNDARY_CALENDAR_*", "CALDAV_*_PASSWORD"]}
```

Matching assignments are removed after the channel strip. Comments and other
keys stay as they were. `mirrored.env_excluded` in the result lists the keys
removed, by name only. If the filter fails, including on a value that is not a
list of strings, the new `.env` is emptied and `mirrored.env` is `false`. The
patterns apply only to the mirrored `.env`, not to `clone_from` copies.

This is an overlay on the pinned Hermes image (`patch-mirror-exclude.py`). The
param and result field stay stable across pin moves.

## MCP approvals for untrusted servers

Hermes asks before a tool on a `trust: untrusted` MCP server runs, unless the
server annotates the tool `readOnlyHint: true`. The `approval` request for that
prompt, and its replay in `approval.pending`, carry the call:

```json
{"request_id": "…", "command": "MCP tool 'caldav_create_event' on UNTRUSTED server 'calendar-personal' wants to run. …\n\nArguments:\n{…}",
 "pattern_key": "mcp_elicitation", "choices": ["once", "session", "always", "deny"],
 "mcp": {"server": "calendar-personal", "tool": "caldav_create_event",
         "arguments": {"summary": "Call with Jordan", "start": "2026-10-23T11:00:00",
                       "end": "2026-10-23T11:30:00", "calendar_name": "Work"},
         "tool_call_id": "call_…"}}
```

`tool` is the server's own tool name. `tool_call_id` is the `tool_id` the
call's `tool.start` event carried, or `null` when Hermes had none. The
arguments are redacted before they are stored. Values under a
credential-looking key (`password`, `token`, `api_key`, `authorization` and the
like) become `[REDACTED]`. Every string goes through Hermes' secret redaction,
as command previews do. Strings are capped at 500 characters, lists and
objects at 50 entries, and nesting at 4 levels.

Since Hermes v0.22.7, `command` ends with Hermes' own redacted argument block,
so two different calls get separate prompts. The prompt offers `once`,
`session`, `always` and `deny`. A `session` or `always` answer is remembered for
that server and tool, and later calls to it do not ask. A call that goes past
the chat's spend limit, and a prompt only about the spend limit, offer `once`
and `deny`.

This is an overlay on the pinned Hermes image (`patch-mcp-trust-details.py`).
The `mcp` fields stay stable across pin moves. Changes are additive.

## MCP servers your app hosts

If your app serves tools to Hermes from its own MCP server, Hermes loses that
server whenever the app closes. After 5 failed reconnects it parks the server:
it drops the tools and tries again only every 300 s. Without the settings
below, the tools can be missing for up to 5 minutes after the app comes back.

Set these on the server in every profile that lists it
(`mcp_servers.<name>` in `data/hermes/config.yaml` and
`data/hermes/profiles/<profile>/config.yaml`):

| Key | Suggested | Effect |
|---|---|---|
| `keepalive_interval` | `15` | Hermes setting (default 180, floor 5). A session counts as healthy after one interval. Until then a single drop parks the server again. Also notices the app closing sooner |
| `parked_retry_interval` | `15` | Stack overlay (default 300, floor 10). How often a parked server is tried again |
| `connect_timeout` | leave at 60 | Must outlast Hermes' first 3 attempts (several seconds against a closed port). A shorter value abandons a server that is down when Hermes starts, instead of parking it, and a wake cannot reach it |

To bring the tools back at once, call this after your server is listening:

```bash
curl -X POST -H "Authorization: Bearer $API_SERVER_KEY" \
  http://localhost:8643/v1/mcp/servers/<name>/reconnect
```

Use the `api-server` profile's key (`compose/hermes/api-server.env`). The call
reaches every profile that configures the server, including the default one,
whatever the [gateway topology](hermes.md#gateway-topology). Under
`multiplex` the same call also works at
`http://localhost:8642/p/api-server/v1/mcp/servers/<name>/reconnect`. The
tools return within about a second.

| Status | Meaning |
|---|---|
| `202` | `{"server", "profiles", "status": "wake_requested", "woken"}`. `profiles` lists every profile whose config names the server. `woken` counts the parked connections the answering gateway process woke at once; other processes wake theirs within a second |
| `404` | No profile configures that name |
| `400` | The name is not `[A-Za-z0-9][A-Za-z0-9_.-]*` (at most 128 characters) |
| `401` | Missing or wrong key |

The call only wakes parked connections and never restarts a connected one, so
calling it on every launch is safe. A request stays live for 30 s, so a server
still on its last retries when you call is woken as soon as it parks. Each
connection acts on a request once. If your server was not listening yet, it
parks again: call again, or let `parked_retry_interval` pick it up.

If your app is closed when Hermes starts, the gateway's startup discovery
spends Hermes' 3 initial attempts (about 8 s) on your server for the first
profile that lists it. Discovery runs one profile at a time, and the gateway
serves no profile until it finishes. Every other profile that lists the same
name and `url` (or `command` and `args`) in the next 120 s parks it after one
failed attempt. They park the same way, so the reconnect call and
`parked_retry_interval` revive all of them.

This is an overlay on the pinned Hermes image (`patch-mcp-wake.py`). The route,
key and response fields stay stable across pin moves. Changes are additive.

## Add-ons: scripts/addon.sh

Boundary lists, configures, enables and disables built-in add-ons
(`addons/<id>/addon.json`, see [addons/README.md](../addons/README.md))
through `scripts/addon.sh`. It needs bash and the container engine; the JSON
and file work runs in the Hermes image. The contract is experimental in
0.3.x. Field names and output shapes can change between patch releases, with
a changelog entry.

| Command | Does |
|---|---|
| `list --json [--sizes]` | Prints a JSON array, one object per add-on, sorted by short id. Takes no lock |
| `configure <short-id> < values.json` | Reads one JSON object on stdin and stores the settings. Prints nothing |
| `configure <short-id> --instance <slug> < values.json` | Same for one instance. Values you omit keep their stored value |
| `configure <short-id> --instance <slug> --remove` | Deletes the instance's file and secrets |
| `enable <short-id> [--reinit] [--json]` | Checks required settings, sets the add-on's flag and gate flags in `.env`, syncs compose profiles, starts the services whose gate is on, runs the `init` hook once, reloads Hermes |
| `disable <short-id> [--purge] [--json]` | Sets the flag and every gate flag to `0`, stops the services, reloads Hermes. A gate flag that is on in `.env` while its `bool` setting isn't stored (set by hand) gets the setting stored as `true` first, so the next `enable` turns it back on. `--purge` then does what `remove --delete-data` does, under the same lock |
| `connect <short-id> --instance <slug> [--force] [--json] < values.json` | Checks the values, runs the manifest's `instances.connect` hook, stores the instance, reloads Hermes. `--force` replaces an existing instance; see [Replacing an instance](#replacing-an-instance) |
| `disconnect <short-id> --instance <slug> [--json]` | Runs `instances.disconnect`, removes the instance, reloads Hermes |
| `remove <short-id> --delete-data [--json]` | Deletes the add-on's containers, compose volumes and data. Refused while the add-on is enabled |
| `run <short-id> <command> [--instance <slug>]` | Runs a manifest `commands` entry in its service with `docker compose run --rm --no-deps -T`, after building the service as described below the table |

Every service built from this repo (one with a `build` section in
`docker-compose.yml`) is built with `docker compose build --quiet` before
`addon.sh` runs it, so a checkout never runs an image older than its source:
in step `start` of `enable` before the one-shots and the services start,
before a hook that runs in a compose service, and before `run`. An unchanged
build is a cache hit that takes a second or two. Compose's build output goes
to stderr, or the step log with `--json`, only when the build fails; a failed
build is `compose_failed` at step `start` for `enable` and exit 4 for `run`.
Dependencies aren't built, so `hermes` is never rebuilt this way.

`list --json` objects carry `id`, `shortId`, `title`, `tagline`,
`permissions`, `platform`, `enabled` (the flag in `.env`), `settings`,
`instances` (slug to values), `services`, `missing`, `data`, `backup`,
`hooks`, `errors` and `schema`, plus `postInstallMessage` (Markdown from
the manifest) when the manifest has a non-empty one. `missing` lists what
`enable` would refuse on: a top-level setting by name, an instance setting
as `<slug>.<name>`, and `instances:<name>` (the manifest's `instances.name`)
when the add-on needs an instance and has none. A manifest with
`instances.optional: true` never lists `instances:<name>` (Twenty sets it). `hooks` names the
hooks the manifest declares, `errors` is its `instances.errors`, and `data`
and `backup` are copied from the manifest. A setting shows its stored
value, else its default, else is absent. A `secret` setting shows only
`{"set": true}` or `{"set": false}`. A manifest that fails validation is left
out with a warning on stderr.

`services` has one object per manifest service, sorted by key: `key`,
`compose`, `oneshot` (another compose service depends on it with
`condition: service_completed_successfully`, so it runs to completion), `image` and `memory` (`{"reservation", "limit"}` in bytes), with
`null` where compose gives no value, and `downloadSize` with `--sizes`.
`image` and `memory` come from `docker compose --profile '*' config
--format json`, which reads every service and starts nothing. Memory is taken
from `deploy.resources`, then `mem_reservation` and `mem_limit`. If `config`
fails, the catalog prints without those figures and one warning goes to
stderr. `--sizes` also runs `docker manifest inspect --verbose` once per
distinct image (a registry call) and sums the layer sizes for the engine's
platform in bytes. An image with no matching entry has no `downloadSize`.

`schema` is the manifest's settings schema, for building a settings form:
`{"settings": {<name>: <spec>}, "instances": {"name": ..., "settings":
{<name>: <spec>}}}`, with `instances` set to `null` when the manifest declares
none. Each spec is copied from the manifest (`type`, `label`, `default`,
`optional`, `flag`, `options` and any other field it sets). A `secret`
setting can't declare a `default`, so the schema holds no secret value.

Values on `configure` must be JSON-native: numbers as numbers, bools as bools,
lists of strings for `list`, `YYYY-MM-DD` strings for `date`. Secrets are
accepted only in that JSON on stdin, never as arguments or environment.
Sending `""` for a secret clears it, and it then counts as unset. A setting
name the manifest doesn't declare exits 2. Plain settings go to
`data/addons/<short-id>/config/settings.env`. A `bool` setting that names a
`flag` (always a gate flag) also writes that flag to `.env`, but only while
the add-on's own flag is on; while the add-on is disabled the value is stored
and the next `enable` writes the flag from it. A gate flag changed through
`configure` takes effect at the next `enable`, so call `enable` after
`configure`. Instance `configure` never writes flags.

`configure` takes the same lock as `enable` (see [Lock and
rollback](#lock-and-rollback)), so a call made while another change runs fails
with `busy`.

```text
data/addons/<short-id>/
  config/settings.env     ADDON_<SHORT>_<NAME>=value, no secrets
  instances/<slug>.json   non-secret instance values
  secrets/<name>          0600; instance secrets are <slug>.<name>
```

Directories are 0700. `config/` is created with the add-on directory, before
any setting is stored. A service whose manifest lists `mounts` gets
`config/`, `instances/` and `secrets/` at `/run/addon/config`,
`/run/addon/instances` and `/run/addon/secrets`, read-only or read-write as
listed.

`settings.env` holds one `KEY=value` line per stored setting. `KEY` is
`ADDON_<SHORT>_<NAME>`: the short id and the setting name, uppercased, with
`-` turned into `_`. Values are quoted with Python's `shlex.quote`, so read
the file with `shlex.split`, not compose
`env_file` or `docker --env-file`, which don't unquote. A `bool` is `1` or
`0`, a `list` is a JSON array, and `int`, `port` and `date` values are
written as text.

An `mcp` entry with `instances` registers one `mcp_servers.<name>` per stored
instance, so an add-on whose users connect several
accounts or workspaces needs no fixed service per instance. The entry gives
`name` as a prefix plus `{slug}` (`crm-{slug}`), and `url` as a template with
`{setting:<name>}`, `{access}` and `{slug}`. Optional: `access` naming a
`choice` setting of `read` and `write` that caps the level, `auth` as
`{header, scheme, secret: {ro, rw}}` naming two `secret` settings, and
`gate: {setting, flag}`: an instance whose bool `setting` is true registers
only while `flag` is on.

Each profile gets an instance at `ro` or `rw`: the instance's `bind` when it
has one, else `mcp.profiles`. `bind` is a map from profile to `ro` or `rw`
that `connect` and `configure --instance` accept in the JSON on stdin beside
the values, for example `{"bind": {"default": "rw", "browser": "rw",
"api-server": "ro"}}`; `rw` is refused on `api-server`. With `auth`, the
bootstrap writes the key the level picks into each profile's `.env` as
`<NAME>_KEY` (`CRM_SALES_KEY`), and an instance missing that key gets no
entry, with the reason in the bootstrap log. Without `auth`, the level fills
`{access}` and picks the tool list. Entries, keys and pins of an instance that
was removed are dropped. Field rules and the prefix rule across add-ons are in
[addons/README.md](../addons/README.md); Twenty ([docs/crm.md](crm.md)) and
Actual ([docs/budget.md](budget.md)) use it.

`disable` keeps `data/addons/<short-id>/`; `disable --purge` deletes it, as
described under [Removing data](#removing-data).

Errors are one line, `error: <message>`, on stderr in both modes. The message
names the setting and never its value. A usage error starts with
`error: usage:` and the synopsis follows on the next lines.

| Exit | Meaning |
|---|---|
| `0` | Done |
| `1` | The data root is invalid, a compose step failed, another add-on change holds the lock (`busy`), or the file work failed (for example, a permission error); the error line names the exception type |
| `2` | Invalid input: unknown add-on, bad JSON, unknown or ill-typed setting, bad slug, unknown command or instance, usage error. Also `connect` without `--force` on an existing instance and `disconnect` on an unknown one |
| `3` | `enable` found required settings with no value |
| `4` | `run`: `docker compose run` failed, whether the command exited non-zero or the container couldn't start. Also a failed hook, `vault.sh reload-plugins`, or a SIGINT or SIGTERM |

### JSON lines

`enable`, `disable`, `connect`, `disconnect` and `remove` take `--json`. Then
stdout carries one JSON object per line and nothing else, and the run ends
with exactly one `done` or `error` line. Without `--json` those commands print
their step output to stderr and nothing to stdout.

```json
{"phase":"pull","service":"gptr","current":1048576,"total":52428800}
{"phase":"setup","step":"start","status":"running"}
{"phase":"setup","step":"start","status":"done"}
{"phase":"done","id":"gptr","action":"enable","restart":[],"mcp":["gpt-researcher"]}
```

`pull` lines come from `enable` only. It pulls each service's image first with
`--policy missing --ignore-buildable`, so a local-only image or an offline
machine still enables and an updated tag isn't fetched. Images built from
this repo are built in step `start` instead. A `pull` line carries
bytes summed over layers, at most one per service per second, and ends with
`{"phase":"pull","service":<name>,"status":"done"}`.

A `setup` line is `{"phase":"setup","step":<name>,"status":"running"|"done"|"error"}`.
A step that runs per service, volume or path adds `"target"`. Steps run in this
order: `flags` (write `.env`, sync `COMPOSE_PROFILES`), `start` (one-shots,
then the services), `stop` (gated services whose gate is off), `init`,
`reload`. `disable` runs `flags`, `stop`, `reload`, then `delete` with
`--purge`. `connect` and `disconnect`
run `check` (the hook), `register` (store or remove the instance) and `reload`.
`remove` runs `delete`.

`done`:

```json
{"phase":"done","id":"<short id>","action":"enable","restart":[],"mcp":["name"],"account":"<slug>","message":"<text>","result":{"key":"value"}}
```

`restart` is always empty since 0.3.14 and stays for clients that read it. On `enable`, `mcp`
is the `name` of each manifest `mcp` entry whose gate is on, followed by the
names the `init` hook's `done` line reported in its `mcp`, each name once.
On `connect` it is the names the `connect` hook reported. `account` appears
only when a hook reported one. `message` appears only when the `init`,
`connect` or `disconnect` hook's `done` line has a string `message`; it is
cleaned like the strings in an `error` line, and on `connect` each staged
secret value in it is `[redacted]`. `result` appears on `enable` and
`connect` when the `init` or `connect` hook's `done` line has a `result`
object with at least one value kept (see [Hooks](#hooks)). `replaced: true`
appears when `connect --force` replaced an existing instance. An `enable` that
skips a recorded `init` gets neither the hook's names, its message nor its
result.
`addon.sh` does not wait for MCP servers. The caller waits until each name in
`done.mcp` is up in `mcp.servers.status`.

`error`:

```json
{"phase":"error","error":"compose_failed","step":"start","message":"...","logTail":["..."],"rolledBack":true}
```

`step` is `null` when the failure came before any step. `logTail` holds up to
20 lines of the failing step's output. `rolledBack` appears when `enable`
restored the flags. `target` names the service, volume or path when the
failure has one. Every line is valid JSON: control characters other than tab
are dropped, a tab becomes a space, and a log line is cut to 500 characters.

| `error` | Exit | Cause |
|---|---|---|
| `bad_input` | 2 | Invalid input, or a rule refused the run (`remove` while enabled, or on Linux a `HERMES_UID` or `HERMES_GID` that isn't the caller's when the add-on has a service that declares `mounts` or runs as `HERMES_UID` in compose) |
| `missing_settings` | 3 | `enable` found required settings with no value |
| `compose_failed` | 1 | A `docker compose` call failed (pull, start, stop, recreate, delete, a bootstrap) |
| `busy` | 1 | Another change holds the lock |
| `command_failed` | 4 | A hook, `vault.sh reload-plugins` or `addon_cli.py` failed, the run was signalled, or `addon.sh` exited unexpectedly (`enable` rolls back first). Exit 1 when the data root is invalid |
| `exists`, `not_found` | 2 | `connect` without `--force` on an existing slug, `disconnect` on an unknown one, detected by `addon.sh` |
| a hook's own name | 4 | See [Hooks](#hooks) |

### Lock and rollback

`configure`, `enable`, `disable`, `connect`, `disconnect` and `remove` hold
`data/addons/.lock` while they run. It is a symlink whose target is the
holder's PID, so it is created together with its owner. A second call while the
holder is alive fails at once with `busy`. A lock whose process has died is
taken over under a guard directory, `data/addons/.lock.takeover`; a guard
older than 10 seconds is treated as left by a takeover that died. `list` and
`run` take no lock.

An `enable` that fails or is interrupted (SIGINT, SIGTERM) restores each flag
it changed to its earlier `.env` line, syncs `COMPOSE_PROFILES` and, if the
add-on was off, stops its services. The `error` line then has
`"rolledBack":true`. Anything the run did that isn't a flag, such as an
`init` hook that already ran, stays. `disable`, `connect`, `disconnect` and
`remove` don't roll back, and a rerun converges: `enable` and `disable` write
the flags first, so a run that fails at a later step leaves them written.

### Reloading Hermes

Step `reload` runs `hermes-api-bootstrap` and `hermes-browser-bootstrap` with
`HERMES_BOOTSTRAP_FORCE=1`, then `scripts/vault.sh reload-plugins`. It never
recreates `hermes` and never calls `make`. Plugin changes, such as Bitwarden's
tools, reach new sessions only. A bootstrap failure is `compose_failed`; a
`reload-plugins` failure is `command_failed`.

### Hooks

A manifest declares `hooks.init`, `instances.connect` and `instances.disconnect`.
A hook is `{"script": [argv]}`, run from the repo root on the host, or
`{"command": <name>}`, one of the manifest's `commands`, run in its service
with `docker compose run --rm --no-deps -T`. `addon.sh` appends `--json`, and
`--slug <slug> --json` for connect and disconnect (`--slug <slug> --replace
--json` for a connect that replaces an instance).

- `init` runs once, in `enable`. `data/addons/<short-id>/state.json` records the
  manifest version and time after it succeeds. `enable --reinit` runs it again.
- `connect` reads the staged instance values on stdin. A secret setting marked
  `"transient": true` in `instances.settings` is passed to the hook and never
  stored: it gets no file under `secrets/` and no key in the instance JSON,
  and `missing` skips it. A secret value containing a CR or LF is refused.
  Without an `instances.connect` hook, `connect` only stores the
  instance.
- `disconnect` runs with stdin `/dev/null`.
- `instances.optional: true` lets `enable` succeed with no instance, so
  `instances:<name>` never appears in `missing`.

Only stdout is read. A hook prints JSON objects, one per line. An object whose
`phase` is `setup` or `pull` is relayed inside the step, with `step` set to
that step and every string cleaned by the rule for `error` lines.
Its `done` line may carry `account` (a string), `mcp` (a list of names) and
`message` (a string) that go into the run's `done` line. An `init` or
`connect` hook's `done` line may also carry `result`, an object. `addon.sh`
keeps its first 32 entries whose key is a letter followed by up to 63
letters, digits or `_` and whose value is a string (cleaned like the other
strings), a finite number or a bool, and drops the rest. On `connect` its
strings are redacted like the hook's other output; an `init` hook's are not,
so don't put a secret there. Anything else on
stdout, and all of stderr, goes to the step log. On `connect`, the staged secret values are
replaced with `[redacted]` in the step log, `logTail` and relayed lines.

A hook fails its step when it exits non-zero, or when it prints an `error`
line, even with exit 0. The `error` line's name passes through, with exit 4,
when it is a shared name (`bad_slug`, `exists`, `not_found`, `login_refused`,
`token_expired`, `no_access`, `timeout`, `server_unreachable`) or in the
manifest's `instances.errors`. Any other name becomes `command_failed`, and the
original name is in `message`. A hook that exits non-zero without an `error`
line is `command_failed`.

### Replacing an instance

`connect --force` on an existing slug runs the `connect` hook with `--replace`
after `--slug <slug>`. The hook checks the new values and replaces what it
stores for the instance only once its checks pass. `addon.sh` then rewrites the
instance from the new values alone: settings left out fall back to their
defaults, and stored secrets left out are deleted. If the hook fails, the old
instance stays as it was. If the reload fails after the rewrite, the new
values stay. The `done` line has `"replaced":true`. On an unknown slug,
`--force` is a plain `connect` and the hook gets no `--replace`.

### Removing data

`remove --delete-data` refuses (`bad_input`) while the add-on is enabled.
`disable --purge` is `disable` followed by the same work, under one lock, with
one `done` line whose `action` is `disable`. Step `delete` does, in order:

1. `docker compose rm -f -s` for every service and one-shot.
2. `docker volume rm <project>_<volume>` for each volume in `data.volumes`
   that exists. The project is `COMPOSE_PROJECT_NAME` from the environment,
   then `.env`, else the directory name.
3. Deletion of each `data.paths` directory, then `data/addons/<short-id>`.

A `data.paths` entry is `{"env": <NAME>, "default": "./data/..."}`. Its path
is the value of `NAME` from the environment, then `.env`, else the default,
made absolute against the repo root without resolving symlinks. A path stays,
reported as `{"phase":"setup","step":"delete","status":"done","kept":<path>,"reason":<text>}`,
when it:

- lies outside `./data` and `ASSISTANT_DATA_ROOT`, or is, or holds, either of them;
- is, holds or lies in stack data: `hermes`, `addons` or one of the user-data
  directories under either root, as written or after resolving symlinks;
- has a symlink between the data directory and itself;
- overlaps a path that another enabled add-on declares.

A symlink that is deleted is unlinked, never followed. A path with a control
character is refused.

### Compose project and profiles

`addon.sh` never sets or unsets `COMPOSE_PROJECT_NAME` or `COMPOSE_PROFILES`.
Every `docker compose` call, including both bootstraps, inherits the caller's
environment. `scripts/test-addon-cli.sh` checks this. The one exception to
the compose arguments is `list --json`, which adds `--profile '*'`.

After `disable`, a compose service the manifest's `uses` maps to is stopped unless
`docker compose config --services` still lists it. Only `neo4j` is mapped
today. That command applies the active profiles, so `disable` decides from the
caller's `COMPOSE_PROFILES` when it is exported, and from `.env` otherwise.
**A caller must not export a stale `COMPOSE_PROFILES`**: one that still
holds the profile `disable` just turned off keeps that service listed, and it
is not stopped.
