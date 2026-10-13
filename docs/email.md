# Email from Apple Mail (design)

**Status: design only.** Nothing in the stack reads mail yet. This page
records how Hermes should get read access to email by reading Apple Mail's
local store instead of logging in to a mail server, and the fake store the
reader will be developed against. Only
[the synthetic store](#synthetic-mail-store) exists so far.

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
