# Built-in add-ons

Each directory holds the `addon.json` manifest of one optional service whose
containers are already in `docker-compose.yml`. The directory is named after
the last segment of the manifest's `id`. The format is experimental while the
stack is in 0.3.x, and field names can change between patch releases, with a
changelog entry. The add-ons so far are GPT Researcher (`gptr/`), Actual
Budget (`actual/`), Bitwarden (`bitwarden/`), Twenty CRM (`twenty/`) and
Email on this Mac (`email-local/`: the Mail.app `mail` and `drafts` servers)
and Email over IMAP (`email/`: containers that hold the account passwords,
registered as `email` and `email-drafts`); both are described in
[docs/email.md](../docs/email.md). The other optional services are still
wired by hand.

What reads a manifest:

- `scripts/sync-profiles.sh` takes the `builtin` block (`flag`, `profile`,
  `seed`) as one more row of its flag-to-profile table.
- Both `hermes-*-bootstrap` services mount this directory and
  `scripts/lib/addon_manifest.py`, and register the `mcp` block on each
  profile listed in `mcp.profiles` while the flag is on. A profile left out
  gets no entry, and turning the flag off drops the entries.

Optional fields beyond the basics, checked by `addon_manifest.py validate`
except where noted:

- `gates`: a list of `{flag, profile, seed}` for built-in add-ons, each one
  more flag-to-profile row after `builtin`, so a second flag can switch on
  part of an add-on. A gate flag must differ from `builtin.flag` and from the
  other gates. Keys appear in the order `flag`, `profile`, `seed`, one per
  line, because `sync-profiles.sh` reads them line by line with `sed`.
- `mcp`: one object or a non-empty list of them. Each entry may set its own
  `title` and `when: {"gate": FLAG}`; the entry is registered while that gate
  flag is on, else while `builtin.flag` is on. Names must be unique within the
  add-on, and each entry needs its own service: two entries may not name the
  same service key, directly or through a `{ro, rw}` map, because the
  bootstrap drops stale registrations by URL.
- `mcp.instances` (built-in add-ons only): an `mcp` entry that registers one
  server per instance in the add-on's store, instead of one fixed server. It
  has no `service`, `path` or `name`. Its fields, besides the usual
  `transport`, `timeout`, `connectTimeout`, `env`, `tools`, `pin`,
  `writeTools` and `profiles`:
  - `name`: a prefix, then `{slug}` once, such as `crm-{slug}`. The prefix may
    not be a core server name or prefix. The bootstrap drops every entry under
    the prefix that has no instance, so no add-on's fixed `mcp.name` may start
    with another add-on's instance prefix, and two instance prefixes may not
    start with one another.
  - `url`: a string with `{setting:<name>}` placeholders that name `url` or
    `string` instance settings, such as `{setting:url}/mcp`.
  - `access`: `{setting:<name>}` naming a `choice` instance setting whose
    `options` are only `read` and `write`. `read` registers the instance with
    its `ro` key and `write` with its `rw` key. A profile that `mcp.profiles`
    maps to `ro` gets the `ro` key whatever the setting (Twenty maps
    `api-server` to `ro`), and a profile whose key is missing gets no entry.
  - `auth`: `{header, scheme, secret: {ro, rw}}`. `secret` names two `secret`
    instance settings. The bootstrap writes the chosen key into the profile's
    `.env` as `<NAME>_KEY`, with `-` turned into `_` and the name uppercased
    (`CRM_SALES_EU_KEY`), and the header carries `<scheme> ${<NAME>_KEY}`.
  - `gate` (optional): `{setting, flag}`. An instance whose bool `setting` is
    true registers only while `flag` (`builtin.flag` or a gate) is on.
  For instance entries the bootstrap writes no `tools.include`: `tools` only
  bounds what `pin` and `writeTools` may name. Of `env` it reads `timeout` and
  `connectTimeout`; `env.url` and `env.access` are ignored.
  The entry needs a top-level `instances` block that declares the settings it
  names. Removing an instance drops its entries and keys on the next
  bootstrap. `addons/twenty/addon.json` is the one user.
- `services.<name>.when`: `{"gate": FLAG}`, so the service starts only while
  that gate flag is on. `mounts` maps `secrets`, `config` and `instances` to
  `ro` or `rw`; they mount the directories `data/addons/<short-id>/secrets`,
  `config` (which holds `settings.env`) and `instances` at
  `/run/addon/secrets`, `/run/addon/config` and `/run/addon/instances`. The
  compose service must declare exactly those mounts, with sources written as
  `${ASSISTANT_DATA_ROOT:-./data}/addons/<short-id>/<name>`;
  `scripts/test-addon-manifests.py` checks this, `validate` does not. A
  service that mounts `secrets` may not be an `mcp` service.
  `oneshot: true` (built-in only) marks a service that runs to completion:
  `addon.sh enable` runs it with `up --exit-code-from` before the other
  services, and it may not have `when` or be the `mcp` target. `volumes` is the old place for
  compose volume names; use `data.volumes`. Both are accepted in 0.3.13, and
  a manifest with both is refused.
