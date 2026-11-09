# Actual Budget add-on

Status: built and released in v0.3.12 (single budget). The amendment at the end, 2026-11-09, plans budgets per profile (CR 0026) and is not built.

## Goal

Give Hermes read and write access to an [Actual Budget](https://actualbudget.org) budget, as an
optional add-on that is off by default. Hermes answers questions about balances, spending and
category status, and on request adds, categorizes and edits transactions and moves money between
categories. The budget either runs in this stack (`actual-server` in a compose profile) or lives on
an Actual server the user already runs.

Actual becomes the second built-in add-on registered from a manifest, after GPT Researcher
(`addons/gptr/addon.json`, merged in 670ac9e). The core changes it needs are four extensions
to `scripts/lib/addon_manifest.py` and the Hermes bootstrap (see Core extensions). Two of them
are already described in the draft manifest spec.

Out of scope for v1: more than one budget per stack, bank sync (SimpleFIN, GoCardless), a
signed-in web UI link, and the packaged (feed) add-on path.

## How Actual differs from Twenty and Vikunja

Actual has no REST API, no built-in MCP endpoint and no scoped API keys. Every client uses the
official `@actual-app/api` Node package. It runs the app's engine headless, downloads the whole
budget into a local SQLite cache, runs each call against that copy, and syncs changes back to the
server. One server password unlocks every budget on the server. A budget can also carry an
end-to-end encryption password.

So Actual itself cannot give `api-server` a read-only key, as Twenty and Vikunja do. The read-only
boundary here is a separate MCP container that never registers write tools, plus Hermes'
`tools.include` list on `ro` profiles.

The `@actual-app/api` version has to be compatible with the server's version. The MCP image and
`ACTUAL_SERVER_IMAGE` are pinned and bumped together.

## Spike first

Two things this design depends on are unverified. The first plan task answers them on a test
stack, and the rest of the plan waits on the result.

**S1: pick the MCP server.** Candidates, all built on `@actual-app/api`:
[giorgiobrullo/actual-mcp](https://github.com/giorgiobrullo/actual-mcp) (fork of
s-stefanov/actual-mcp, writes behind `--enable-write`),
[Type-zero-labs/actual-mcp](https://glama.ai/mcp/servers/Type-zero-labs/actual-mcp) (npm,
per-tool safety layer) and
[agigante80/actual-mcp-server](https://glama.ai/mcp/servers/agigante80/actual-mcp-server) (Docker,
HTTP). The chosen one must:

- serve Streamable HTTP or SSE, so Hermes can reach it in a container
- expose no write tool when started read-only. Check by listing tools and calling a write tool by
  name against the read-only instance; the call must fail.
- sync from the server before a read, so the read-only instance doesn't answer from a stale cache
  after the read/write instance changed something
- take the server password and encryption password from the environment or a file, and never log
  them
- have a license that allows vendoring

If none passes, fall back to writing our own thin server on `@actual-app/api` under
`compose/actual-mcp/`, with the same contract. The spike records the tool list, which the
manifest's `tools`, `pin` and `writeTools` are built from.

**S2: headless local setup.** Whether `actual-server` lets a script set the first server password
(believed to be `POST /account/bootstrap`) and whether `@actual-app/api` can create an empty
budget. If it can't create one, `budget.sh init` stops after setting the password, the user
creates the budget in the web app, and the next `budget.sh init` adopts the only budget on the
server (the way `todo.sh init` adopts the only Vikunja account). With more than one budget, it
lists their names and asks for `SYNC_ID=`.

## Compose

New services, all under profile `budget` unless noted:

| Service | Role |
|---|---|
| `actual-server` | Pinned `${ACTUAL_SERVER_IMAGE}`. Web app and sync server on `127.0.0.1:${ACTUAL_PORT:-5006}`. Data in `${ACTUAL_DATA_DIR:-./data/actual/server}` at `/data`. Profile `budget-local` |
| `actual-mcp-rw` | Vendored MCP server with writes on. Internal `budget` network and the network `hermes` reaches MCP servers on. No host port |
| `actual-mcp-ro` | Same image with writes off. Same networks |

Each MCP container keeps its budget cache in its own named volume (`actual_mcp_rw_cache`,
`actual_mcp_ro_cache`), so a restart doesn't download the budget again. The cache is a full copy
of the budget, so the volumes are listed in `docs/budget.md` as holding financial data.

Both MCP containers read `compose/actual/budget.env` through `env_file` with `required: false`.
With no file they start, report unhealthy, and the existing parked-retry handling covers Hermes.

Two flags, because a remote budget needs the MCP containers but not the server:

- `ACTUAL_MCP_ENABLED` starts profile `budget` and registers the MCP entries. It is the manifest's
  `builtin.flag`.
- `ACTUAL_LOCAL_ENABLED` starts profile `budget-local`. The manifest has room for only one flag
  (an open question in the manifest spec, the same one Twenty raises), so this one is a
  hand-wired row in `SERVICES_CORE` in `scripts/sync-profiles.sh`. The manifest-roadmap work is
  settling how a manifest declares a second flag (Twenty's `TWENTY_LOCAL_ENABLED` and Bitwarden's
  `BITWARDEN_LOCAL_ENABLED` need it too). The row moves to the manifest when that shape lands.
  `sync-profiles.sh` refuses a flag listed in both places, so the move can't register it twice.

## Budget file

`compose/actual/budget.env`, mode 600, gitignored with the rest of `compose/actual/` except a
`budget.env.example`:

```bash
ACTUAL_SERVER_URL=http://actual-server:5006   # or https://budget.example.com
ACTUAL_WEB_URL=http://localhost:5006/          # where make budget-open goes
ACTUAL_PASSWORD=...
ACTUAL_SYNC_ID=...
ACTUAL_ENCRYPTION_PASSWORD=                    # only for an end-to-end encrypted budget
```

The passwords stay out of `.env`, `config.yaml` and the Hermes profiles' `.env` files, because
Hermes never talks to Actual directly. If the chosen MCP server can read them from files, they move
to compose secrets (the `TWENTY_PASSWORD_FILE` pattern) instead of `env_file`.

## Manifest

`addons/actual/addon.json`, canonical layout, roughly:

```json
{
  "manifestVersion": 1,
  "id": "io.github.kaxline.stack.actual",
  "title": "Actual Budget",
  "builtin": {"flag": "ACTUAL_MCP_ENABLED", "profile": "budget", "seed": false},
  "services": {
    "rw": {"compose": "actual-mcp-rw", "port": 3000},
    "ro": {"compose": "actual-mcp-ro", "port": 3000}
  },
  "mcp": {
    "service": {"rw": "rw", "ro": "ro"},
    "transport": "http",
    "path": "/mcp",
    "name": "budget",
    "env": {"access": "ACTUAL_ACCESS"},
    "tools": ["…from S1"],
    "pin": ["…the read tools Hermes needs first"],
    "writeTools": ["…every tool that changes the budget"],
    "profiles": {"default": "rw", "browser": "rw", "api-server": "ro"}
  },
  "permissions": {
    "reads": [],
    "writes": ["external:budget"],
    "sends": false,
    "egress": ["internet"],
    "exposesToAgent": false,
    "cron": "ro"
  }
}
```

Port, transport, path and tool names come from S1. `egress` is `internet` because a remote
budget is outside the stack. `name` is `budget`, so tools appear as `mcp__budget__<tool>`; it is
not on the reserved list.

### Is budget data a `reads` class?

Budget data is personal, so it arguably belongs in `permissions.reads` as `finance`. Today the
validator gives every `reads` add-on `cron: false` and no `api-server`, which would rule out the
read-only access chosen for unattended runs. Bitwarden needs the same exception for credentials,
behind `BITWARDEN_UNATTENDED`. Both come down to one policy question for Keith: may a
personal-data add-on reach cron and `api-server` read-only? Mail and Messages stay `cron: false`
and off `api-server` whatever the answer.

Decided 2026-11-08 (Keith, relayed by the manifest-roadmap session): strict for now, with a
per-class allowance planned. For this work, `reads` stays empty, the manifest's `description`
says the add-on holds financial data, the `reads` rules in `addon_manifest.py` stay as they are,
and `cron: "ro"` is valid only while `reads` is empty.

Follow-up: the manifest spec ("Unattended access to personal data" in
`docs/private/addon-manifest-spec.md`) plans `finance` as a read class allowed read-only cron and
`api-server`, while credentials, mail, messages and contacts are never unattended. It ships with a
real cron fence, `writeTools` filtering in `mcp-plan` and an egress and delivery limit for
unattended turns. When it lands, this manifest declares `"reads": ["finance"]`, and extensions 2
and 4 here give way to that work's versions if it gets there first.

## Core extensions

All in `scripts/lib/addon_manifest.py`, `register_addon_mcp_servers` in
`compose/hermes/bootstrap-api-profile.sh`, and `scripts/test-addon-manifests.py`. GPT Researcher's
written config must stay byte-identical, and `addons/gptr/addon.json` doesn't change. The
Bitwarden work may change the same files, so each extension is its own small commit, separate
from the Actual-specific ones.

1. **A service per access level.** `mcp.service` may be a string (today) or an object mapping
   `ro` and `rw` to service keys, as the SQL MCP's `sql-mcp-ro` and `sql-mcp-rw` already work.
   Each service the map names needs a `port`. `mcp-plan` picks the URL from the profile's access
   level. `addon_mcp_urls` lists both URLs, so turning the flag off drops both entries and parked
   retry sees both.
2. **`writeTools` left out of `ro` profiles.** The manifest spec already says this; the code
   registers `tools` unchanged today. `mcp-plan` prints `tools` minus `writeTools`, and `pin` minus
   `writeTools`, for `ro`. For Actual this is a second boundary behind the read-only container.
3. **An access cap from the environment.** `mcp.env.access` names an env var. It fails closed:
   `read` turns `rw` profiles into `ro`; `write` or an empty value keeps the manifest's level;
   any other value gives the profile no entry at all, never `rw` or `ro`, so a value added later
   (Twenty may gain `ask`, CR 0023 R3) can't widen access on an older stack. It never raises `ro`
   to `rw`. This is how the bootstrap already treats `TWENTY_ACCESS`, and it
   lets Boundary offer "look things up only". `ACTUAL_ACCESS` lives in `.env`, not the budget file, and both bootstrap services receive it as
   an environment variable, so they never see the Actual passwords. The bootstrap hashes its
   environment, so a changed value re-runs it.

4. **Read-only cron.** `permissions.cron` also accepts `"ro"`: cron may use the add-on only on
   profiles where its access is `ro`. The bootstrap generalizes `fence_mail_from_cron` (today it
   matches mail and Messages by URL) to also drop an add-on's server from
   `platform_toolsets.cron` on profiles where it registered `rw`, with the same
   managed-state file, so a list the user wrote by hand is edited rather than replaced. The
   validator accepts `"ro"` only when `permissions.reads` is empty; a `reads` add-on still needs
   `false`, as decided above.

The validator rejects an object `mcp.service` whose keys aren't a subset of `ro`/`rw` or whose
values aren't `services` keys, and a `profiles` level with no matching service.

## Hermes

| Profile | Used by | Instance | Tools |
|---|---|---|---|
| default | dashboard, CLI, Boundary | `actual-mcp-rw`, or `-ro` with `ACTUAL_ACCESS=read` | all, or reads only |
| `browser` | WebUI | same as default | same |
| `api-server` | unattended API, n8n | `actual-mcp-ro` | reads only |

Cron gets read-only access. A cron job runs with its profile's `platform_toolsets.cron`, which
names MCP servers, not tools, so on default a job would get the whole read/write `budget` server.
Core extension 4 keeps `budget` out of the cron toolsets of every profile where it is `rw`. A
scheduled job that reads the budget (a weekly spending summary) is created on `api-server`, where
`budget` is the read-only instance.

A `budget` skill in `compose/hermes/skills/budget/` tells Hermes to:

- look up account, category and payee ids before writing, never guess them
- state amounts in the budget's currency, and treat Actual's integer amounts as cents
- confirm before a delete, a bulk edit (more than one transaction) or a move between categories,
  naming what will change
- leave reconciliation and bank sync to the user

## Scripts

`scripts/budget.sh`, with `make` targets. It needs no host python3 or node: Python runs in the
Hermes image and Node in the MCP image, as `crm.sh` and `todo.sh` do, and
`scripts/test-no-host-python.sh` covers it.

| Target | Does |
|---|---|
| `make budget-init` | Local. Generates the server password, bootstraps `actual-server`, creates or adopts the budget (S2), writes `budget.env`, sets `ACTUAL_MCP_ENABLED=1` and `ACTUAL_LOCAL_ENABLED=1` |
| `make budget-add URL=` | Remote. Asks for the password, sync id and encryption password (or reads them from the environment), checks that they open the budget, and writes nothing if they don't |
| `make budget-open` | Opens `ACTUAL_WEB_URL` and copies the server password to the clipboard (`pbcopy`, or prints where it is on Linux) |
| `make budget-remove` | Deletes `budget.env` and turns `ACTUAL_MCP_ENABLED` off. Leaves server data in place |

`setup.sh --budget` turns on both flags for a local budget. `make doctor` checks that the file
exists and is mode 600, that each MCP instance answers `initialize`, that the read-only instance
lists no write tool, and, with `ACTUAL_LOCAL_ENABLED=0`, warns when the file still points at
`actual-server`.

## Testing

- `test-addon-manifests.py`: the three core extensions, the Actual manifest against compose and
  `sync-profiles.sh`, and GPT Researcher unchanged.
- `scripts/test-actual-registration.sh`, modelled on `test-gptr-mcp-registration.sh`: per-profile
  URL and `tools.include`, `ACTUAL_ACCESS=read`, flag off drops the entries.
- `scripts/test-budget-sh.sh`: `budget.sh` against a stub server; `add` writes nothing on a bad
  password; file mode is 600.
- `test-sync-profiles.sh`: the `budget-local` row.
- Live check on a test stack, never a real budget: `make budget-init`, then in WebUI add a
  transaction and ask for the month's spending; through `api-server`, a write must fail. The
  results go in the release notes.

## Docs and release

`docs/budget.md` (local and remote quick paths, the two instances, where data lives, image
bumps), a line in `docs/README.md` and `addons/README.md`, `.env.example` keys, and a
`CHANGELOG.md` entry under a 0.3.x patch release. Both flags default to off, so Boundary and
existing installs see no change.

After an image bump, re-run the live check: a version mismatch between `@actual-app/api` and the
server, or a renamed tool, breaks the add-on without failing any offline test.

## Spike results

Run 2026-11-08 on a throwaway stack: `actual-server`, the candidates and every `@actual-app/api`
script ran in Docker containers on one scratch network, with made-up budgets only. Full commands
and outputs are in `.superpowers/sdd/2026-11-08-actual-budget/task-1-report.md`.

### S1: chosen server

[giorgiobrullo/actual-mcp](https://github.com/giorgiobrullo/actual-mcp) at
`0c73b0d727a1d32bb924ab323985fffc9f0944fb` (`version` 1.15.1 in its `package.json`), MIT (copyright 2025 Stefan
Stefanov, the upstream author). It passed all five criteria with `ACTUAL_SYNC_TTL_MS=0`.

| Criterion | giorgiobrullo | agigante80 `2bf0987` | Type-zero-labs `f4aab60` |
|---|---|---|---|
| Streamable HTTP or SSE | pass | pass | fail: stdio only |
| No write tool read-only | pass | pass | fail: no read-only mode |
| Sync before read | pass, with `ACTUAL_SYNC_TTL_MS=0` | fail inside one MCP session | fail: downloads once (code reading) |
| Credentials from env or file, not logged | pass | pass (env only) | env only; password not in stderr |
| License allows vendoring | MIT | MIT | MIT |

agigante80 fails sync before read: on one open MCP session, a read after a write from the `rw`
instance (confirmed on the server by a fresh download) returned the old balance. New sessions do
see the change. Its pooled read path runs reads against the session's loaded copy without
`api.sync()`, so it can't serve a long-lived Hermes connection.

**Vendoring needs a patch.** At `0c73b0d`, `package.json` points `@actual-app/api` and
`@actual-app/core` at `file:../actual/packages/...` (the author's local Actual fork), so the repo
doesn't build alone. The spike replaced both with `26.11.0` from npm, dropped the lockfile, and
built the repo's Dockerfile with `npm install`. The build passed. Against npm `26.11.0`,
`get-accounts` worked on `ro` and `rw` and `create-transaction` worked on `rw`; on `ro`,
`create-transaction` and `delete-transaction` were refused as unknown tools, as intended. The
other tools are unverified against npm `26.11.0`. The commit message of `0c73b0d` says
`run-bank-sync` (out of scope here) now expects a return value from the fork's API. The vendored copy has to carry that change and
a regenerated lockfile.

**Runtime contract**

- Command: `node build/index.js --sse [--enable-write] [--enable-bearer]`. `--sse` starts the
  HTTP server. The name is misleading: it serves Streamable HTTP and legacy SSE.
- Port: `3000`, the default (tested). A `--port N` flag exists (from code reading, untested).
- Transport `http`, path `/mcp`, and legacy SSE on `/sse`; both tested. From code reading, `/`
  serves Streamable HTTP too (untested).
- Read-only: leave out `--enable-write`. `rw` adds it.
- Auth: `--enable-bearer` with `BEARER_TOKEN` requires `Authorization: Bearer <token>` on `/mcp`
  and `/sse` (401 without it). Without the flag the endpoints are open.
- Env: `ACTUAL_SERVER_URL`, `ACTUAL_PASSWORD`, `ACTUAL_BUDGET_SYNC_ID`,
  `ACTUAL_BUDGET_ENCRYPTION_PASSWORD`, `ACTUAL_DATA_DIR` (cache dir; default `~/.actual`), and
  `ACTUAL_SYNC_TTL_MS`, which must be `0`. Its default of `60000` lets a read answer from a copy
  up to a minute old. The `~/.actual` default and the 60000 default are from code reading.
  From code reading and the README, `ACTUAL_SESSION_TOKEN` replaces `ACTUAL_PASSWORD` for OpenID
  servers (untested).
- Always set `ACTUAL_BUDGET_SYNC_ID`. From code reading (untested), without it the server opens
  the first budget it lists.
- Secrets from a file: the server runs `dotenv` on `.env` in its working directory (`/app` in
  the image). With the variables only in a file mounted at `/app/.env` (none in the container
  environment), it opened the encrypted budget. So `budget.env` can be mounted read-only at
  `/app/.env`, or delivered as a compose secret at that path, instead of going through `env_file`.
- Logs: `docker logs` of the three giorgiobrullo test containers (`rw`, `ro`, and the one that
  opened the encrypted budget) had no match for the server password, encryption passwords or
  bearer token.
- No `/health` endpoint (`GET /health` returned 404) and no Docker `HEALTHCHECK` (`docker
  inspect` shows none). The container runs as root (`id` gives uid 0). The budget loads on the
  first tool call, not at startup (seen in the logs), so a bad password shows only as a failed
  call.

**Tools** (from `tools/list` on the two instances; `rw` lists 51, `ro` 18, and `ro` is a
subset of `rw`).

Read (18): `get-transactions`, `spending-by-category`, `monthly-summary`, `balance-history`,
`budget-vs-actual`, `net-worth`, `category-trends`, `spending-by-payee`, `cash-flow`,
`get-accounts`, `get-grouped-categories`, `get-payees`, `get-rules`, `get-custom-reports`,
`get-dashboards`, `get-budget-months`, `get-budget-month`, `get-schedules`.

Write (33): `add-dashboard-widget`, `batch-update-transactions`, `create-category`,
`create-category-group`, `create-custom-report`, `create-dashboard-page`, `create-payee`,
`create-rule`, `create-transaction`, `delete-category`, `delete-category-group`,
`delete-custom-report`, `delete-dashboard-page`, `delete-payee`, `delete-rule`,
`delete-transaction`, `hold-budget-for-next-month`, `import-transactions`, `merge-payees`,
`organize-dashboard`, `remove-dashboard-widget`, `rename-dashboard-page`, `reset-budget-hold`,
`run-bank-sync`, `set-budget-amount`, `set-budget-carryover`, `update-category`,
`update-category-group`, `update-custom-report`, `update-dashboard-widget`, `update-payee`,
`update-rule`, `update-transaction`.

Calling `create-transaction` or `delete-transaction` on `ro` returns
`Error: Unknown tool <name>` with `isError: true`. There is no tool that moves money
between categories in one call. That takes two `set-budget-amount` calls.

**Write propagation.** The `rw` instance returns from a write before the change reaches the
server; the push happens a few seconds later. In one run a fresh download about 1 s after
`create-transaction` returned didn't have the change yet; downloads at about 5 s and later did.
A `ro` read right after an `rw` write can miss it. With `ACTUAL_SYNC_TTL_MS=0` the next read
after the push sees it.

**Wrong encryption password** (tool result, `isError: true`):

- wrong password: `Error: Unable to decrypt file with this password. Please try again.`
- no `ACTUAL_BUDGET_ENCRYPTION_PASSWORD`: `Error: File Spike Encrypted is encrypted. Please provide a password.`
  (the budget's name is in the message)

### Versions

`actualbudget/actual-server:26.11.0` (digest
`sha256:06cf186569cf58d93e12659ff1a517b65b5b0ebee7050a3991d4bd8a1d398da6`, also `latest` on
2026-11-08) with `@actual-app/api` `26.11.0` and `@actual-app/core` `26.11.0`.

### S2: headless setup

Both steps work without the web app.

- `GET /account/needs-bootstrap` returns
  `{"status":"ok","data":{"bootstrapped":false,"loginMethod":"password",...}}` on a new server.
- `POST /account/bootstrap` with `{"password":"..."}` sets the first password and returns
  `{"status":"ok","data":{"token":"<uuid>"}}`. A second call returns
  `{"status":"error","reason":"already-bootstrapped"}`.
- `POST /account/login` with `{"loginMethod":"password","password":"..."}` returns
  `{"status":"ok","data":{"token":"<uuid>"}}`, or `{"status":"error","reason":"invalid-password"}`.
- `GET /sync/list-user-files` with header `X-ACTUAL-TOKEN: <token>` lists budgets: `data[]` with
  `name`, `fileId`, `groupId`, `encryptKeyId` (null when not encrypted) and `deleted`. The
  `groupId` is the Sync ID that goes in `ACTUAL_BUDGET_SYNC_ID`.
- `api.runImport(name, fn)` creates a budget and uploads it to the server
  (tested; the new budget appeared in `/sync/list-user-files`). From reading
  `@actual-app/api`'s `dist/index.js` (not checked in the created budget), `api/finish-import`
  calls the upload, and `api/start-import` deletes the default expense categories and groups
  first, so the budget starts with income categories only, plus whatever `fn` creates.
  `budget.sh init` can create the budget itself, and the adopt-the-only-budget path is still
  needed for remote servers.
- Encryption can also be turned on headless: `api.internal.send('key-make', {password})` with
  the budget loaded. The spike used this instead of the web app; the server then listed an
  `encryptKeyId` for that budget. Whether the web app calls the same handler is unverified.

## Amendment 2026-11-09: budgets per profile (CR 0026)

Not built. This replaces "one budget per stack" from the Goal for installs that connect budgets;
the single budget from `ACTUAL_BUDGET_FILE` keeps working unchanged. Boundary's CR 0026 asks for
it, and the stack reply is in `docs/private/cr-0026-stack-reply.md`.

### What 26.11.0 allows

Boundary checked these facts in the pinned `actualbudget/actual-server:26.11.0` image, and they
replace anything this spec or the 2026-11-09 research notes said about API keys:

- The image has no API keys. A sign-in creates a session (`sessions`: `token`, `expires_at`,
  `user_id`, `auth_method`), and the client sends it as `x-actual-token`. `expires_at` is `-1`
  (never) unless `token_expiration` sets a limit, or it follows the OpenID provider.
- Password login always signs in as the same user, so every password client is one identity.
- Access is per budget and all-or-nothing: `user_access (user_id, file_id)` has no permission
  column. Read-only access still has to come from the read-only MCP server.
- Synced changes carry no author, so no credential choice gives attribution.
- Disabling a user ends their sessions.

The vendored MCP server takes `ACTUAL_SESSION_TOKEN` in place of `ACTUAL_PASSWORD`, and the token
wins when both are set (`src/integrations/actual/connection.ts`). That path has not been run.

### Design

**Rows.** A connected budget is one row, keyed by Hermes profile and slug. Its secret file,
`compose/actual/budgets/<slug>.env` (mode 600, gitignored), holds `ACTUAL_SERVER_URL`,
`ACTUAL_BUDGET_SYNC_ID`, `ACTUAL_SESSION_TOKEN` and, if the budget is encrypted,
`ACTUAL_BUDGET_ENCRYPTION_PASSWORD`. The password-free index `compose/actual/budgets.list` holds
one line per row: slug, profile, access (`read` or `write`, default `read`) and server URL with
any credentials masked. The bootstrap reads only the index.

**Credentials.** `connect` accepts `ACTUAL_SESSION_TOKEN` or `ACTUAL_PASSWORD`. With a password,
it signs in through `/account/login` and stores the session token it gets back, never the
password. On an OpenID server the person copies a token from the web app; 26.11.0 has no screen
that shows one.

**Processes.** Both MCP containers get a small launcher as their entrypoint. It starts one
actual-mcp child per row: write-enabled in `actual-mcp-rw`, read-only in `actual-mcp-ro`. Each
child has its own cache directory in the container's cache volume. The launcher routes
`/<slug>/mcp` to its child, and `/mcp` stays the legacy single budget. Compose services,
networks and the secret approach stay the same, with the budgets directory mounted read-only. A
spike decides whether children run on internal ports behind a proxy, or whether the server can
take a path prefix itself.

**Hermes entries.** For each row, the bootstrap registers on the row's profile:

- `budget-<slug>-ro`: the read-only URL, the 18 read tools. Always registered. Cron gets only
  this one.
- `budget-<slug>`: the read/write URL, all 50 manifest tools, for chats. Only when the row's
  access is `write`. The cron fence drops it.

When the profile is `default`, `browser` gets the same entries. Boundary confirmed this. The
shared `api-server` profile gets no connected budgets; it keeps only the single v0.3.12 budget.
A row whose access isn't `read` or `write` registers nothing, which fails closed. The stack-wide
`ACTUAL_ACCESS` in `.env` keeps its v0.3.12 meaning for the single budget only. For a `write` row,
if Hermes's per-platform toolsets can keep `-ro` out of chats, they do. Otherwise chats list the
read tools twice. Registration needs `mcp.instances` in the manifest, which `addon_manifest.py` rejects as
not implemented. Agree its shape with the manifest roadmap work, or give Actual its own
registration function as Twenty has, and move it later.

**Commands.** Boundary calls these commands. They stay supported entry points with the same
keys, checks, errors and output even if budgets later become `addon.sh` instances. In that case
they turn into wrappers, and `connect` also accepts values as JSON on stdin. Credentials never
come from argv.

- `budget.sh connect --json`. Keys: `ACTUAL_SLUG`, `ACTUAL_PROFILE` (default `default`),
  `ACTUAL_SERVER_URL`, `ACTUAL_BUDGET_SYNC_ID`, `ACTUAL_SESSION_TOKEN` or `ACTUAL_PASSWORD`,
  optional `ACTUAL_BUDGET_ENCRYPTION_PASSWORD`, and `ACTUAL_ACCESS` (`read` or `write`, default
  `read`). `--force` replaces an existing connection.
- `budget.sh disconnect <slug> --json`.
- `budget.sh access <slug> read|write --json` changes a row's access without reconnecting.
- `budget.sh status [--slug <slug>] --json` checks each stored credential afresh, outside the
  MCP process. It prints `{"slug","profile","access","state"}` per row. `state` is `ok` or one
  of the error values. `make doctor` reports the same states.

- Before writing anything, `connect` checks the slug, the profile and the credential, using the
  existing `actual-admin.mjs check` with its time limit.
- Both commands recreate the MCP containers and run `make hermes-reload`.
- They print JSON lines ending in `done` or `error`. The errors are `bad_slug`,
  `unknown_profile`, `bad_access`, `login_refused`, `token_expired`, `no_access`,
  `encryption_password`, `timeout`, `server_unreachable` and `exists`. `token_expired` means an
  ended session, whether it ended by expiry, by a disabled user, or by a password change (if
  26.11.0 ends sessions on a password change).
- `disconnect` removes the entries, the secret file and the cache. It leaves the budget on the
  server.

**Host ports, later (CR 0026 R3).** `ACTUAL_MCP_RO_PORT` and `ACTUAL_MCP_RW_PORT`, unset by
default, publish on `127.0.0.1`. Setting either one starts both servers with `--enable-bearer`,
using generated tokens in mode-600 files. There is no open endpoint on the host.

**Skill.** The `budget` skill learns the crm skill's rule: use the budget the user named, and ask
when it's ambiguous.

### Spike before building

- `ACTUAL_SESSION_TOKEN` opens a budget against 26.11.0. How do a bad token and a disabled
  user's token fail (the error text and the `actual-admin.mjs` exit code)?
- What a tool call does after the session behind an already-loaded budget ends: fail, or
  answer from the loaded copy without saying so. Check both the password path and the token
  path. If it answers silently, the launcher restarts that budget's process when its sync fails
  with an ended session, so calls fail with a `token_expired` tool error.
- Whether changing the server password ends sessions from earlier password sign-ins.
- Whether a user without access to a budget can open it by sync id (expect `no_access`).
- Path prefix on the vendored server, or a proxy to internal ports. Memory per child with a
  realistic budget.
- Whether Hermes v0.22.7 per-platform toolsets can keep `budget-<slug>-ro` out of chats while
  cron keeps it.
- The `Mcp-Session-Id` lifetime claim (until `DELETE` or restart, no idle expiry). It comes from
  reading `src/index.ts`, and the spike runs it.

### Settled with Boundary (CR 0026, round 2)

- `browser` mirrors `default`.
- "Unattended runs" means the person's own profile's cron. The shared `api-server` profile keeps
  only the single budget.
- Access is per budget, defaults to `read`, and can be changed without reconnecting.
- Boundary detects ended sessions with `budget.sh status`, not from tool errors, which Hermes
  passes to the model rather than to Boundary.
- Boundary asks only for a password on password servers. For a remote OpenID server, the person
  copies the token from the web app.
