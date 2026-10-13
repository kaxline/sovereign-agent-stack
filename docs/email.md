# Email from Apple Mail (design)

**Status: indexer only.** This page records how Hermes should get read access
to email by reading Apple Mail's local store instead of logging in to a mail
server. [The indexer](#indexer) and [the synthetic store](#synthetic-mail-store)
exist. The MCP that serves the index to Hermes does not yet, so Hermes can't
read mail.

## Goals

- Hermes can search and read the mail you choose (accounts, mailboxes, a date
  window) without IMAP credentials or another copy of your mailbox online.
- Nothing in the stack can read mail you didn't scope in, and no container
  mounts the Mail folder.
- Contributors, and any coding agent they use, never need real mail to build
  or test it.

## Where Mail keeps messages

Mail.app keeps one folder per account under `~/Library/Mail/V10/` (the
version folder changes with major macOS releases). Each mailbox is a
`<name>.mbox` folder, and nested mailboxes nest those folders. Messages sit in
`<store-uuid>/Data/…/Messages/<id>.emlx`. Each `.emlx` file holds:

1. a line with the byte length of the message,
2. the raw RFC 5322 message,
3. an XML property list with Mail's flags and dates.

When Mail hasn't downloaded an attachment it writes `<id>.partial.emlx`. That
file keeps the attachment's MIME headers with an empty body, and the
attachment itself goes in `Data/…/Attachments/<id>/<part>/`. Metadata for
every message (mailbox, sender, recipients, subject, dates, flags, thread)
lives in a SQLite database, `V10/MailData/Envelope Index`. The `<id>` is the
`ROWID` of the message in that database.

Apple doesn't document any of this, and the database schema changes between
macOS releases. Everything above comes from public write-ups and open-source
readers. macOS only lets processes with **Full Disk Access** read the folder.

## Options

| Option | How | For | Against |
|---|---|---|---|
| A. Parse `.emlx` files | Walk the mailbox folders and parse each file | Complete; works with Mail closed; stable file format | A full scan is slow; no search without building an index |
| B. Envelope Index + `.emlx` | Query a read-only snapshot of the database for metadata, and open `.emlx` only for bodies | Fast filtering and incremental sync by `ROWID` | Undocumented schema that shifts between releases; Mail writes to it in WAL mode |
| C. Script Mail.app | AppleScript or JXA through `osascript` | Supported interface; matches what Mail shows | Slow on large mailboxes; Mail must be running; needs an Automation prompt |
| D. Spotlight | `mdfind` over Mail's indexed messages | Full-text search for free | Coarse; depends on indexing state; hard to scope to accounts |
| E. Mail rule hand-off | A Mail rule runs a script that copies matching new mail to a folder | Only mail you route leaves Mail | New mail only; per-machine setup |
| F. Skip Mail.app | Sync over IMAP into a Maildir (`mbsync`) and index it (`notmuch`) | Documented formats; works off macOS | Not "local Mail"; needs credentials and another copy of the mailbox |

## Recommended design

Option **B**, run by a small host-side helper:

```
Mail store ──(Full Disk Access, read-only)──▶ mail-indexer (host)
                                                   │ scoped by config
                                                   ▼
                                    data/mail/ index (SQLite + FTS)
                                                   │
                                     mail MCP (read-only, narrow tools)
                                                   ▼
                                                Hermes
```

- **One narrow program gets Full Disk Access.** That program is the indexer,
  not a terminal, `python3`, `node` or the container runtime. It opens the
  Envelope Index read-only from a snapshot copy (database plus `-wal`). It
  works out the schema with `PRAGMA table_info` and refuses to run on a
  layout it doesn't recognise. It finds message files by globbing, because
  folder layouts shift between releases.
- **Scope is explicit, with no defaults.** The store path, the accounts, the
  mailboxes and the date window all come from configuration, and nothing is
  indexed until they're set. Bodies and attachments are separate opt-ins.
  Junk and Trash are excluded unless listed.
- **Hermes sees the index, never the store.** The index lives in the
  gitignored `data/` tree (see [User data directory](data-dir.md)) and is
  served through a read-only MCP with narrow tools: search, get one message,
  list a thread. Compose files never mount the Mail folder.
- **Mail content is untrusted input.** Messages can carry instructions aimed
  at the agent. Tool results mark message text as quoted data, and the MCP
  has no send, move or delete tools.
- **Incremental.** The indexer tracks the highest `ROWID` per mailbox and
  each message's flags, so later runs pick up only new or changed messages.

Option **C** stays in reserve for actions (mark read, move), which need
Mail's cooperation and would be a separate, opt-in tool.

## Indexer

`scripts/mail-index.py` (library: `scripts/lib/mail_index.py`) runs on the
host. Python 3.11 or newer, standard library only.

```bash
cp mail-index.toml.example mail-index.toml   # gitignored; fill in store, since, accounts
make mail-index-mailboxes                     # account ids and mailbox names, to write the config
make mail-index-doctor                        # checks the store and scope; counts only
make mail-index                               # build or update the index
```

Use `MAIL_INDEX_CONFIG=path` with make, or `--config path` with the script,
to point at a different config.

### Config

| Key | Required | Meaning |
|---|---|---|
| `store` | yes | The folder holding Mail's `V<n>/` directory. No default |
| `index` | yes | The index file to write. Must be outside the store. Keep it in the data tree |
| `since` | yes | Only messages received on or after this date |
| `[[accounts]]` | yes, at least one | `id` (account folder UUID) and `mailboxes` |
| `bodies` | no, `false` | Store message bodies (HTML converted to text) for search |
| `attachments` | no, `false` | Store attachment names, types and sizes. Contents are never copied |
| `max_body_chars` | no, `100000` | Longest body kept per message |

`mailboxes` entries are exact names (`INBOX`, `Projects/Alpha`) or glob
patterns (`*`, `Clients/*`). Patterns skip Junk and Trash folders (Junk,
Spam, Trash, Deleted Messages, Deleted Items, Bin); name one to include it.
A pattern matches a mailbox, not its children, so `Projects` doesn't include
`Projects/Alpha`.

### What a run does

1. Copies the metadata database and its `-wal` / `-shm` files to a private
   temporary folder, so Mail's live file is never opened, and runs SQLite's
   quick integrity check on the copy.
2. Probes the copy's columns. It stops with exit code 3, without touching the
   index, if the `messages` and `mailboxes` columns it needs are missing.
   Other columns (subjects, senders, flags, conversation ids) are used when
   present.
3. Keeps rows in scope: listed accounts and mailboxes, received on or after
   `since`, not deleted.
4. Reads headers from each new message's file (Message-ID, threading,
   recipients, decoded subject and sender), and the body when `bodies` is on.
   It searches only the mailbox's own folder, not child mailboxes. If a file
   is missing or unreadable, the message is still indexed from the database.
