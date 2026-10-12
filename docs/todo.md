# Todo lists (Vikunja)

Opt-in todo lists for you and Hermes. [Vikunja](https://vikunja.io) gives you
a web app (lists, kanban, Gantt, mobile apps, CalDAV) and serves its own MCP
endpoint, so Hermes reads and edits the same tasks you see. Its data lives in
the [shared Postgres](database.md) in a schema of its own.

Requires the **`todo`** compose profile (`./scripts/setup.sh --todo`). It
starts the shared Postgres too, and does not need `--db`.

## Quick path

```bash
./scripts/setup.sh --todo
make up
make todo-init        # account with a generated password + Hermes' two tokens
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
make doctor
make todo-open        # the web app, already signed in
```

There is no password to choose or remember. `make todo-open` opens
**[http://localhost:3456/login-local](http://localhost:3456/login-local)**,
which signs the browser in and keeps it signed in; bookmark it. In WebUI:
*Add "renew passport" to my todo list, due Friday.* Then in a new chat:
*What's on my todo list?*

## How it works

| Piece | Role |
|---|---|
| `vikunja` | [Vikunja](https://github.com/go-vikunja/vikunja) 2.7 (AGPL-3.0). No host port. MCP at `http://vikunja:3456/api/v2/mcp`, Streamable HTTP with a bearer token |
| `vikunja-proxy` | nginx in front of Vikunja on `127.0.0.1:${VIKUNJA_PORT}` (default 3456). Passes everything through and adds `/login-local` (`compose/vikunja-proxy/`). About 4 MiB of RAM, capped at half a CPU, well under a millisecond per request |
| Schema `vikunja` | Owned by role `svc_vikunja` in the shared database. `compose/postgres/services/vikunja.env` holds its login (gitignored, written by `setup.sh --todo`) |
| `VIKUNJA_FILES_DIR` | Task attachments, default `./data/vikunja/files` |
| `make todo-init` | Creates the account (or adopts the only existing one) with a generated password, mints two API tokens, and writes all of it to `.env` |
| Hermes bootstrap | Registers `mcp_servers.vikunja` when `VIKUNJA_MCP_ENABLED=1` and the profile's token is set |
| `living-todos` skill | Routes "add to my todo list" to Vikunja when its tools are present, and to the markdown file otherwise (`compose/hermes/skills/living-todos/`) |

### Two tokens, enforced by Vikunja

| Hermes profile | Used by | Token | Tools |
|---|---|---|---|
| `api-server` | unattended API / n8n / Boundary | `VIKUNJA_MCP_TOKEN_RO` | 11: list and read tasks, projects, labels, comments, assignees; kanban views and buckets via `find_action` |
| default | dashboard, CLI | `VIKUNJA_MCP_TOKEN_RW` | 26: the above plus create, update, delete; also create, rename and delete buckets and move tasks between them |
| `browser` | Hermes WebUI | `VIKUNJA_MCP_TOKEN_RW` | same as default |

Vikunja filters the tool list by the token's scopes and refuses any call
outside them, including through its `find_action` / `do_action` meta-tools.
So the boundary sits in Vikunja, and Hermes needs no tool allowlist. The
read/write token gets the same scopes as the *Typed* preset on Vikunja's
Settings → MCP page (tasks, projects, labels, comments, assignees, user
search), plus kanban: reading project views, and reading and writing their
buckets (`buckets_*`, `task_bucket_update`). The read-only token gets the read
half of each. Neither can create, change or delete views. Neither has access
to account settings, webhooks, sharing, or teams.

Bootstrap writes each token into that profile's `.env` under `data/hermes/` as
`VIKUNJA_MCP_TOKEN`. `config.yaml` carries only the reference:

```yaml
mcp_servers:
  vikunja:
    url: http://vikunja:3456/api/v2/mcp
    headers:
      Authorization: Bearer ${VIKUNJA_MCP_TOKEN}
```

Pinned (always visible) tools: `tasks_list`, `tasks_read`, `projects_list`,
plus `tasks_create` and `tasks_update` on the read/write profiles. The rest
are found through tool search.

### Signing in without a password

Vikunja has no login-free mode, so the stack manages one account for you.
`make todo-init` gives it a random password, stores `VIKUNJA_USERNAME` and
`VIKUNJA_PASSWORD` in `.env`, and `vikunja-proxy` uses them:

1. `/login-local` is a small page on Vikunja's own origin. It first tries to
   renew from the browser's existing Vikunja refresh cookie, so a browser
   that signed in before reuses its session.
2. Otherwise it fetches `/login-local/session`. The proxy turns that into a
   login with the stored password (the password never reaches the browser),
   and Vikunja answers with a session token and a refresh cookie that lasts
   `VIKUNJA_SESSION_TTL` (10 years).
3. The page stores the token where Vikunja's web app expects it and opens
   your lists.

What this means: **anything that can reach `127.0.0.1:3456` on this machine
is signed in as that account.** That is the point for a personal, local todo
list. The port is loopback-only, so other devices on your network cannot reach
it. The proxy also refuses `/login-local` when:

- the request is for a host name other than `localhost`, `127.0.0.1`, or
  `[::1]`, which blocks DNS-rebinding pages;
- a page on another site, or another localhost port, fetches the session
  endpoint (`Sec-Fetch-Site`). It also strips CORS headers, so no other page
  can read the response.

Vikunja's sign-up form stays off (`VIKUNJA_ENABLE_REGISTRATION=false`). An
account you made earlier by hand is adopted if it is the only one: `make
todo-init` replaces its password with a generated one, and the old password
stops working. With several accounts, set `VIKUNJA_USERNAME` in `.env` to pick
one. The normal sign-in form at `/login` still works if you want to read the
password from `.env`.

## Tokens

`make todo-init` is safe to re-run. It deletes the tokens it made before
(titles `assistant-hermes-rw` and `assistant-hermes-ro`), mints new ones, and
updates `.env`. Re-run both bootstraps and restart `hermes` afterwards.

- **Rotate:** `make todo-init`, then the three commands it prints.
- **Use another account:** set `VIKUNJA_USERNAME=<name>` in `.env`, clear
  `VIKUNJA_PASSWORD`, and run `make todo-init`. It creates that account, or
  gives an existing one a generated password.
- **Revoke:** delete the token under Settings → API Tokens in Vikunja.
  Hermes' calls then fail with 401 until you run `make todo-init` again.

Tokens expire in 2099. Vikunja requires an expiry date.

## Your existing markdown list

Before Vikunja, the `living-todos` skill kept the list in
`data/projects/project-todo-list/README.md`. Once Hermes has the Vikunja
tools, it stops writing that file. Nothing moves on its own. To bring the
open items over, ask in WebUI: *Move my markdown todo list into Vikunja.* The
skill creates one task per open `- [ ]` item and leaves the file in place.

## Querying tasks with SQL

The `vikunja` schema is readable by `agent_ro` and `agent_rw` like any
integration schema, so with `--db` on Hermes can join tasks with other data
(`vikunja.tasks`, `vikunja.projects`). Writes go through the MCP tools, not
SQL. Vikunja owns its tables, and `agent_rw` cannot write them.

## Settings

| `.env` key | Default | Meaning |
|---|---|---|
| `VIKUNJA_PORT` | `3456` | Host port for the web UI (loopback only) |
| `VIKUNJA_PUBLIC_URL` | `http://localhost:${VIKUNJA_PORT}/` | Base URL Vikunja puts in links and on its MCP page |
| `VIKUNJA_SECRET` | generated | Signs login sessions. Empty means a new one each start, which logs everyone out |
| `VIKUNJA_ENABLE_REGISTRATION` | `false` | Allow sign-up from the web UI |
| `VIKUNJA_USERNAME` / `VIKUNJA_PASSWORD` | written by `make todo-init` | The managed account `/login-local` signs in as |
| `VIKUNJA_SESSION_TTL` | `315360000` (10 years) | How long a sign-in lasts (Vikunja's `service.jwtttllong`) |
| `VIKUNJA_FILES_DIR` | `./data/vikunja/files` | Attachments. Not moved by `ASSISTANT_DATA_ROOT` |
| `VIKUNJA_MCP_ENABLED` | `0` | `1` registers the MCP on Hermes. `setup.sh --todo` sets it |
| `VIKUNJA_MCP_TOKEN_RW` / `_RO` | empty | Written by `make todo-init` |
| `VIKUNJA_MCP_URL` | `http://vikunja:3456/api/v2/mcp` | Endpoint the bootstrap registers |
| `VIKUNJA_MCP_TIMEOUT` / `_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts |
| `VIKUNJA_IMAGE` | `vikunja/vikunja:2.7.0` | MCP needs 2.7.0 or later |
| `VIKUNJA_PROXY_IMAGE` | `nginx:1.29.1-alpine` | The loopback proxy |

## Backup

Tasks are in the shared database, so `make db-backup` covers them.
Attachments are files under `VIKUNJA_FILES_DIR`; copy that directory too.

## Turning it off

Remove `todo` from `COMPOSE_PROFILES`, set `VIKUNJA_MCP_ENABLED=0`, re-run
both bootstraps, and restart `hermes`. The bootstrap drops the `vikunja` MCP
entry and the token from each profile. The schema and its data stay in
Postgres until you drop them ([database.md](database.md#adding-an-integration)).

## Troubleshooting

| Symptom | Check |
|---|---|
| `vikunja` restarts with `permission denied` on `/app/vikunja/files` | The files directory must belong to `HERMES_UID`:`HERMES_GID`. `./scripts/todo.sh prepare` creates it, and Docker creates it as root if it gets there first |
| `vikunja` logs `password authentication failed for user "svc_vikunja"` | `docker compose run --rm db-provision`, then `docker compose restart vikunja`. `make doctor` checks `services/vikunja.env` |
| Hermes reports a 401 from `vikunja` | The token was revoked or rotated. Run `make todo-init` again, then both bootstraps |
| Hermes still writes the markdown file | `vikunja` is missing from `hermes mcp list` for that profile. Re-run the bootstrap for that profile and restart `hermes` |
| Logged out after every restart | `VIKUNJA_SECRET` is empty. Re-run `./scripts/setup.sh` |
| `/login-local` says automatic sign-in failed | `make todo-init` has not run, or `.env` changed since. Run it again; it recreates `vikunja-proxy` |
| `/login-local` returns 403 | Open it as `localhost` or `127.0.0.1`, not another host name |

## Building another UI on it

Vikunja has no switch to turn off its web app, but you don't need one. The
web app is a client of the same REST API, which any other UI can use:

- OpenAPI: `http://localhost:3456/api/v2/openapi.json`, browsable at
  `/api/v2/docs` (v1: `/api/v1/docs.json`)
- Auth: a scoped API token (Settings → API Tokens, or `PUT /api/v1/tokens`
  as `scripts/todo.sh` does), sent as `Authorization: Bearer <token>`
- Live updates: the `/api/v2/ws` WebSocket
- CORS allows `localhost` and `127.0.0.1` on any port by default. Any other
  origin (a desktop app's custom scheme, say) has to be listed in Vikunja's
  `cors.origins`, which this stack does not set yet
- CalDAV tasks (VTODO) at `/dav/` for clients that speak it
