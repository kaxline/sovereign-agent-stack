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
{"request_id": "…", "command": "MCP tool 'caldav_create_event' on UNTRUSTED server 'calendar-personal' wants to run with {…}. …",
 "pattern_key": "mcp_elicitation", "choices": ["once", "deny"],
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
objects at 50 entries, and nesting at 4 levels. The same summary, capped at 400
characters, goes into `command`. Two different calls therefore get separate
prompts, where identical text used to make the second wait on the first and
take its answer.

These prompts offer only `once` and `deny`. `session` and `always` were
accepted as a single approval and never remembered.

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
reaches every profile, including the default one, which has no API server of
its own. The tools return within about a second.

| Status | Meaning |
|---|---|
| `202` | `{"server", "profiles", "status": "wake_requested"}`. `profiles` lists every profile whose config names the server |
| `404` | No profile configures that name |
| `400` | The name is not `[A-Za-z0-9][A-Za-z0-9_.-]*` (at most 128 characters) |
| `401` | Missing or wrong key |

The call only wakes parked connections and never restarts a connected one, so
calling it on every launch is safe. A request stays live for 30 s, so a server
still on its last retries when you call is woken as soon as it parks. Each
connection acts on a request once. If your server was not listening yet, it
parks again: call again, or let `parked_retry_interval` pick it up.

This is an overlay on the pinned Hermes image (`patch-mcp-wake.py`). The route,
key and response fields stay stable across pin moves. Changes are additive.
