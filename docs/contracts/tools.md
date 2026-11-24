# Tools contract: MCP add-ons

An add-on is an optional service that gives the agent tools over MCP. It is
described by one manifest, `addons/<short-id>/addon.json`, and configured
through one lifecycle CLI, `scripts/addon.sh`, which writes its values to the
instance store. An agent adapter registers add-on tools from the plans that
`scripts/lib/addon_manifest.py` prints, so the same manifests and store can
feed a different agent runtime with its own adapter. Hermes is the one agent
today; its side is in [agent.md](agent.md). An identity is the agent-side
notion of who a run acts as, with its own tools and access level; Hermes
implements an identity as a profile, and manifests name identities by their
Hermes profile names (`default`, `api-server`, `browser`).

The format is experimental while the stack is in 0.3.x. Field names can change
between patch releases, with a changelog entry.

## Manifest

`scripts/lib/addon_manifest.py validate` checks every manifest. Only built-in
add-ons (manifests in `addons/`) are accepted. The full field rules are in
[addons/README.md](../../addons/README.md); this table says what each field is
for.

| Field | For |
|---|---|
| `manifestVersion`, `id`, `version`, `title`, `tagline`, `description`, `publisher`, `minStackVersion`, `maxStackVersion` | Naming, versioning and catalog text. `id` is reverse-domain; its last segment is the short id and the directory name |
| `postInstallMessage` | Markdown passed through by `addon.sh list --json` |
| `builtin` | `{flag, profile, seed}`: the `.env` flag that turns the add-on on and the compose profile it switches |
| `gates` | More `{flag, profile, seed}` rows, so a second flag can switch on part of an add-on |
| `services` | One entry per compose service: `compose` (the service name), `port`, `when: {gate}`, and `mounts` for the instance store (below) |
| `uses` | Stack services the add-on needs (`llm`, `search`, `storage`, `postgres`, `neo4j`, `redis`, `secrets`, `scheduler`) |
| `permissions` | `reads` (personal-data classes), `writes`, `sends`, `exposesToAgent`, `egress`, and `cron` (`true`, `false` or `"ro"`) |
| `mcp` | One server entry or a list: `name`, `title`, `service`, `path`, `transport`, `timeout`, `connectTimeout`, `tools`, `pin`, `writeTools`, `profiles`, `env`, `when`, and `auth` (`{header, scheme, env: {ro, rw}}`, the variable holding the token for each level) |
| `mcp.instances` | In place of `service`, `path` and `name`: one server per stored instance, with `name` (`<prefix>{slug}`), a `url` template (`{setting:...}`, `{access}`, `{slug}`), and optional `access`, `auth` and `gate` |
| `vaultBackend` | A login backend for the agent's browser vault: `name`, `plugin`, `toolset`, `tools`, `profiles` |
| `settings` | The add-on's settings schema, each with a `type`, optional `default`, `label`, `optional` and, for a `bool`, a gate `flag` |
| `instances` | `{name, settings}`: a settings schema stored once per instance slug, plus `optional`, `errors` and the `connect`/`disconnect` hooks |
| `commands` | `{service, instance, argv}` entries that `addon.sh run` runs in a service |
| `hooks` | `prepare`, run on the host by every `addon.sh enable` before any service starts, and `init`, run once after the services start (`--reinit` runs it again). Each is `{command}` (a `commands` entry without an instance) or `{script}` (a `scripts/<name>.sh` argv); `prepare` must be a script |
| `data` | Bind-mount paths and compose volumes that `addon.sh remove --delete-data` deletes |
| `backup` | `{secrets, recommended}`: lists of env var names that `addon.sh list --json` passes to callers that offer a backup |
| `host` | A part that runs on the host instead of in Docker (`email-local` and `messages` use it); `addon.sh list --json` reports its `platform` |
| `gateway` | `{platform, sync}`: the Hermes chat platform the add-on switches and the `scripts/<name>.sh` that seeds or clears it. A gateway add-on may have no `services` |
| `share` | `{levels, forward, memberKeys, handout}`: whether and how one instance may be shared, with `levels` naming keys of `mcp.instances.auth.secret` |
| `store` | Display facts for a store front; only `dataLeaves`, required when `permissions.egress` includes `internet` |
| `web` | `{service, path}`: the page a person opens, on a service that has a `port` |
| `generate` | `{env, encoding, bytes}` entries: `.env` secrets `addon.sh enable` writes when `.env` has none (`hex` or `base64`, 16 to 64 bytes) |

