# Budget (Actual Budget)

[Actual Budget](https://actualbudget.org) is an open-source budgeting app with
a web UI and a sync server. Hermes reaches each connected budget through two
MCP servers built from a vendored copy of
[giorgiobrullo/actual-mcp](https://github.com/giorgiobrullo/actual-mcp) (see
`compose/actual-mcp/VENDORED.md`), so it reads and edits the same accounts,
categories and transactions you see in Actual. Run an Actual server in this
stack, connect budgets on servers you already have, or both. The budget is
financial data: read [Where data lives](#where-data-lives) before backing up or
sharing a checkout.

The add-on is untested on a live stack so far; the checks that exist run
against stubs (see [Testing](#testing)).

## Two flags

| Flag | Default | Effect |
|---|---|---|
| `ACTUAL_MCP_ENABLED` | `0` | Starts `actual-mcp-rw` and `actual-mcp-ro` (profile `budget`); Hermes registers one MCP entry per connected budget |
| `ACTUAL_LOCAL_ENABLED` | `0` | Starts `actual-server` (profile `budget-local`) on `127.0.0.1:${ACTUAL_PORT:-5006}` |

Budgets on a remote server need only the first. The bundled server needs both.

## Local quick path

```bash
./scripts/setup.sh --budget        # sets both flags
make up
scripts/addon.sh enable actual     # sets up actual-server and creates a budget
make budget-connect SLUG=home URL=local ACCESS=write
make doctor
make budget-open                   # opens the web app, copies the server password
```

`addon.sh enable actual` runs the add-on's init hook, `budget.sh init
--server-only`. On a new server it sets a generated password, which it keeps in
`<data root>/addons/actual/secrets/server_password` (mode 600) before telling
the server, and it creates a budget named Budget when the server has none. With
`ACTUAL_LOCAL_ENABLED` off the hook does nothing, so after turning the bundled
server on later, run `scripts/addon.sh enable actual --reinit`.

`make budget-open` opens `http://localhost:${ACTUAL_PORT}/` and copies the
stored server password with `pbcopy`; without `pbcopy` it prints where the
password is. It refuses while `ACTUAL_LOCAL_ENABLED` is off.

In WebUI: *Add a $12.50 coffee at Example Cafe to my checking account.* Then
in a new chat: *What did I spend this month, by category?*

## Remote quick path

```bash
scripts/addon.sh enable actual
make budget-connect SLUG=home URL=https://budget.example.com ACCESS=write SYNC_ID=...
```

## Connected budgets

A connected budget is one budget on one Actual server (the bundled one or a
remote one), stored as a row under `<data root>/addons/actual/instances/`. Any
number can be connected. Each gets one MCP entry per Hermes profile, named
`budget-<slug>`. A slug is a lowercase letter followed by up to 30 of `a-z`,
`0-9` and `-`.

Enable the add-on (`scripts/addon.sh enable actual`) before connecting a
budget. `addon.sh connect` on a disabled add-on stores the row and reports
`done`, but Hermes registers no entry for it until the add-on is enabled.

```bash
make budget-connect SLUG=home URL=https://budget.example.com ACCESS=write SYNC_ID=...
make budget-connect SLUG=mine URL=local        # the bundled server, no password asked
make budget-access SLUG=home ACCESS=read
make budget-disconnect SLUG=home
```

`make budget-connect` reads the server password and the encryption password
from the terminal without echo (or from `ACTUAL_PASSWORD` and
`ACTUAL_BUDGET_ENCRYPTION_PASSWORD`), builds the JSON below with them on stdin
only, and pipes it to `scripts/addon.sh connect actual --instance <slug> --json`.
It refuses while `ACTUAL_MCP_ENABLED` is off. Without `SYNC_ID` it uses the
server's only budget and stops when there are several. `ACCESS=write` adds the
bind that `access <slug> write` stores (see [Access and bind](#access-and-bind));
without it the budget is read-only. It and `make budget-disconnect` print
`addon.sh`'s result as `[budget]` lines. Boundary calls `addon.sh` directly
with the same JSON:

| Key | Meaning |
|---|---|
| `server` | `local`, or the server's `http(s)` URL |
| `sync_id` | Settings > Advanced settings in Actual |
| `session_token` or `password` | One of them signs in; `local` falls back to the stored server password |
| `encryption_password` | End-to-end encrypted budgets only |
| `bind` | Optional: profile to `ro` or `rw` (see [Access and bind](#access-and-bind)) |

`addon.sh` checks the input, stores `bind` itself, and runs `budget.sh
connect`, which opens the budget with the rest and, only if that works, stores
the session token. The password is used once and never stored. A failed check
writes nothing. The hook's errors are `bad_slug`, `not_found`,
`login_refused`, `token_expired`, `encryption_password`, `no_access`, `timeout`
and `server_unreachable`. `not_found` covers a local server whose password
isn't stored (run `scripts/addon.sh enable actual --reinit`) and, on Linux, a
host user other than uid 1000. A setting the manifest doesn't know, such as
the old `profile` or `access`, is refused by `addon.sh` before the hook runs.

To replace a connected budget (a new password, a new session after the old
one ended), connect it again with `FORCE=1`:

```bash
make budget-connect SLUG=home URL=https://budget.example.com FORCE=1
```

`FORCE=1` passes `--force` to `addon.sh connect`, which replaces the row and
runs `budget.sh connect --slug <slug> --replace --json`. `--replace` makes the
same checks in the same order as a new connect and writes nothing until they
pass, so a failed replace leaves the old session token and encryption
password as they were. On success it overwrites `secrets/<slug>.session_token`
and writes or removes `secrets/<slug>.encryption_password`. The MCP launcher
restarts that budget's server when its files change. The row is replaced
too, so pass `ACCESS=write` again to keep a write bind.

## Access and bind

Each row may hold `bind`, a map from Hermes profile to `ro` or `rw`. `ro`
points the profile's `budget-<slug>` entry at `actual-mcp-ro`, `rw` at
`actual-mcp-rw`. A row without `bind` gets the manifest's `mcp.profiles`, which
is `ro` on `default`, `browser` and `api-server`, so a new budget is read-only
until its bind raises a profile. A profile left out of a row's bind gets no
entry for that budget. The store refuses `rw` on `api-server`.

`budget.sh access <slug> read|write` writes one of two binds through
`scripts/addon.sh configure actual --instance <slug>`:

| Access | Bind |
|---|---|
| `read` | `{"default":"ro","browser":"ro","api-server":"ro"}` |
| `write` | `{"default":"rw","browser":"rw","api-server":"ro"}` |

`addon.sh configure` stores the bind without reloading Hermes, so `access`
then re-runs `hermes-api-bootstrap` and `hermes-browser-bootstrap` with
`HERMES_BOOTSTRAP_FORCE=1` and `vault.sh reload-plugins`. With `--json` it
prints `{"phase":"done","mcp":["budget-<slug>"]}`; Boundary reads `mcp` from
that line. `make budget-access SLUG=... ACCESS=read|write` runs it.

| Hermes profile | Used by | Instance with `ro` | Instance with `rw` |
|---|---|---|---|
| default | dashboard, CLI, Boundary | `actual-mcp-ro`: the 18 reads | `actual-mcp-rw`: the 50 tools in the manifest (18 read, 32 write) |
| `browser` | WebUI | same | same |
| `api-server` | unattended API, n8n | `actual-mcp-ro` | not allowed |

Both instances run the same image. The read-only instance starts without
`--enable-write`, so the write tools aren't in its `tools/list`; a call to
`create-transaction` there returns `Unknown tool`. The manifest also lists the
write tools in `writeTools`, so the bootstrap leaves them out of the
`tools.include` of any profile that gets the read-only instance.

`run-bank-sync` exists in the upstream server and isn't in the manifest's tool
list, so Hermes isn't offered it.

## Commands

`scripts/budget.sh` subcommands. The `--json` ones print JSON lines that end in
`{"phase":"done",...}` or one `{"phase":"error",...}`:

| Command | Effect |
|---|---|
| `init --server-only [--json]` | The manifest's init hook (above). When the server has exactly one budget afterwards, the `done` line also carries it as `"result": {"sync_id": ..., "name": ...}`, which `addon.sh enable actual --json` returns as `done.result` |
| `budgets --json` | Reads `server` and `session_token` or `password` on stdin and lists the budgets that credential can open |
| `connect` / `disconnect --slug SLUG --json` | The manifest's instance hooks; `addon.sh` runs them. `connect` prints `{"phase":"done","account":"<slug>","mcp":["budget-<slug>"]}` |
| `status [--slug SLUG] --json` | Opens each connected budget with its stored token: one `{"phase":"budget","slug":...,"access":...,"state":...}` line each |
| `access SLUG read\|write [--json]` | Writes the row's bind (see [Access and bind](#access-and-bind)) |
| `connect-cli`, `disconnect-cli` | `make budget-connect` and `make budget-disconnect` |
| `open` | `make budget-open` |
| `ensure` | Creates `addons/actual/instances` and `secrets` at mode 700; `make ensure-local` runs it |

In `status`, `access` is `write` when the row's `bind.default` is `rw`, else
`read`. `state` is `ok`, `token_expired`, `no_access`, `encryption_password`,
`timeout`, `server_unreachable` or `server_off`. `server_off` is a local row
while `ACTUAL_LOCAL_ENABLED` is off, reported without a sign-in or an Actual
check. A bad state doesn't change the exit code.

`budget.sh` admin runs (`connect`, `status`, and creating the bundled server's
first budget) start a short-lived `docker compose run` container and pass the
credentials to it as environment variables. They are visible in `docker
inspect` of that container while it runs.

## Cron

A cron job with no tool list of its own runs with its profile's
`platform_toolsets.cron`, which names MCP servers, not tools. A `budget-<slug>`
entry on `actual-mcp-rw` would hand such a job every write tool, so the
bootstraps keep write entries out of that list (the manifest sets
`permissions.cron` to `"ro"`). A job that reads a budget, such as a weekly
spending summary, goes on `api-server`, where every budget is read-only.

The fence only edits `platform_toolsets.cron`, so a job with its own
`enabled_toolsets` isn't covered. If that list names no MCP server, the job
gets every MCP server on its profile, and a list that names a `budget-<slug>`
entry gets it too. On `default` and `browser` that entry is the read/write
instance when the bind says `rw`. Give such jobs the MCP servers they need by
name, or `no_mcp`.

## The bundled server and the add-on switches

`addon.sh disable actual` sets `ACTUAL_LOCAL_ENABLED=0` and stops
`actual-server`; `addon.sh enable actual` turns it back on when the manifest's
`local` setting is stored (`addon.sh configure actual` with `{"local": true}`).
When `ACTUAL_LOCAL_ENABLED=1` was set by hand and `local` isn't stored,
`disable` stores `local` as true before it zeroes the flag, so the next
`enable` restores it.

While `ACTUAL_LOCAL_ENABLED` is off, `budget.sh status` reports rows whose
`server` is `local` as `server_off`. To turn the bundled server back on, run
`echo '{"local": true}' | scripts/addon.sh configure actual`, then
`scripts/addon.sh enable actual`; add `--reinit` if the add-on was first
enabled with the local server off, since the init hook did nothing then.

## Expired sessions

A connected budget holds a session token, not the password. Changing the
password on an Actual server, or turning OpenID on, ends the session of every
budget connected to it. `budget.sh status` then shows `token_expired` for each
until it is reconnected (`make budget-connect ... FORCE=1`, or
`make budget-disconnect` then `make budget-connect`).

Turning on end-to-end encryption in Actual changes the budget's sync id, so
the budget has to be connected again with the new one.

A write made in the seconds between a good sync and the background push can
still be stranded if the session ends right then.

## Where data lives

| Path | Content |
|---|---|
| `ACTUAL_DATA_DIR` (default `./data/actual/server`) | The bundled server's budget files and its own state. Back it up with the server stopped |
| `<data root>/addons/actual/instances/<slug>.json` | The row: server, sync id, and `bind` when one was set |
| `<data root>/addons/actual/secrets/<slug>.session_token` | The token the MCP child signs in with |
| `<data root>/addons/actual/secrets/<slug>.encryption_password` | Only for encrypted budgets |
| `<data root>/addons/actual/secrets/server_password` | The bundled server's generated password |
| `actual_mcp_rw_cache`, `actual_mcp_ro_cache` (Docker volumes) | Each MCP container's copy of the budgets it serves; `docker compose down -v` deletes them |

Secret files are mode 600. The MCP containers mount `data/addons/actual`
read-only at `/budgets`. On Linux they run as uid 1000, so connected budgets
need the host user to be uid 1000 (`HERMES_UID`, the default); `budget.sh
connect` refuses another uid and `make doctor` flags it. The launcher drops
one trailing newline from a secret file and keeps every other character.

In each MCP container a launcher starts one child per row and serves it at
`/<slug>/mcp`. `actual-mcp-rw` starts children only for rows whose bind gives
some profile `rw`. The launcher checks the store every 2 seconds, restarts a
child when any of its files change, and deletes the child's cache whenever it
stops.

## Networks

The `budget` network holds `hermes`, `actual-server` and the two MCP
containers, and nothing else. The MCP servers take requests without bearer
auth, so network membership is the access control. The network isn't
`internal`, because the MCP containers may need to reach a remote Actual
server.

With `HERMES_TERMINAL_BACKEND=local` the agent's shell runs inside `hermes`,
which is on `budget`, so any profile with a shell tool can reach
`actual-mcp-rw` with curl, whatever a budget's bind says.

## Limits and quirks

- A read on the read-only instance within about 5 seconds of a write on the
  read/write instance can miss it. The read/write instance returns before its
  change reaches the server. Both instances run with `ACTUAL_SYNC_TTL_MS=0`, so
  every read syncs first; the delay is the write's push.
- No tool moves money between categories. It takes two `set-budget-amount`
  calls, which the `budget` skill (`compose/hermes/skills/budget/`) asks
  Hermes to confirm first.
- `ACTUAL_ADMIN_TIMEOUT_S` (default 45) bounds each admin check. A server that
  doesn't answer in time makes `actual-admin.mjs` exit 4; `connect` reports
  `timeout` and writes nothing, and `status` reports the row as `timeout`.
- The MCP containers load a budget on its first tool call, not at startup, so
  a bad credential shows as a failed call. `make doctor` opens each connected
  budget with its stored token to catch it.

## Checks

`make doctor` checks that the `budget` profile (and `budget-local`, when
`ACTUAL_LOCAL_ENABLED` is on) is in `COMPOSE_PROFILES`, that the MCP containers
and the bundled server are healthy, and, on Linux, that the host user is uid
1000 when budgets are connected. It lists each connected budget with its
`status` state; anything but `ok` is a warning. For each budget in state `ok`
it runs `tools/list` on `actual-mcp-ro`'s `/<slug>/mcp` and fails if a write
tool is listed, and, when the status says `access` `write`, checks that
`actual-mcp-rw`'s `/<slug>/mcp` answers.

## Testing

Offline, against stubs: `test-addon-manifests.py`, `test-actual-instances.sh`,
`test-addon-cron-fence.sh`, `test-budget-sh.sh` and `test-actual-doctor.sh`.
`test-actual-compose.sh` runs `docker compose config`. None of them starts
Actual.
`test-actual-admin.sh` runs `actual-admin.mjs` against a real
`actual-server` container and skips when Docker or either image is missing.

## Image bumps

The `actual-server` image (`ACTUAL_SERVER_IMAGE`, pinned by digest) and
`@actual-app/api` in `compose/actual-mcp` are pinned together at 26.11.0. Bump
them together. A version mismatch, or a renamed tool, breaks the add-on
without failing any offline test, so re-run a live check after any bump: add a
transaction from WebUI on a budget with access `write`, ask for the month's
spending, confirm a write through `api-server` fails, and run `budget.sh
access <slug> read` and confirm WebUI loses its write tools.

## Turning it off

`make budget-disconnect SLUG=...` removes one budget and its Hermes entries.
To turn the add-on off, run `scripts/addon.sh disable actual`, or set
`ACTUAL_MCP_ENABLED=0` and run `make hermes-reload`.

Compose leaves a removed profile's containers running, and `make up` doesn't
stop them. Stop the MCP containers yourself:

```bash
docker compose stop actual-mcp-rw actual-mcp-ro
```

With `ACTUAL_LOCAL_ENABLED=0`, stop the server the same way (`docker compose
stop actual-server`); its data stays in `ACTUAL_DATA_DIR`.
