# Budget (Actual Budget)

[Actual Budget](https://actualbudget.org) is an open-source budgeting app with
a web UI and a sync server. Hermes reaches a budget file through two MCP
servers built from a vendored copy of
[giorgiobrullo/actual-mcp](https://github.com/giorgiobrullo/actual-mcp) (see
`compose/actual-mcp/VENDORED.md`), so it reads and edits the same accounts,
categories and transactions you see in Actual. Run an Actual server in this
stack, connect one you already have, or both. The budget is financial data:
read this page's [Where data lives](#where-data-lives) before backing up or
sharing a checkout.

The add-on is untested on a live stack so far; the checks that exist run
against stubs (see [Testing](#testing)).

## Two flags

| Flag | Default | Effect |
|---|---|---|
| `ACTUAL_MCP_ENABLED` | `0` | Starts `actual-mcp-rw` and `actual-mcp-ro` (profile `budget`) and registers the `budget` MCP server in Hermes |
| `ACTUAL_LOCAL_ENABLED` | `0` | Starts `actual-server` (profile `budget-local`) on `127.0.0.1:${ACTUAL_PORT:-5006}` |

A remote Actual server needs only the first. A local one needs both.

## Local quick path

```bash
./scripts/setup.sh --budget   # sets both flags
make up
make budget-init              # server password, a budget, compose/actual/budget.env
make hermes-reload
make doctor
make budget-open              # opens the web app, copies the server password
```

`make budget-init` sets a generated password on a new server, creates a budget
or adopts the server's only one (`SYNC_ID=` picks one of several), checks that
it opens, writes the budget file, sets `ACTUAL_BUDGET_FILE` and
`ACTUAL_MCP_ENABLED=1`, and recreates running MCP containers. It does nothing
when the budget file exists; `ROTATE=1` sets a new server password instead.
`make budget-open` copies the password with `pbcopy`, or, when `pbcopy` isn't available, prints
where it is.

In WebUI: *Add a $12.50 coffee at Example Cafe to my checking account.* Then
in a new chat: *What did I spend this month, by category?*

## Remote quick path

```bash
make budget-add URL=https://budget.example.com   # asks for the password and sync id
make hermes-reload
```

`budget-add` takes `ACTUAL_PASSWORD`, `ACTUAL_BUDGET_SYNC_ID` and
`ACTUAL_BUDGET_ENCRYPTION_PASSWORD` from the environment, or asks for them
(passwords hidden). With no sync id it uses the server's only budget. It opens
the budget with those values and writes nothing if that fails. If the budget
file exists it refuses unless `FORCE=1` is given.

`make budget-remove` deletes the budget file, clears `ACTUAL_BUDGET_FILE` and
sets `ACTUAL_MCP_ENABLED=0`.
Server data stays.

## The budget file

Credentials live in `compose/actual/budget.env`, mode 600, which `.env`
points to with `ACTUAL_BUDGET_FILE`. Compose mounts it read-only at `/app/.env`
in both MCP containers as a secret, and the server loads it with dotenv, so the
passwords appear in neither `.env` nor `docker inspect` of the MCP containers.
The file holds:

```bash
ACTUAL_SERVER_URL=http://actual-server:5006   # or the remote https:// URL
ACTUAL_WEB_URL=http://localhost:5006/         # where make budget-open goes
ACTUAL_PASSWORD=...
ACTUAL_BUDGET_SYNC_ID=...                     # Settings > Advanced settings
ACTUAL_BUDGET_ENCRYPTION_PASSWORD=...         # end-to-end encrypted budgets only
```

`compose/actual/budget.env.example` has the template. With `ACTUAL_BUDGET_FILE`
empty, an empty placeholder is mounted and every tool call fails.

On Linux the MCP containers run as uid 1000, so `budget.sh` refuses to write
the file unless the host user is uid 1000. Nothing is written otherwise.

`budget.sh` admin runs (`init`, `add`, `check`) start a short-lived
`docker compose run` container and pass the credentials to it as environment
variables. They are visible in `docker inspect` of that container while it
runs.

Turning on end-to-end encryption in Actual changes the budget's sync id. Run
`make budget-add URL=... FORCE=1` again for a remote server, or, for the local
one, `make budget-remove` then `make budget-init`.

## Two instances

| Hermes profile | Used by | Instance | Tools |
|---|---|---|---|
| default | dashboard, CLI, Boundary | `actual-mcp-rw`, or `actual-mcp-ro` with `ACTUAL_ACCESS=read` | the 50 tools in the manifest (18 read, 32 write), or the 18 reads |
| `browser` | WebUI | same as default | same |
| `api-server` | unattended API, n8n | `actual-mcp-ro` | the 18 reads |

Both are the same image. The read-only instance starts without
`--enable-write`, so the write tools aren't in its `tools/list`; a call to
`create-transaction` there returns `Unknown tool`. The read/write instance adds
the write tools. The manifest also lists the write tools in `writeTools`, so
the bootstrap leaves them out of the `tools.include` of any profile that gets
the read-only instance.

`run-bank-sync` exists in the upstream server and isn't in the manifest's tool list, so Hermes isn't offered it.

`ACTUAL_ACCESS` caps what chats may do:

| Value | Effect |
|---|---|
| empty or `write` | The levels in the table above |
| `read` | default and `browser` use the read-only instance |
| anything else | No `budget` entry on any profile |

`make budget-access ACCESS=read` sets it; run `make hermes-reload` afterwards.

## Cron

A cron job with no tool list of its own runs with its profile's
`platform_toolsets.cron`, which names MCP servers, not tools. A read/write
`budget` entry would hand such a job every write tool, so the bootstraps keep
it out of that list (the manifest sets `permissions.cron` to `"ro"`). A job
that reads the budget, such as a weekly spending summary, goes on
`api-server`, where `budget` is the read-only instance.

The fence only edits `platform_toolsets.cron`, so a job with its own
`enabled_toolsets` isn't covered. If that list names no MCP server, the job
gets every MCP server on its profile, and a list that names `budget` gets it
too. On default and `browser` that is the read/write instance. Give such jobs
the MCP servers they need by name, or `no_mcp`.

## Connected budgets

A connected budget is one budget on one Actual server (the bundled one or a
remote one), stored as a row under `<data root>/addons/actual/instances/`. Any
number can be connected beside, or instead of, the single budget above. Each
gets its own MCP entries in Hermes: `budget-<slug>-ro` always, and
`budget-<slug>` when the row's access is `write`. Only the `default` Hermes
profile is supported, and `browser` mirrors it. A row naming another profile
is refused with `unknown_profile`.

```bash
make budget-connect SLUG=home URL=https://budget.example.com ACCESS=write SYNC_ID=...
make budget-connect SLUG=mine URL=local        # the bundled server, no password asked
make budget-disconnect SLUG=home
```

`make budget-connect` reads the server password and the encryption password
from the terminal without echo (or from `ACTUAL_PASSWORD` and
`ACTUAL_BUDGET_ENCRYPTION_PASSWORD`), builds the JSON below with them on stdin
only, and pipes it to `scripts/addon.sh connect actual --instance <slug> --json`.
Without `SYNC_ID` it uses the server's only budget and stops when there are
several. `ACCESS` defaults to `read`. Boundary calls `addon.sh` directly with
the same JSON:

| Key | Meaning |
|---|---|
| `server` | `local`, or the server's `http(s)` URL |
| `sync_id` | Settings > Advanced settings in Actual |
| `session_token` or `password` | One of them signs in; `local` falls back to the stored server password |
| `encryption_password` | End-to-end encrypted budgets only |
| `profile` | `default` (the default) |
| `access` | `read` (the default) or `write` |

`addon.sh` runs `budget.sh connect`, which opens the budget with those values
and, only if that works, stores the session token. The password is used once
and never stored. A failed check writes nothing. Errors end with one of
`bad_slug`, `bad_access`, `unknown_profile`, `login_refused`, `token_expired`,
`encryption_password`, `no_access`, `timeout` or `server_unreachable`.

### Commands

`scripts/budget.sh` subcommands for connected budgets, all printing JSON lines
that end in `{"phase":"done"}` or one `{"phase":"error",...}`:

| Command | Effect |
|---|---|
| `budgets --json` | Reads `server` and `session_token` or `password` on stdin and lists the budgets that credential can open |
| `status [--slug SLUG] --json` | Opens each connected budget with its stored token. `state` is `ok`, `token_expired`, `no_access`, `encryption_password`, `timeout`, `server_unreachable` or `bad_access`; a bad state doesn't change the exit code |
| `access <slug> read\|write --json` | Saves the row's access with `addon.sh configure` and reloads Hermes |
| `init --server-only --json` | The manifest's init hook: sets up the bundled server (generated password in `secrets/server_password`, a budget named Budget if there is none) without writing `compose/actual/budget.env` |
| `ensure` | Creates `addons/actual/instances` and `secrets` at mode 700; `make ensure-local` runs it |

`make doctor` lists each connected budget with its `status` state.

### What lives under `data/addons/actual/`

| Path | Content |
|---|---|
| `instances/<slug>.json` | The row: server, sync id, profile, access |
| `secrets/<slug>.session_token` | The token the MCP child signs in with |
| `secrets/<slug>.encryption_password` | Only for encrypted budgets |
| `secrets/server_password` | The bundled server's generated password |

Secret files are mode 600. The MCP containers mount `data/addons/actual`
read-only at `/budgets`. On Linux they run as uid 1000, so connected budgets need the host
user to be uid 1000 (`HERMES_UID`, the default).

In each MCP container a launcher starts one child per row and serves it at
`/<slug>/mcp`; the legacy `/mcp` path still serves the single budget. The
launcher checks the store every 2 seconds, restarts a child when any of its
files change, and deletes the child's cache whenever it stops. The `-ro`
container's children run without write tools.

### Hermes entries and cron

`budget-<slug>-ro` and `budget-<slug>` are registered on `default` and
`browser` only. The plain `budget` entry registers only while
`ACTUAL_BUDGET_FILE` names a budget file. The cron fence matches add-on
services by scheme, host and port, so `budget-<slug>` stays out of cron lists
like `budget` does.

### The bundled server and the add-on switches

`addon.sh disable actual` sets `ACTUAL_LOCAL_ENABLED=0` and stops
`actual-server`; `addon.sh enable actual` turns it back on only if the
manifest's `local` setting is stored (`addon.sh configure actual` with
`{"local": true}`). An install from v0.3.12 that set `ACTUAL_LOCAL_ENABLED=1`
by hand needs that setting stored the same way before it uses `disable` and
`enable`. The `make budget-*` commands don't depend on it.

`make budget-remove` keeps `ACTUAL_MCP_ENABLED=1` while any connected budget
exists.

### Expired sessions

A connected budget holds a session token, not the password. Changing the
password on an Actual server, or turning OpenID on, ends the session of every
budget connected to it. `budget.sh status` then shows `token_expired` for each
until it is reconnected (`make budget-disconnect`, then `make budget-connect`).

## Networks

The `budget` network holds `hermes`, `actual-server` and the two MCP
containers, and nothing else. The MCP servers take requests without bearer
auth, so network membership is the access control. The network isn't
`internal`, because the MCP containers may need to reach a remote Actual
server.

With `HERMES_TERMINAL_BACKEND=local` the agent's shell runs inside `hermes`,
which is on `budget`, so any profile with a shell tool can reach
`actual-mcp-rw` with curl.

## Where data lives

- The server's data directory, `ACTUAL_DATA_DIR` (default
  `./data/actual/server`), holds the budget files and the server's own state. Back it up with the server stopped.
- `actual_mcp_rw_cache` and `actual_mcp_ro_cache`, Docker volumes, each hold a
  full copy of the budget that the MCP container downloaded. Treat them like
  the data directory; `docker compose down -v` deletes them.
- `compose/actual/budget.env` holds the credentials.
- `<data root>/addons/actual/` holds the connected budgets (see
  [Connected budgets](#connected-budgets)); each MCP container keeps a cache
  per connected budget that the launcher deletes when the child stops.

## Limits and quirks

- A read on the read-only instance within about 5 seconds of a write on the
  read/write instance can miss it. The read/write instance returns before its
  change reaches the server. Both instances run with `ACTUAL_SYNC_TTL_MS=0`, so
  every read syncs first; the delay is the write's push.
- No tool moves money between categories. It takes two `set-budget-amount`
  calls, which the `budget` skill (`compose/hermes/skills/budget/`) asks
  Hermes to confirm first.
- `ACTUAL_ADMIN_TIMEOUT_S` (default 45) bounds each admin check. A server that
  doesn't answer in time makes `actual-admin.mjs` exit 4; `make doctor` then
  warns and skips the credential check, and `budget.sh` reports the timeout and
  writes nothing.
- The MCP containers load the budget on the first tool call, not at startup, so
  a wrong password shows as a failed call. `make doctor` opens the budget with
  the file's credentials to catch it.

## Checks

`make doctor` checks that the profiles are enabled, that the budget file exists
and is mode 600, that the file points at a server that is running (it warns
when it names `actual-server` and `ACTUAL_LOCAL_ENABLED=0`), that both MCP
containers are healthy, that the budget opens with the file's credentials, that
`actual-mcp-rw` answers `tools/list`, and that `actual-mcp-ro` lists no write
tool. It also reports each connected budget's `status`.

## Testing

Offline, against stubs: `test-addon-manifests.py`, `test-actual-registration.sh`,
`test-addon-mcp-access.sh`, `test-addon-cron-fence.sh`, `test-actual-rows.sh`, `test-actual-admin.sh`, `test-budget-sh.sh` and
`test-actual-doctor.sh`. None of them starts Actual.

## Image bumps

The `actual-server` image (`ACTUAL_SERVER_IMAGE`, pinned by digest) and
`@actual-app/api` in `compose/actual-mcp` are pinned together at 26.11.0. Bump
them together. A version mismatch, or a renamed tool, breaks the add-on
without failing any offline test, so re-run a live check after any bump: add a
transaction from WebUI, ask for the month's spending, confirm a write through
`api-server` fails, and set `ACTUAL_ACCESS=read` and confirm WebUI loses its
write tools.

## Turning it off

Set `ACTUAL_MCP_ENABLED=0`, or run `make budget-remove`, then run
`make hermes-reload`. That drops the `budget` entry from every profile's
`config.yaml` and cron list.

Compose leaves a removed profile's containers running, and `make up` doesn't
stop them. Stop the MCP containers yourself:

```bash
docker compose stop actual-mcp-rw actual-mcp-ro
```

Until they stop, they keep serving the budget with the credentials from the
file they mounted at start. With `ACTUAL_LOCAL_ENABLED=0`, stop the server the
same way (`docker compose stop actual-server`); its data stays in
`ACTUAL_DATA_DIR`.