Rules a reader of the plans can rely on, all enforced by `validate`:

- An add-on with `permissions.reads` may not send, may not run in cron, and
  may not register on `api-server`, the unattended identity.
- `mcp.profiles` and `vaultBackend.profiles` map each identity to `ro` or
  `rw`. For an `ro` identity, plans leave out the entry's `writeTools`.
- A service that mounts `secrets` may not be an MCP service.
- Two `mcp` entries may not share a service, and an `mcp.name` may not take
  a name core registers itself (`searxng`, `opencode`, `messages`).

### Removed in 0.3.14

`onToggle`, `services.<name>.volumes` (use `data.volumes`),
`services.<name>.oneshot`, `mcp.unmanaged` and the `upgrade`, `backup` and
`restore` hooks are gone. `validate` refuses the first four with a message
saying so, and the three hooks as unknown fields of `hooks`. Packaged add-ons
and feeds are design only.

A one-shot service is no longer marked in the manifest. A service is a
one-shot when another compose service lists it in `depends_on` with
`condition: service_completed_successfully`, written out in that service
rather than through a YAML merge key (`compose_oneshots()` in
`addon_manifest.py`). `addon.sh enable` runs it to completion first.

## Instance store

Each add-on keeps its settings, instances and secrets under
`<data root>/addons/<short id>/`. The layout, file modes and write rules are on
the storage page under [add-on store](storage.md#add-on-store).

An instance file (`instances/<slug>.json`) may hold `bind`, the stack's per-instance binding: a map from
identity to `ro` or `rw` that replaces `mcp.profiles` for that instance. An
identity it leaves out gets no entry. It may raise `ro` to `rw`, but never on
`api-server`, the unattended identity. `addon_manifest.bind_problem()` has
the rules, and `addon.sh connect` and `configure --instance` write it.

A slug is a lowercase letter followed by up to 30 lowercase letters, digits
or `-`: `^[a-z][a-z0-9-]{0,30}\Z` (`SLUG_RE` in `addon_store.py`). An adapter
that reads the store validates slugs against the same rule and skips a file
whose name fails it. `addon_manifest.py` keeps a copy as `STORE_SLUG_RE`, and
the Actual launcher has its own in `compose/actual-mcp/src/launcher/rows.ts`.

A service whose manifest lists `mounts` gets `config/`, `instances/` and
`secrets/` at `/run/addon/config`, `/run/addon/instances` and
`/run/addon/secrets`, read-only or read-write as listed.
`scripts/test-addon-manifests.py` checks that the compose service declares
exactly those mounts.

## Self-contained adapters

An adapter is the container that serves an add-on's tools. A self-contained
adapter:

- builds from its own `compose/<name>/` directory;
- imports nothing from `scripts/lib`;
- gets its configuration only through documented environment variables, the
  instance store mounted read-only, and secret files;
- has a `VENDORED.md` when its code comes from upstream, naming the upstream
  commit and the local changes (`compose/actual-mcp/VENDORED.md` is one).

Host-side lifecycle scripts such as `scripts/budget.sh`, `scripts/crm.sh` and
`scripts/vault.sh` are stack glue, not adapters, and may use `scripts/lib`.
Host code may also import an adapter's code from its `compose/<name>/`
directory: `scripts/mail-index.py` and `scripts/mail-drafts.py` run the files
in `compose/email-imap/`, so the host tools and the image share one copy.

## Plans for the agent side

These are the neutral outputs: lines of fields, not agent configuration. An
agent adapter reads them and writes its own configuration. The `<identity>`
argument is an identity name as `mcp.profiles` spells it. Each line holds fields
separated by `|`, and a value containing `|` or a newline makes the line be
skipped with a warning on stderr. Invalid manifests are reported on stderr.

`addon_manifest.py mcp-plan <addons-dir> <identity>` prints one line per
fixed `mcp` entry:

```text
name|title|enabled|access|url|timeout|connect_timeout|transport|tools|pins|all_urls|keysrc|keyenv|header|scheme
```

`enabled` is `1` or `0` from the entry's `when.gate` flag, else
`builtin.flag`. `access` is `rw`, `ro`, or empty when the identity gets no
entry. `tools`, `pins` and `all_urls` are space-separated, and at `ro` `tools`
and `pins` leave out `writeTools`. `all_urls` lists every URL the entry can
register, so the adapter can drop the ones this identity must not keep. For an
entry with `auth`, `keysrc` is the variable holding the token for this
identity's level (empty when the identity gets no entry), `keyenv` the
variable the adapter copies it to (`instance_key_env()` of the name), and
`header` and `scheme` come from `auth`. Without `auth` all four are empty. The
plan never prints a token. An invalid manifest still gets a line with
`enabled` `0` for each URL no valid manifest registers, so its stale entries
are dropped.

`addon_manifest.py instances-plan <addons-dir> <data-dir> <identity>` prints,
for each `mcp.instances` entry, one line for the add-on and then one per
stored instance:

```text
name|title|on|url|keyenv|keyfile|timeout|connect|pins|reason|header|scheme|tools|headers|trust
```

The add-on's line has `name` `<prefix>*`, `on` from its flag and `reason` the
flag's name. The identity's level is `ro` or `rw`, from the instance's `bind`
when it has one, else from `mcp.profiles`; an `access` setting of `read` caps
it at `ro`. The level fills `{access}` in the URL and picks the key. An
instance line is on (`1`) when the flag is on, the identity has a level, the
URL is http(s), the gate lets it through, the access setting (if any) is
`read` or `write`, and the key file it needs (if any) holds one line.
Otherwise `on` is `0` and `reason` says which check failed. `keyenv` is the
variable name the key goes under (`crm-sales-eu` gives `CRM_SALES_EU_KEY`);
`keyfile` is a path under `<data-dir>`. An entry without `auth` has empty
`keyenv`, `keyfile`, `header` and `scheme`, and the adapter registers it with
`tools`, which leaves out `writeTools` at `ro`. The plan never prints a key.
`headers` is a JSON object of filled headers for an entry with
`mcp.instances.headers`, else empty. `trust` is `untrusted` for an instance
whose stored `trust` is `untrusted` and empty otherwise; an adapter makes the
agent ask before each call that is not read-only on an untrusted instance.

`addon_manifest.py vault-plan <addons-dir> <identity>` prints one line per
add-on with a `vaultBackend`:

```text
name|enabled|access|plugin|tools
```

`plugin` is the repo-relative directory of the plugin; `tools` is
space-separated.

`addon_manifest.py mcp-urls <addons-dir>` prints every fixed MCP URL of every
add-on, one per line, whatever the flags and identities say. It registers
nothing; it is for tuning entries that already exist.

`cron_fence(addons_dir, environ=os.environ, data_dir=None)` is a function, not
a subcommand. It returns `(names, urls)`, the sorted MCP server names and URLs
to keep out of scheduled runs: every entry of every add-on whose
`permissions.cron` is not `true`. With `cron` `"ro"`, a fixed entry keeps its
name and its `ro` URL and loses the others, and `mcp.instances` entries stay at
the level of the identity the run uses, with each instance's `rw` URL fenced
when the URL has `{access}`. An instance with a stored `endpoint` is fenced
whatever `cron` says. An invalid manifest is fenced by whatever name it
carries. For any other `cron` value, an `mcp.instances` URL with no
placeholders is fenced as it is, and with `data_dir` each instance in
`<data_dir>/<short id>/` is fenced by its name and URLs. `cron_fence_toolsets(addons_dir)` returns
`(name, toolset)` pairs for the `vaultBackend` toolsets kept out the same
way.

## Lifecycle CLI

`scripts/addon.sh` lists, configures, enables, disables, connects and
disconnects instances, runs commands, and removes add-ons. `enable`,
`disable`, `connect`, `disconnect` and `remove` take `--json`, which prints one
JSON object per line and ends with one `done` or `error` line. Commands, exit
codes, line shapes, hooks and locking are in
[extending.md](../extending.md#add-ons-scriptsaddonsh).
