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
make todo-init        # creates your Vikunja account and Hermes' two tokens
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
make doctor
```

Open **[http://localhost:3456](http://localhost:3456)** and sign in with the
account `make todo-init` created. In WebUI: *Add "renew passport" to my todo
list, due Friday.* Then in a new chat: *What's on my todo list?*

## How it works

| Piece | Role |
|---|---|
| `vikunja` | [Vikunja](https://github.com/go-vikunja/vikunja) 2.7 (AGPL-3.0). Web UI on `127.0.0.1:${VIKUNJA_PORT}` (default 3456). MCP at `http://vikunja:3456/api/v2/mcp`, Streamable HTTP with a bearer token |
| Schema `vikunja` | Owned by role `svc_vikunja` in the shared database. `compose/postgres/services/vikunja.env` holds its login (gitignored, written by `setup.sh --todo`) |
| `VIKUNJA_FILES_DIR` | Task attachments, default `./data/vikunja/files` |
| `make todo-init` | Creates the account from the Vikunja CLI if it does not exist, then mints two API tokens and writes them to `.env` |
| Hermes bootstrap | Registers `mcp_servers.vikunja` when `VIKUNJA_MCP_ENABLED=1` and the profile's token is set |
| `living-todos` skill | Routes "add to my todo list" to Vikunja when its tools are present, and to the markdown file otherwise (`compose/hermes/skills/living-todos/`) |

### Two tokens, enforced by Vikunja

| Hermes profile | Used by | Token | Tools |
|---|---|---|---|
| `api-server` | unattended API / n8n / Boundary | `VIKUNJA_MCP_TOKEN_RO` | 11: list and read tasks, projects, labels, comments, assignees |
| default | dashboard, CLI | `VIKUNJA_MCP_TOKEN_RW` | 26: the above plus create, update, delete |
| `browser` | Hermes WebUI | `VIKUNJA_MCP_TOKEN_RW` | same as default |

Vikunja filters the tool list by the token's scopes and refuses any call
outside them, including through its `find_action` / `do_action` meta-tools.
So the boundary sits in Vikunja, and Hermes needs no tool allowlist. The
read/write token gets the same scopes as the *Typed* preset on Vikunja's
Settings → MCP page (tasks, projects, labels, comments, assignees, user
search). It has no access to account settings, webhooks, sharing, or teams.

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

### Why the account is created from the CLI

Vikunja's sign-up form is off (`VIKUNJA_ENABLE_REGISTRATION=false`), so
nobody who reaches the port can create an account. `make todo-init` runs
`vikunja user create` inside the container. To let others sign up, set
`VIKUNJA_ENABLE_REGISTRATION=true` and recreate `vikunja`. Even then, Hermes
only sees the tasks of the account whose tokens it holds.

## Tokens

`make todo-init` is safe to re-run. It deletes the tokens it made before
(titles `assistant-hermes-rw` and `assistant-hermes-ro`), mints new ones, and
updates `.env`. Re-run both bootstraps and restart `hermes` afterwards.

- **Rotate:** `make todo-init`, then the three commands it prints.
- **Use another account:** `VIKUNJA_USERNAME=<name> make todo-init`. It
  prompts for anything not in the environment (`VIKUNJA_USERNAME`,
  `VIKUNJA_PASSWORD`, `VIKUNJA_EMAIL`).
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
| `VIKUNJA_FILES_DIR` | `./data/vikunja/files` | Attachments. Not moved by `ASSISTANT_DATA_ROOT` |
| `VIKUNJA_MCP_ENABLED` | `0` | `1` registers the MCP on Hermes. `setup.sh --todo` sets it |
| `VIKUNJA_MCP_TOKEN_RW` / `_RO` | empty | Written by `make todo-init` |
| `VIKUNJA_MCP_URL` | `http://vikunja:3456/api/v2/mcp` | Endpoint the bootstrap registers |
| `VIKUNJA_MCP_TIMEOUT` / `_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts |
| `VIKUNJA_IMAGE` | `vikunja/vikunja:2.7.0` | MCP needs 2.7.0 or later |

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