5. Updates the index incrementally. New messages are added. Messages that
   left scope or the store are removed. A flag change updates the row without
   re-reading the file. Turning `bodies` on or off re-reads every file once.

`run` prints counts as JSON (`in_scope`, `added`, `updated`, `removed`,
`missing_files`, `unreadable`, `accounts_missing`) and never message content.
`doctor` prints the version folder, which optional columns exist, whether
dates look like Unix seconds, and how many messages and files are in scope.

### The index

One SQLite file, owner-only (`0600`, folder `0700`). Table `messages` is keyed
by the store's message `ROWID` and holds the account, mailbox, Message-ID,
thread id, dates, sender, recipients (JSON), subject, flags, the optional
body and attachment list. `messages_fts` is an FTS5 table over subject,
sender, recipients and body, with diacritics folded. `meta` records the
schema version, a fingerprint of the scope and the last run time. If the
schema version changes, delete the index and re-run.

### First run on real mail

The indexer is tested only against the synthetic store, and Apple's format is
undocumented, so the first real run checks the assumptions:

1. Run `make mail-index-doctor`. `dates_look_like_unix_seconds` should be
   `true` and `message_files_found` should equal `messages_in_scope`. If not,
   the store's layout differs from the synthetic one. Report the counts, not
   messages.
2. Exit code 3 means the metadata layout wasn't recognised, and the error
   names the missing columns. Exit code 4 means macOS denied access.

macOS grants Full Disk Access per program. Running the indexer with the shared
`python3` means granting that interpreter access, and with it every Python
script on the machine. Packaging the indexer as its own executable is a
follow-up.

## Synthetic mail store

`scripts/lib/synthetic_mail.py` writes a fake Mail store with 13 made-up
messages across two accounts. It covers:

- plain-text, HTML-only and multipart messages
- an inline attachment, and a partial message with its attachment stored
  beside it
- a three-message thread
- non-ASCII names, subjects and bodies
- Junk, Deleted Messages and Sent Messages mailboxes, and a nested mailbox
- message ids above and below 1000, which use different `Data/` folder depths
- a message that tries prompt injection

It also writes a matching `Envelope Index` (a documented subset of tables and
columns) and a `manifest.json` that lists what was generated, for test
assertions.

```bash
python3 scripts/make-synthetic-mail.py /tmp/synthetic-mail/store
make synthetic-mail-test
```

The output path must contain `synthetic-mail`. The generator refuses
anything under `~/Library` and uses only `example.com`, `example.org` and
`example.net` addresses. Output is byte-for-byte reproducible, except for the
SQLite file's page layout.

The fake schema is modelled on public descriptions and isn't authoritative. A
reader that passes against it still has to probe the real schema at runtime,
as described above.

## Rules for contributors

- **Never use real mail as a fixture,** not even "anonymised". Add cases to
  `SPECS` in `scripts/lib/synthetic_mail.py` instead.
- **Never give a mail path a default** that resolves to a real store, and
  never mount the Mail folder in a committed compose file.
- **Learn the format from documentation and open-source code,** not by
  inspecting a mailbox. If a bug only shows up on real data, the person who
  owns that mailbox extracts a minimal case and rewrites it as a synthetic
  spec.
- **Keep coding agents out of real mail.** If you use one, block it from the
  Mail folder and webmail with its own permission rules, and don't grant Full
  Disk Access to the terminal it runs in.
