# iMessage reader

Read-only access to your Messages.app history on macOS, for a local AI
assistant. A host-side indexer reads Messages' database and writes the chats
you choose to a small SQLite index with full-text search. A watcher keeps the
index current and raises an event when new messages arrive. An MCP server
(not built yet) will serve the index to the assistant. Nothing here can send,
react, mark read, edit or delete messages.

This folder is self-contained: Python 3.11+ standard library only, with no
imports from the rest of the repository, so it can move to its own repository.
The design, the projects it was compared with, and how the assistant stack
wires it in are in [docs/messages.md](../docs/messages.md).

| Part | Status |
|---|---|
| [Synthetic store](#synthetic-store) | Built. A fake Messages store for development and tests |
| [Indexer](#indexer) | Built. Tested only against the synthetic store |
| [Watcher and new-message events](#watcher) | Built. Tested only against the synthetic store |
| [macOS app](#macos-app) (its own Full Disk Access grant) | Built. Not yet run with a real grant |
| Read-only MCP | Not built |

```
imessage/
  messages-index.py              CLI: run, doctor, chats, watch, notify-test
  messages-index.toml.example    config template
  make-synthetic-messages.py     writes a fake store
  lib/messages_index.py          indexer
  lib/messages_watch.py          watcher and events
  lib/typedstream.py             reads and writes attributedBody archives
  lib/synthetic_messages.py      fake store generator
  macos/                         app launcher, build and install scripts
  tests/                         stdlib test scripts
```

## Indexer

```bash
cp imessage/messages-index.toml.example messages-index.toml   # fill in store, since, chats
python3 imessage/messages-index.py --config messages-index.toml chats    # chat ids, for the config
python3 imessage/messages-index.py --config messages-index.toml doctor   # counts only
python3 imessage/messages-index.py --config messages-index.toml run      # build or update the index
```

`--config` can also come from `MESSAGES_INDEX_CONFIG`. In the assistant
stack, `make messages-index`, `make messages-index-doctor` and
`make messages-index-chats` run the same commands.

### Config

| Key | Required | Meaning |
|---|---|---|
| `store` | yes | The folder holding `chat.db` (the Messages folder in your Library). No default |
| `index` | yes | The index file to write. Must be outside `store` |
| `since` | yes | Only messages on or after this date |
| `chats` | yes | Chat ids from `chats`, or patterns: `"*;-;*"` every 1:1 chat, `"iMessage;+;*"` every iMessage group, `"*"` everything |
| `exclude` | no | Chat ids or patterns to leave out even when `chats` matches them |
| `contacts` | no | The folder holding Contacts' `AddressBook-v22.abcddb` files, to put names on handles |
| `attachments` | no, `false` | List attachment names, types and sizes. Contents are never copied |
| `attachments_root` | no | Where attachment files live, if not `<store>/Attachments` |
| `max_body_chars` | no, `20000` | Longest body kept per message |

### What a run does

1. Opens `chat.db` read-only (`mode=ro`, `query_only`) and reads everything
   inside one read transaction, so it sees a consistent snapshot, including
   rows still in the WAL. It never writes or checkpoints. When Messages
   isn't running and there's no `-wal` or `-shm` file, it opens the
   database as immutable, so it doesn't create those files in the Messages
   folder.
2. Checks the columns it needs (`PRAGMA table_info`). It stops with exit
   code 3 if they're missing. Other columns are used when present.
3. Keeps chats that match `chats` and not `exclude`, and their messages on or
   after `since`. Messages in Recently Deleted are skipped. A message with no
   chat yet (Messages writes the join a moment later) waits for the next run.
4. Gets each body from `attributedBody` with its own typedstream decoder,
   falling back to the `text` column. Reactions go to their own table.
   Group changes become short event lines ("Jordan Lee added María Gómez").
   Unsent messages keep a row with no body. Edits set `edited` and an edit
   count.
5. Updates the index incrementally. Each row has a fingerprint of the
   columns that change (edits, unsends, read state, contact name), so later
   runs re-read only new or changed messages. Messages that left the scope
   or the store are removed.

`run` prints counts as JSON (`in_scope`, `added`, `updated`, `removed`,
`reactions`, `events`, `unsent`, `edited`, decoder counts), never content.
`doctor` prints layout facts and counts: missing optional columns, whether
dates are nanoseconds, how many messages are in scope, how many bodies
decoded, and how many contacts were found. `chats` prints chat ids,
names, participants and message counts. That is personal data, so run it
yourself.

Exit codes: 2 config, 3 unrecognised database layout, 4 macOS denied access.

### The index

One SQLite file, owner-only (`0600`, folder `0700`).

| Table | Holds |
|---|---|
| `chats` | Chat id, name, service, whether it's a group, participants with names |
| `messages` | One row per message or event: stable `id`, Messages' `guid` and `ROWID`, chat, date (Unix seconds), sender and name, kind (`message`, `event`, `unsent`), body, read state, edit, failure, thread and reply links, mentions, attachments |
| `reactions` | Each reaction or removal, with its target message, sender, kind and emoji |
| `messages_fts` | FTS5 over body, sender and chat names, with diacritics folded |
| `meta` | Schema version, scope fingerprint, last run |

If the schema version changes, delete the index and re-run.

### Permissions

Reading `chat.db` needs Full Disk Access for the program that runs the
indexer. Full Disk Access can't be narrowed: whatever holds it can read every
app's data. Run from a terminal, the grant belongs to the terminal and
covers everything you run there. For everyday use, install the
[app](#macos-app) and grant it instead.

## Watcher

```bash
python3 imessage/messages-index.py --config messages-index.toml watch         # runs until stopped
python3 imessage/messages-index.py --config messages-index.toml notify-test   # one test event
```

`watch` keeps the index current and, with a `[notify]` table, raises an
event when new messages arrive. It's what the app's LaunchAgent runs.

- **Noticing changes.** It watches `chat.db`, its `-wal` and `-shm` files and
  the Messages folder with kqueue, waits `debounce_ms` for a burst of writes
  to settle, and checks again every `poll_seconds` in case an event was
  missed. When Messages replaces a file (the WAL is deleted and recreated as
  it opens and closes), the watcher re-attaches. The design follows
  [openclaw/imsg](https://github.com/openclaw/imsg)'s watcher (MIT). On
  systems without kqueue it polls.
- **Updating the index.** Each wake runs an incremental pass: rows added
  after a `ROWID` cursor, plus messages from the last `window_hours`, where
  edits, unsends and late chat links happen. Rows that haven't changed cost
  a fingerprint check; message bodies are read only for new or changed
  rows. A full pass runs at start, when the scope changes, and every
  `full_every_minutes`. The cursor lives in a state file next to the index
  (`<index>.watch.json`, owner-only), so a restart doesn't repeat events.
- **Errors.** If macOS denies access (no Full Disk Access yet) or the
  database layout isn't recognised, the watcher logs it and tries again a
  minute later instead of exiting.

| `[watch]` key | Default | Meaning |
|---|---|---|
| `window_hours` | `2` | How far back each wake re-reads |
| `full_every_minutes` | `60` | How often a full pass runs |
| `poll_seconds` | `5` | Fallback check interval |
| `debounce_ms` | `250` | Wait for writes to settle |
| `state` | next to the index | The watcher's state file |

### Events

An event says that new messages arrived in a chat, never what they say:

```json
{"type": "messages.new", "id": "6f1c…", "created_at": "2026-10-13T16:20:05+00:00",
 "chat": {"id": "iMessage;-;+14155550101", "name": "Alex Chen", "is_group": false},
 "count": 2, "senders": ["Alex Chen"], "message_ids": [131, 132],
 "latest_at": "2026-10-13T16:20:03+00:00"}
```

`message_ids` are index ids, for looking the messages up through the MCP.
Only incoming messages (not yours, not reactions or group events) in
`notify.chats` count, and only if they're at most `max_age_minutes` old when
first seen, so a first run over your history or an iCloud backfill doesn't
raise a flood. The very first watcher run raises nothing. One wake produces
at most one event per chat.

| `[notify]` key | Required | Meaning |
|---|---|---|
| `chats` | yes | Which indexed chats raise events (ids or patterns) |
| `outbox` | one of these | A folder that gets one JSON file per event (`0600`, folder `0700`) |
| `url` | one of these | An endpoint that gets each event as a signed POST. Must be `https` unless it's on this machine |
| `secret_file` | with `url` | The shared HMAC secret, at least 16 bytes |
| `max_age_minutes` | no, `10` | Older messages don't raise events |
| `quiet_hours` | no | Local hours like `"22:00-07:00"`. Events are held and sent afterwards, merged into one per chat |

The POST is signed the way [Hermes](https://github.com/NousResearch/hermes-agent)
generic webhooks expect: `X-Webhook-Signature-V2` is the hex HMAC-SHA256 of
`"<X-Webhook-Timestamp>.<body>"`, and `X-Request-ID` carries the event id so
the receiver can drop duplicates. A failed POST is retried on each wake for
an hour, then dropped and counted. The outbox copy is written once.

Logs are one JSON line of counts per wake that changed something (`added`,
`updated`, `removed`, `events`, `delivered`, `held`, `pending`, `dropped`).

## macOS app

```bash
imessage/macos/build.sh                                         # into imessage/build (needs swiftc, Python 3.11+)
imessage/macos/install.sh --config "$PWD/messages-index.toml"   # app to ~/Applications, LaunchAgent runs `watch`
```

Then add the app in System Settings > Privacy & Security > Full Disk Access.
macOS doesn't ask for Full Disk Access the way it asks for other
permissions. Until you add the app, its log shows "permission denied" once a
minute.

The app, *Assistant Messages Reader*, is a compiled launcher
(`macos/Launcher.swift`) plus only the indexer and watcher scripts, sealed by
its code signature. It holds no send code and asks for no Apple events.
Because launchd starts the app, macOS attributes its file access to the app,
not to your terminal or a shared `python3`. A changed script breaks the
signature, so editing the repository can't change what a granted app does.
The launcher runs the scripts with `python -I -B` and a minimal environment.

`install.sh` copies the app to `~/Applications` and loads a LaunchAgent
(`~/Library/LaunchAgents/local.assistant.messages-reader.plist`) that keeps
`watch` running. `--interval 900` runs `run` every 15 minutes instead, with
no events. Logs (counts only) go to `~/Library/Logs/assistant-messages/`.
Add `--dry-run` to print the agent's plist without installing anything, and
`install.sh uninstall` to remove it.

| Variable | Default | Meaning |
|---|---|---|
| `MESSAGES_APP_PYTHON` | `python3` on `PATH` | Interpreter the launcher runs |
| `MESSAGES_APP_SIGN_IDENTITY` | `-` (ad hoc) | Signing identity. macOS ties the grant to the signature, so with ad hoc signing each rebuild needs Full Disk Access again. A stable identity (a free Apple Development certificate is enough) keeps it |
| `MESSAGES_APP_BUNDLE_PREFIX` | `local.assistant` | Bundle id and launchd label prefix. Set the same value for `install.sh` |
| `MESSAGES_APP_DIR` | `~/Applications` | Where `install.sh` puts the app |

**First real run.** After installing and granting the app, check its log
for a line of counts, and that `launchctl procinfo $(pgrep -f
assistant-messages-reader | head -1)` names the app as the responsible
process. Your terminal doesn't need Full Disk Access for any of this.

### First run on real messages

The indexer is tested only against the synthetic store, and Apple's format
is undocumented, so the first real run checks the assumptions:

1. Run `doctor`. `dates_look_like_nanoseconds` should be `true`,
   `attributed_body_failed` should be `0` or close to it, and
   `optional_columns_missing` shows what this macOS version lacks. Report
   counts, not messages.
2. Exit code 3 names the missing columns. Exit code 4 means macOS denied
   access.

## Synthetic store

`lib/synthetic_messages.py` writes a fake Messages store with 28 made-up
messages in five chats, plus a fake Contacts database:

```
<out>/Messages/chat.db                          WAL mode, subset of tables and columns
<out>/Messages/Attachments/<xx>/<nn>/<guid>/…   attachment files
<out>/AddressBook/Sources/<uuid>/AddressBook-v22.abcddb
<out>/manifest.json                             what was generated, for test assertions
```

It covers:

- bodies only in `attributedBody`, in both `text` and `attributedBody`, and
  only in `text` (old rows), with plain and mutable archive classes
- bodies long enough to need two-byte and four-byte lengths in the archive,
  non-BMP emoji, accents and Hebrew
- a 1:1 chat over a phone number, one over email, an SMS chat with a sender
  not in Contacts, a named group and an unnamed group
- reactions (a heart, an emoji reaction and a reaction taken back), a
  threaded reply, a mention, a link preview and a message sent with an
  effect
- an edited message with its history in `message_summary_info`, an unsent
  message, a message in Recently Deleted, and a failed send
- an image attachment, an audio message, and an attachment row whose file
  is gone
- group events: a rename, a member added and a member removed
- a message that tries prompt injection on the assistant
- Contacts entries with phone numbers in several formats, a nickname, and a
  contact who isn't in any chat

`attributedBody` blobs come from `lib/typedstream.py`, which writes
NSAttributedString archives that match Foundation's NSArchiver byte for byte.
The tests check that against archives Foundation produced, and on macOS with
`swiftc` they also decode every body with Foundation's NSUnarchiver.

Attachment `filename` values keep Messages' `~/Library/Messages/Attachments/…`
form. A reader has to map that prefix onto its configured attachment root and
must never expand `~`.

```bash
python3 imessage/make-synthetic-messages.py /tmp/synthetic-messages/store
```

The output path must contain `synthetic-messages`. The generator refuses
anything under `~/Library`. It only uses `example.com`, `example.org` and
`example.net` addresses, and phone numbers in the fictional 555-0100 to 0199
range. Output is reproducible, apart from SQLite's page layout.

For watcher tests, `append()` adds a message to an existing store the way
Messages would. `append(..., join=False)` followed by `add_join()`
reproduces the moment when a new row has no chat yet.

The schema is modelled on public descriptions and open-source readers, and
isn't authoritative. Some values are best guesses and are marked as such in
the code: the failed-send error code, the `message_summary_info` keys and the
Recently Deleted date. A reader that passes against this store still has to
probe the real schema at runtime.

## Tests

```bash
python3 imessage/tests/test-synthetic-messages.py   # generator and typedstream writer
python3 imessage/tests/test-messages-index.py       # indexer against the synthetic store
python3 imessage/tests/test-messages-watch.py       # watcher: events, quiet hours, retries, live kqueue loop
python3 imessage/tests/test-messages-app.py         # builds the app, checks its seal, runs it (macOS)
```

## Rules for contributors

- **Never use real messages or contacts as fixtures,** not even "anonymised".
  Add cases to `SPECS` in `lib/synthetic_messages.py` instead.
- **Never give a Messages, contacts or backup path a default** that resolves
  to a real store.
- **Learn the format from documentation and open-source code,** not by
  opening a real `chat.db`, schema included. If a bug only shows up on real
  data, the person who owns the messages extracts a minimal case and rewrites
  it as a synthetic one.
- **No send path.** Nothing in this folder sends, reacts, marks read, edits
  or deletes.
- **Keep coding agents out of real messages.** Block them from the Messages
  folder and related stores with their own permission rules, and don't grant
  Full Disk Access to the terminal they run in.
