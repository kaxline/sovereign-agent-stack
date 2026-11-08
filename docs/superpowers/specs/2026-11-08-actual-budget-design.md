# Actual Budget add-on

Status: draft for review, 2026-11-08. Branch `feat/actual-budget`.

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
ACTUAL_ACCESS=write                            # read | write, default write
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
   lets Boundary offer "look things up only". The bootstrap services get `ACTUAL_ACCESS` from the
   budget file through `env_file` with `required: false`, so a changed file re-runs the bootstrap
   as Twenty's workspace files do.

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
