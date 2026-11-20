# Moving chat history

`scripts/sessions.sh` copies Hermes sessions from one install to another: to a
new Mac, from a local stack to a managed one, or onto a remote stack host. It
adds to the target's history; it doesn't replace it, as copying `state.db`
files would.

```bash
scripts/sessions.sh export --dest ~/hermes-history
scripts/sessions.sh import --from ~/hermes-history
```

Both run against the running `hermes` service as the `hermes` user, and the
gateway keeps running. `--profile NAME` (repeatable) limits either command to
some profiles. `--json` prints [JSON lines](extending.md#json-lines) holding
counts and session ids, never titles or message text; the full shapes are in
the script's header (`scripts/sessions.sh --help`).

## Export

`export` writes one `<profile>.jsonl` per profile, `default` for the store at
the Hermes home, plus `manifest.json` with each file's session and message
counts and session ids (`session_ids`), the stack version and the Hermes
image. The destination must be empty
or new. Each line is one session from `SessionDB.export_all(include_inactive=True)`,
the shape `hermes sessions export --format jsonl` writes: every stored message
row with its `active` and `compacted` flags, so turns that compaction archived
come back archived, not as live context. Nothing is redacted. The files hold
whole transcripts and are written mode 0600 in a 0700 directory; treat them
like a backup.

The stack's own profiles (`api-server`, `browser`) are exported like any
other.

## Import

`import` loads each file into the profile of the same name through
`SessionDB.import_sessions`, with session ids unchanged, so Boundary's state
keyed by session id still applies. Per profile it reports:

- `imported`: new rows.
- `skipped`: ids the target already has. A second import of the same export
  imports nothing.
- `refused`: ids deleted on the target. Hermes fences a deleted id for good.
- `too_large`: sessions over Hermes' per-session import limits, 10,000 message
  rows or 5 MB. They can't be imported at all.
- `detached`: children whose parent is in neither the target nor the export;
  they're imported as top-level sessions.
- `renamed`: imported sessions whose title the target, or an earlier session
  in the same import, already held. Titles are unique in a Hermes store, so
  the import adds ` (<last 12 characters of the id>)`, as Hermes' own import
  paths do, cut to fit the 100-character title limit. `renamed_ids` lists them.
- `invalid`: lines that aren't a session, or that Hermes rejects.
- `failed`: sessions that failed for another reason, such as a write lock
  held past Hermes' retry. A batch that raises is retried one session at a
  time, so only the sessions that fail again are counted. The run exits 1
  when any failed; run it again to retry them.

Each count except `detached` and `renamed` comes with an id list
(`imported_ids`, `skipped_ids`, `refused_ids`, `too_large_ids`,
`invalid_ids`, `failed_ids`), and every session id the import read is in
exactly one of them, so together they account for every id in the
profile's manifest entry. A line with no id counts as `invalid` without an
entry in `invalid_ids`, and a second line with an id already read is listed
in `invalid_ids` as well as wherever the first one went.

A profile the target doesn't have is skipped and reported with
`"status":"profile_missing"` and its session count. Its sessions never go into
another profile. Create the agent on the target (`scripts/agent-create.sh`)
and import again.

One `import_sessions` call writes nothing when any entry in it is invalid or
deleted, so the script checks ids first and imports the rest in batches under
Hermes' caps (500 sessions, 50,000 messages) and at most 8 MB each, parents
before children.

## While the gateway runs

Imports run beside a live gateway. Each batch is one `BEGIN IMMEDIATE`
transaction through Hermes' `_execute_write`, which retries on a held lock;
the gateway waits for at most one batch. `session.list` reads the database on
every call, so imported sessions show up without a restart. Checked on
v0.23.1 against a running `gateway run` on a synthetic home.

## Not carried

Files that messages point at, such as cached images and audio under the Hermes
home's `cache/` and spilled-over tool results, stay behind. A moved session
shows a broken reference where one of those was. Boundary's own per-session
state (project filing, channels) lives in Boundary and moves with Boundary's
export.

`scripts/test-sessions-sh.sh` runs an export and import between two synthetic
homes in the pinned image.
