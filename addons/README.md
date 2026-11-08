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