- `settings`: each setting has a `type` (`string`, `int`, `bool`, `choice`,
  `port`, `secret`, `url`, `path`, `list`, `date`), an optional `default`,
  `label` and `optional`. A setting with neither `default` nor
  `optional: true` is required, and `addon.sh enable` refuses while one has no
  value. A `secret` setting may not have a `default`. A `bool` setting may
  name a `flag`, which must be a gate flag, not `builtin.flag`. Two names that
  differ only by case, `-` or `_` are refused, because they share one
  `settings.env` key.
- `instances`: `{name, settings}`, a settings schema stored once per instance
  slug. `flag` is not allowed inside it, and the name and default rules above
  apply. `addon.sh enable` also needs at least one instance, and reports
  `instances:<name>` in `missing` until there is one, unless
  `instances.optional` is `true` (built-in only). A `secret` instance setting
  may set `transient: true`: `addon.sh connect` passes the value to the
  `instances.connect` hook and never stores it. It needs that hook.
  `instances.connect` and `instances.disconnect` are hooks (below), and
  `instances.errors` lists the error names those hooks may report besides
  the shared ones.
  Twenty sets `optional`, so it can be enabled before any workspace is connected.
- `commands`: `{<name>: {service, instance, argv}}`. `addon.sh run` runs
  `argv` in the named service, and appends `--account <slug>` when `instance`
  is true. An add-on that reads personal data may not run a command in an MCP
  service.
- `hooks`: `init`, `upgrade`, `backup` and `restore`, each
  `{"script": [argv]}` (`scripts/<name>.sh` plus arguments, run on the host)
  or `{"command": <name>}` (an entry of `commands` with `instance: false`).
  `addon.sh` runs `init` on `enable`, and `connect` and `disconnect` from
  `instances`; it doesn't run the others yet.
- `onToggle`: `{"recreate": [<compose service>], "script"?: [argv]}`, built-in
  only. `enable` and `disable` recreate those services, then run the script,
  when the add-on's flag changes value. No built-in manifest uses it.
- `data`: `{"paths": [{"env", "default"}], "volumes": [<compose volume>]}`,
  built-in only. `addon.sh remove --delete-data` deletes them. A path's
  `default` is `./data/...` and must be a bind-mount source in
  `docker-compose.yml`, or its parent.
- `backup`: `{"secrets": [<env name>], "recommended": [<env name>]}`, listed
  by `addon.sh list --json` for callers that offer a backup.
- `host`: a part that runs on the host instead of in Docker, with
  `platform`, `install`, `uninstall`, `grants`, `grantTarget` and `check`.
  `addon.sh list --json` reports `platform` (`any` when there is no `host`);
  nothing installs the host part. Refused from a community feed.
  `email-local` uses it for the Mail.app helper apps.

`scripts/addon.sh` reads these; see [docs/extending.md](../docs/extending.md#add-ons-scriptsaddonsh).

`sync-profiles.sh` runs on a first install without host python3 and reads the
`builtin` block with `sed`, so manifests must stay in canonical layout:
`json.dumps(manifest, indent=2, ensure_ascii=False)` plus a newline. Check a
change with:

```bash
python3 scripts/test-addon-manifests.py
```

It validates every manifest, including the personal-data rules (an add-on
that reads mail or messages may not send, run in cron or register on
`api-server`), and checks each one against `docker-compose.yml` and
`sync-profiles.sh`.

The bootstraps fence add-on MCP servers out of cron jobs with the same
`platform_toolsets.cron` list that keeps mail and messages out (see
[docs/email.md](../docs/email.md)). `permissions.cron` decides what stays in
cron: `true` keeps every entry, `mcp.instances` entries included; `"ro"`
keeps the `ro` service of a `{ro, rw}` service map and fences the `rw` one,
and keeps `mcp.instances` entries, which then carry the key of the profile
the job runs on; `false` or a missing field fences them all. Twenty sets
`true`, so a cron job on the default profile gets a write workspace's
read/write key. A manifest that fails validation is still fenced by its
`mcp.name` and by the name of each stored instance. If
`addon_manifest.py` can't be loaded, the list written on the last run is
kept. `sh scripts/test-addon-cron-fence.sh` runs the fence in the Hermes
image.
