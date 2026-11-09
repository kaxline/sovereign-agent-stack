# Built-in add-ons

Each directory holds the `addon.json` manifest of one optional service whose
containers are already in `docker-compose.yml`. The directory is named after
the last segment of the manifest's `id`. The format is experimental while the
stack is in 0.3.x, and field names can change between patch releases, with a
changelog entry. GPT Researcher (`gptr/`) and Actual Budget (`actual/`) are the
add-ons so far; the other optional services are still wired by hand.

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
  `unmanaged` (built-in add-ons only) is a list of Hermes profile names, for
  example `["default"]`, that bootstrap leaves alone for that entry: it
  registers, drops, pins and sets parked retry on none of them, so a server
  the user added by hand on that profile at the same URL stays. The names
  must be distinct and may not appear in `profiles`. An empty list behaves as
  absent.
- `services.<name>.when`: `{"gate": FLAG}`, so the service starts only while
  that gate flag is on. `mounts` maps `secrets`, `config` and `instances` to
  `ro` or `rw`; they mount the directories `data/addons/<short-id>/secrets`,
  `config` (which holds `settings.env`) and `instances` at
  `/run/addon/secrets`, `/run/addon/config` and `/run/addon/instances`. The
  compose service must declare exactly those mounts, with sources written as
  `${ASSISTANT_DATA_ROOT:-./data}/addons/<short-id>/<name>`;
  `scripts/test-addon-manifests.py` checks this, `validate` does not. A
  service that mounts `secrets` may not be an `mcp` service.
  `volumes` lists the compose volume names that `addon.sh disable --purge`
  removes.
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
  `instances:<name>` in `missing` until there is one.
- `commands`: `{<name>: {service, instance, argv}}`. `addon.sh run` runs
  `argv` in the named service, and appends `--account <slug>` when `instance`
  is true. An add-on that reads personal data may not run a command in an MCP
  service.

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

The bootstraps keep an add-on's MCP server out of cron jobs unless its
manifest sets `permissions.cron` to `true`, using the same
`platform_toolsets.cron` list that keeps mail and messages out (see
[docs/email.md](../docs/email.md)). A missing field counts as `false`. A
manifest that fails validation is still fenced by its `mcp.name`. If
`addon_manifest.py` can't be loaded, the list written on the last run is
kept. `sh scripts/test-addon-cron-fence.sh` runs the fence in the Hermes
image.
