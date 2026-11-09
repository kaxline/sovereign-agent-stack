# CRM (Twenty)

[Twenty](https://github.com/twentyhq/twenty) is an open-source CRM: people,
companies, opportunities, notes and tasks, with a web app and its own MCP
endpoint. Hermes uses that endpoint, so it reads and edits the same records
you see. Run Twenty in this stack (the **`crm`** profile), connect a Twenty
you already have, or both. Every workspace becomes one MCP server in Hermes,
named `crm-<workspace>`.

## Local quick path

```bash
./scripts/setup.sh --crm
make up                 # the first start runs Twenty's migrations, about a minute
make crm-init           # owner account, workspace "main", Hermes' two keys
make hermes-reload
make doctor
make crm-open           # opens the workspace signed in as the owner
```

In WebUI: *Add Ada Example from Acme as a contact.* Then in a new chat:
*Who do we know at Acme?*

## Remote quick path

Create two API keys in your Twenty workspace, in Settings under the API
section (Twenty's own docs label it differently between versions). Give one a
role that can read and write records and the other a read-only role. Then:

```bash
make crm-add SLUG=hosted URL=https://crm.example.com   # asks for both keys
make hermes-reload
```

`crm-add` runs an MCP `initialize` with each key and writes nothing if Twenty
rejects one or the URL doesn't answer. The read-only key is optional. Without
one, unattended runs (`api-server`) don't get this CRM at all. For a
non-interactive run, put `TWENTY_KEY_RW`, `TWENTY_KEY_RO` and, if you want them,
`TWENTY_ACCESS` and `TWENTY_WORKSPACE_URL` in the environment; nothing is asked.
No containers start for a remote workspace. `make crm-remove SLUG=hosted`
forgets it again.

An app that manages add-ons connects a workspace through
`scripts/addon.sh connect twenty --instance <slug>`, with a JSON object on
stdin: `url`, `access` (`read` or `write`), `local`, `key_ro` and `key_rw`.
The manifest's connect hook runs `crm.sh connect`, which checks each key with
an MCP `initialize` before anything is stored. `disconnect` removes the
instance and its keys. `crm.sh add`, `remove` and `access` write the same
store.

## How it works

| Piece | Role |
|---|---|
| `twenty-server` | Web app, API and MCP at `http://twenty-server:3000/mcp`. Published on `127.0.0.1:${TWENTY_PORT}` (default 3000). Runs Twenty's database migrations on every start |
| `twenty-worker` | Background jobs. Same image, `yarn worker:prod`, no migrations |
| `twenty-db` | Twenty's own Postgres 16, on the internal `crm` network |
| `twenty-redis` | Twenty's queue and cache, `--maxmemory-policy noeviction`, on the `crm` network |
| `data/addons/twenty/` | The add-on's store: `instances/<slug>.json` and `secrets/<slug>.key_ro`, `<slug>.key_rw` (mode 600), one pair per workspace, local or remote. `make crm-init` and `make crm-add` write them |
| Hermes bootstrap | Registers each instance as `mcp_servers.crm-<slug>` when `TWENTY_MCP_ENABLED=1`, through the manifest's `mcp.instances` |
| `crm` skill | Tells Hermes how to pick a workspace and reach records (`compose/hermes/skills/crm/`) |

Twenty doesn't use the [shared Postgres](database.md). It pins Postgres 16
(the shared server runs 18), creates schemas per workspace while it runs, and
needs a Redis that never evicts, because Redis holds its job queue.

An instance file holds:

```json
{
  "url": "http://twenty-server:3000",
  "access": "write",
  "local": true,
  "workspace_url": "http://localhost:3000/",
  "workspace_id": "..."
}
```

`url` is the instance without `/mcp` (a hosted one reads
`https://crm.example.com`). `access` is `read` or `write`. `workspace_url` is
where `make crm-open` goes, and `workspace_id` is set for local workspaces
only. The keys are separate files in `secrets/`; both are optional.

The API key alone selects its workspace, so every entry points at the
instance's `/mcp`, whatever the workspace's own hostname.

### Two keys, enforced by Twenty

| Hermes profile | Used by | Key |
|---|---|---|
| `api-server` | unattended API, n8n | the read-only key; no entry when there is none |
| default | dashboard, CLI, Boundary | the read/write key, or the read-only key with access `read` |
| `browser` | Hermes WebUI | same as default |

Access `read` lets chats look things up without changing anything. When
the key that the access setting calls for is missing, that profile gets no entry
for the workspace, and the bootstrap log and `make doctor` say why.

To change it later, without the keys at hand:

```bash
make crm-access SLUG=main ACCESS=read   # or ACCESS=write
make hermes-reload
```

`crm-access` refuses `read` when the workspace has no read-only key, and
`write` when it has no read/write key. `make crm-init ROTATE=1` mints new keys
and keeps the access setting.

The Hermes bootstraps read the workspaces from the add-on's instance store
(`data/addons/twenty/instances/<slug>.json` and `secrets/<slug>.key_ro|key_rw`,
mounted read-only) and write each key into that profile's `.env` under
`data/hermes/` as `CRM_<SLUG>_KEY` (`crm-sales-eu` uses `CRM_SALES_EU_KEY`).
Keys stored by earlier versions as `TWENTY_<SLUG>_KEY` are removed.
`config.yaml` holds only the reference:

```yaml
mcp_servers:
  crm-main:
    url: http://twenty-server:3000/mcp
    headers:
      Authorization: Bearer ${CRM_MAIN_KEY}
```

On a local instance `make crm-init` creates two roles for those keys:

- **Hermes read-only** reads every record and changes nothing.
- **Hermes read-write** reads, edits and soft-deletes records. It can't
  destroy records, change settings or run Twenty's own AI tools.

On Twenty v2.55 the role-wide "no updates" setting doesn't cover junction
and system objects such as `noteTarget`, `taskTarget` and `attachment`: a
read-only key could still create them. `crm-init` closes that by giving the
read-only role an explicit read-only permission on every object. Twenty
refuses those permissions on objects it marks read-only in its own UI (agent
messages, short links and a few others), and those objects have no write
tools. A custom object you add later is covered by the role-wide setting. A
new *system* object from a Twenty upgrade isn't covered until you run
`make crm-init ROTATE=1`. For a remote instance, check what your read-only
role can write before you trust it on unattended runs.

### Signing in

`make crm-open` opens a local workspace already signed in as the owner, since
nobody chose that account's password. It runs `./scripts/crm.sh login-link`,
which signs in to Twenty's API with the owner's password and prints one line,
`<TWENTY_WORKSPACE_URL>verify?loginToken=<token>`. Nothing else goes to
stdout, so another app can run it and open the URL itself. The token gives a
browser an owner session, which includes server admin. Twenty accepts it for
`LOGIN_TOKEN_EXPIRES_IN`, 15 minutes by default. `login-link` refuses remote
workspaces; sign in on their own Twenty. When it fails, `crm-open` opens the
sign-in page and says where the password is.

### Upgrading from workspace files

Before stack 0.3.13, each workspace was a file,
`compose/twenty/workspaces/<slug>.env`. `./scripts/crm.sh migrate` imports
those files into the store and renames each to `<slug>.env.migrated`. `make
ensure-local` runs it, and so do `crm.sh init`, `add`, `remove`, `access`,
`login-link` and `open`. It keeps a workspace already in the store and leaves
its file where it is, with a warning. It skips, with a message and without
renaming:

- a name that doesn't start with a letter or is longer than 31 characters;
- a key that holds whitespace or a control character;
- a URL that isn't `http(s)` or has `user:password@` in it;
- a value with a quote, backslash or control character;
- a `TWENTY_ACCESS` other than `read` or `write`.

Fix the file and run `migrate` again. `make doctor` warns about every file
still waiting. The profile key names changed with it: `TWENTY_<SLUG>_KEY` in
the Hermes profile `.env` files is now `CRM_<SLUG>_KEY`, and the bootstrap
removes the old lines. `compose/twenty/workspace.env.example` documents the
old format and nothing copies it.

### Picking up a new workspace

`make hermes-reload` re-runs both bootstraps and recreates Hermes. On Hermes
v0.22.7 the recreate isn't needed: running the bootstraps alone
(`docker compose run --rm hermes-api-bootstrap` and `hermes-browser-bootstrap`)
is enough. The running Hermes connects the new `crm-<slug>` server within
about a second, and open chats can use it on their next turn.

### What Hermes sees

Twenty's MCP lists seven tools however many objects the workspace has:
`get_tool_catalog`, `learn_tools`, `execute_tool`,
`list_object_metadata_names`, `list_skills`, `load_skills` and
`search_help_center`. All seven are pinned. Record tools such as
`find_many_people` or `create_one_company` are listed by `get_tool_catalog`,
described by `learn_tools` and run through `execute_tool`. On the read-only
key, write tools are missing from the catalog.

## Single and multi-workspace

`TWENTY_MULTIWORKSPACE` sets the mode for the local instance only. A remote
instance's mode is up to whoever runs it; add each of its workspaces with
`make crm-add`.

| | Single (`0`, default) | Multi (`1`) |
|---|---|---|
| Workspaces | one, slug `main` | as many as you create |
| Web app | `http://localhost:3000` | `http://<subdomain>.localhost:3000` |
| Add one | — | `make crm-init SLUG=sales` |

```bash
make crm-mode MULTI=1          # restarts server and worker
make crm-init SLUG=sales       # http://sales.localhost:3000, Hermes sees crm-sales
make hermes-reload
```

Switching an instance from single to multi keeps its data and keys. The
existing workspace moves to the subdomain Twenty gave it when it was created
(often taken from the owner's email, so `crm`), and `make crm-mode` prints the
new URL and updates `TWENTY_WORKSPACE_URL`. Switching back is refused while
the instance has more than one workspace; that direction hasn't been tested
with data.

Chrome and other Chromium browsers send `*.localhost` to your own machine
(checked). Firefox and Safari haven't been checked here; if a subdomain
doesn't load, use a Chromium browser or add the name to `/etc/hosts`.

## Memory and disk

Measured on v2.55 by sampling `docker stats` every two seconds through a
5,000-record import, 200 MCP `find_many_people` calls and a restart:

| Container | Idle | Peak | Reservation | Limit |
|---|---|---|---|---|
| `twenty-server` | 710–850 MiB | 851 MiB | 1 GiB | 1152 MiB |
| `twenty-worker` | 740–870 MiB | 867 MiB | 1 GiB | 1152 MiB |
| `twenty-db` | 60–120 MiB | 119 MiB | 128 MiB | 256 MiB |
| `twenty-redis` | 20–30 MiB | 28 MiB | 32 MiB | 128 MiB |

About 1.8 GiB in total, and close to no CPU when idle. Redis runs with
`--maxmemory 96mb`, so a full queue refuses writes instead of the container
being killed. The load didn't cover workflows, email sync or large exports.
If a container is killed for memory under heavier use, raise its limit in
`docker-compose.override.yml`. The image is 1.12 GB.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `TWENTY_LOCAL_ENABLED` | `0` | Starts the local instance (profile `crm`). While `0`, Hermes gets no entry for local workspaces |
| `TWENTY_MCP_ENABLED` | `0` | Registers every stored workspace in Hermes (profile `crm-mcp`, which selects no service). `crm-init` and `crm-add` set it to `1` |
| `TWENTY_MCP_TIMEOUT`, `TWENTY_MCP_CONNECT_TIMEOUT` | manifest: 60 and 30 | Seconds Hermes waits for a call and for the first connection, on every `crm-*` server |
| `TWENTY_MULTIWORKSPACE` | `0` | Local instance: one workspace (`0`) or several on subdomains (`1`). Change it with `make crm-mode` |
| `TWENTY_PORT` | `3000` | Host port on `127.0.0.1` |
| `TWENTY_IMAGE` | `twentycrm/twenty:v2.55.0` | Pinned. The server migrates its database on start, so a bump can't be undone by changing it back: back up first |
| `TWENTY_DATA_DIR` | `./data/twenty` | `db/` (Postgres) and `storage/` (attachments). Not moved by `ASSISTANT_DATA_ROOT` |
| `TWENTY_ENCRYPTION_KEY` | generated by `setup.sh` | Encrypts the secrets Twenty stores. Losing it loses them |
| `TWENTY_DB_PASSWORD` | generated by `setup.sh` | Postgres password, hex |
| `TWENTY_ENCRYPTION_KEY_FILE`, `TWENTY_DB_PASSWORD_FILE` | unset | Host paths to files holding the two secrets, used instead of the values |
| `TWENTY_USERNAME`, `TWENTY_PASSWORD` | written by `make crm-init` | The local owner account (`owner@crm.localhost`, generated password) |
| `TWENTY_PASSWORD_FILE` | unset | Set before the first `crm-init` and the generated owner password goes to this file (mode 600) instead of `.env`; later runs read it there. Setting it as well as `TWENTY_PASSWORD` is refused |

### Secrets from files

Set `TWENTY_ENCRYPTION_KEY_FILE` and `TWENTY_DB_PASSWORD_FILE` to files on the
host and leave `TWENTY_ENCRYPTION_KEY` and `TWENTY_DB_PASSWORD` empty. Compose
mounts the files as secrets. Postgres reads its password file itself, and
`compose/twenty/secrets-entrypoint.sh` reads both for the server and worker,
so neither secret appears in `docker inspect`. Setting a value and its file
together stops the container at start, with an error that names the variable.
The database password may contain any characters; the entrypoint
percent-encodes it for the connection URL.

The Hermes API keys stay in each profile's `.env`, as every MCP key does.

## Backup

`make db-backup` covers the shared Postgres, not Twenty. `TWENTY_DATA_DIR`
holds a live Postgres data folder, so copy it only with the instance stopped
(`TWENTY_LOCAL_ENABLED=0`, `make up`), or dump it while it runs:

```bash
docker compose exec -T twenty-db pg_dump -U twenty twenty > twenty-$(date +%F).sql
```

A restore needs `TWENTY_ENCRYPTION_KEY` and `TWENTY_DB_PASSWORD` (or their
files) from the same install; keep them with the backup. The owner password
and `data/addons/twenty/` are worth keeping too, but can be recreated:
`make crm-init ROTATE=1` mints new keys. Back up before changing
`TWENTY_IMAGE`.

## Turning it off

Set `TWENTY_LOCAL_ENABLED=0` and run `make up` to stop the local instance;
its data stays in `TWENTY_DATA_DIR`. The bootstraps that `make up` reruns
then drop the `crm-*` entries and keys of every local workspace, and keep
the store. Remote workspaces stay
registered, so moving to a hosted Twenty needs only `make crm-add` and this.
Setting it back to `1` registers the local workspaces again. Set `TWENTY_MCP_ENABLED=0` and run
`make hermes-reload` to remove every `crm-*` entry and key from Hermes.
`make crm-remove SLUG=name` and `make hermes-reload` remove one workspace
only.

## Troubleshooting

- **`make crm-init` waits, then fails:** the first start runs every
  migration. `docker compose logs twenty-server` shows progress.
- **`Password too weak`:** Twenty accepts passwords of 8 to 50 characters.
  `crm-init` checks this before it calls Twenty.
- **`Workspace creation is restricted to admins`:** the owner isn't a server
  admin. `crm-init` grants it on every run, so running it again fixes this.
  For an account created another way, run `./scripts/crm.sh grant-admin <email>`.
- **`crm-init` stopped partway:** run it again. Once Twenty has created the
  workspace, its id is in the instance file, so the next run picks up that
  workspace, revokes any keys the failed run minted and mints new ones. Until
  then the workspace has no keys, and Hermes skips it.
- **`make doctor` says Twenty rejects a key:** the key was revoked or the
  workspace deleted. `make crm-init ROTATE=1` mints new keys for a local
  workspace. For a remote one, create new keys in Twenty and run
  `make crm-add SLUG=… URL=… FORCE=1`.
- **A subdomain doesn't load:** see the browser note above.

## License

Twenty is mostly AGPL-3.0. Some of its files carry a commercial license and
are marked `/* @license Enterprise */`; using those in production requires
agreeing to Twenty's commercial terms. The stack runs the unmodified `twentycrm/twenty` image
pulled at run time and talks to it only over its API and MCP, so this repo's
MIT license is unaffected. Don't copy Twenty source into this repo. The
Twenty name and logo belong to the Twenty project; refer to it by name and
don't present it as part of this stack.
