# Email from Apple Mail (design)

Read-only email for Hermes, through Apple Mail instead of a login to a mail
server. A [host-side indexer](#indexer) builds a scoped index, and
the [`mail-mcp`](#mail-mcp) sidecar serves it to Hermes. Both have been tested
only against [the synthetic store](#synthetic-mail-store); see
[First run on real mail](#first-run-on-real-mail).

Requires the **`mail`** compose profile (`./scripts/setup.sh --mail`).

On 2026-10-13 we changed the plan (see [Decision](#decision-2026-10-13)).
The default source becomes Mail.app scripting, which needs only the
Automation permission for Mail. Reading Mail's files with Full Disk Access
becomes an opt-in fast mode. Sending goes through drafts that the user sends
from Mail.

## Quick path

Today the only indexer reads Mail's files and needs Full Disk Access. The
Mail.app scripting source is being built and will replace it as the default.

```bash
./scripts/setup.sh --mail          # profile, MAIL_MCP_ENABLED=1, mail-index.toml, data/mail (0700)
make mail-index-mailboxes          # account ids and mailbox names for mail-index.toml
make mail-index-doctor             # counts only; check before the first run
make mail-index                    # build or update the index (re-run to refresh)
make up
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose restart hermes
```

In WebUI: *Any unread mail from Priya this week?*

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

## Decision (2026-10-13)

The original design below (option B) needs Full Disk Access. We've kept it
as an opt-in fast mode and made Mail.app scripting the default, for these
reasons:

- **Full Disk Access can't be scoped.** Granting it to read Mail also exposes
  Messages, browser history and every other app's data. On 2026-10-02 Apple
  said it will make the permission harder to grant, citing AI agents, with no
  date given ([MacRumors](https://macrumors.com/2026/10/02/apple-announces-macos-full-disk-access-changes)).
  That's a hard prompt to put in front of people installing the stack.
- **Reading isn't where the risk is.** An agent that reads an inbox already
  holds private data and untrusted content. Prompt injection does damage
  when it can also send ([the lethal trifecta](https://simonwillison.net/2025/jun/16/the-lethal-trifecta/)).
  How sending is gated matters more than whether reading uses IMAP or files.
- **Scripting Mail.app needs no credentials.** It works for every account
  configured in Mail, and the permission covers Mail only. It isn't
  read-only: whatever holds it can also send and delete. So only the helper
  holds it, never the agent.

The resulting design:

| Part | Default | Opt-in |
|---|---|---|
| Read source | Mail.app via JXA (Automation → Mail) feeding the scoped index | Option B indexer with Full Disk Access, as its own signed executable |
| What Hermes sees | The same index through the read-only mail MCP | Same |
| Sending | A separate helper creates a draft or opens a Mail compose window; the user clicks Send | Autonomous sending only from an agent-owned address, with a recipient allowlist and rate limit |

- **The index makes scripting fast enough.** Live AppleScript queries are slow
  on large mailboxes. Benchmarks on a 73K-message store measured 50-message
  batch fetches at about 1.2 s, and `whose body contains` timed out
  ([imdinu benchmarks](https://imdinu.github.io/apple-mail-mcp/benchmarks/)).
  A background indexer working through a date window can live with that.
  Interactive search runs against our FTS index, never against Mail.
- **Drafts are the approval screen.** The user reviews the real message in
  Mail's compose window (recipients, body, attachments), not a summary the
  model wrote. Drafts saved to the account sync, so they can be sent from
  a phone too.
- **Keep reading and sending apart.** The send helper has no read tools and
  no Full Disk Access. The read helper and the mail MCP have no send tools.
  The Messages tooling follows the same split.
- **Real runs stay with the mailbox owner.** Driving Mail.app is live access
  to real mail, so a coding agent can't test it. The helper ships with a
  `doctor` mode that prints counts and timings only, for the owner to run.

### Prior art

We surveyed Apple Mail MCP servers on 2026-10-13. Stats are from the GitHub
and package-registry APIs on that day.

| Project | Stars | Commits since Jul 13 | Contributors | License | Reads via | Full Disk Access | Send |
|---|---|---|---|---|---|---|---|
| [sweetrb/apple-mail-mcp](https://github.com/sweetrb/apple-mail-mcp) (npm `apple-mail-mcp`, ~21K downloads/month) | 91 | 100+ | 14 | MIT | AppleScript; optional IMAP (`EXAMINE`) | Contacts search only | Direct send by default; drafts optional |
| [patrickfreyer/apple-mail-mcp](https://github.com/patrickfreyer/apple-mail-mcp) (PyPI `mcp-apple-mail`) | 227 | 4 | 12 | MIT | AppleScript | No | Direct send; `--read-only` leaves drafts |
| [imdinu/apple-mail-mcp](https://github.com/imdinu/apple-mail-mcp) (PyPI `apple-mail-mcp`, ~1.3K/month) | 71 | 12 | 5 | GPL-3.0 | Envelope Index + `.emlx` + FTS5; JXA fallback | Yes, for the index | None (read-only) |
| [parasxos/apple-mail-mcp](https://github.com/parasxos/apple-mail-mcp) (PyPI `apple-mailbox-mcp`) | 32 | 100+ | 1 | MIT | Mail's SQLite + body FTS | Yes | SMTP or SSH sendmail; read-only env flag |
| [s-morgan-jeffries/apple-mail-fast-mcp](https://github.com/s-morgan-jeffries/apple-mail-fast-mcp) | 105 | 19 | 8 | MIT | AppleScript; optional IMAP via app password | No | Drafts; `send_now` behind an MCP elicitation prompt |
| [PsychQuant/che-apple-mail-mcp](https://github.com/PsychQuant/che-apple-mail-mcp) | 10 | 100+ | 1 | MIT | SQLite with AppleScript fallback | For the fast path | Direct send; drafts |
| [like-a-freedom/rusty_apple_mail_mcp](https://github.com/like-a-freedom/rusty_apple_mail_mcp) | 9 | 32 | 2 | none | Envelope Index + `.emlx` | Yes | None (read-only) |

[supermemoryai/apple-mcp](https://github.com/supermemoryai/apple-mcp) (3.1K
stars) included Mail but has been archived since 2025-08.

What we found:

- **Nobody ships our combination.** The fast servers all need Full Disk
  Access. The servers that avoid it query Mail live and time out on big
  mailboxes. None builds a scoped index from scripting, and none defaults to
  drafts with no send path.
- **Scoping is thin.** Most expose every account. Defaults like
  `APPLE_MAIL_DEFAULT_ACCOUNT` set a preference, not a boundary. None has
  required account, mailbox and date-window scope.
- **None treats message text as untrusted** beyond escaping AppleScript
  input. Our quoted-data marking has no precedent to borrow.
- **Sustainability is uneven.** The two most active projects with
  Full Disk Access paths (parasxos, PsychQuant) each have a single maintainer.
  imdinu is GPL-3.0, which constrains embedding it in this MIT-licensed stack.

We won't adopt any of them as a dependency: each would mean
turning off most of its tools and adding the scoping and send split we need.
Ideas worth borrowing:

- `EXAMINE` plus `BODY.PEEK[]` for any read-only IMAP path, so nothing is
  marked read (sweetrb).
- JXA batch property fetches (`messages.subject()` and similar over a range)
  instead of per-message calls or `whose` clauses (imdinu 0.3).
- Saving drafts by writing an `.eml` and opening it in Mail, since HTML in
  AppleScript `content` often arrives as literal markup (patrickfreyer).
- A `doctor` command that names each failing permission with its fix
  (parasxos, sweetrb), and MCP `readOnlyHint` and `destructiveHint`
  annotations on every tool (s-morgan-jeffries).
- Pacing attachment calls, because Mail drops attachments under fast IPC
  (PsychQuant).

## Original design (now the opt-in fast mode)

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

Option **C** is now the default read source (see
[Decision](#decision-2026-10-13)). Actions such as mark read or move would be
a separate, opt-in tool.

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

## mail-mcp

`compose/mail-mcp` is a small Python server (`mcp==1.30.0`, streamable HTTP
at `http://mail-mcp:8000/mcp`, compose-internal only). The query logic in
`mail_query.py` uses only the standard library.

| Tool | Returns |
|---|---|
| `mail_status` | Message count, date range, last index run, whether bodies are searchable, each account/mailbox with total and unread counts |
| `mail_search` | Up to 50 summaries, newest first: id, date, sender, subject, mailbox, read/flagged, attachment count, and a snippet when the query matched. Filters: `query` (every word must match, as a prefix), `sender`, `mailbox`, `since`/`until` (inclusive dates), `unread_only`, `flagged_only`, `limit`, `offset` |
| `mail_get` | One message by id: recipients, Message-ID, thread id, attachment names, and the body (up to `max_chars`, default 20,000) |
| `mail_thread` | Every indexed message in the same conversation, oldest first |

There are no tools that send, move, delete or flag mail, and the index is
opened read-only on a read-only mount.

**Untrusted content.** Every result carries a `notice` telling the model that
subjects, names, snippets and bodies are third-party text to report on, never
instructions. Bodies come between `<<<EMAIL BODY (untrusted)>>>` and
`<<<END EMAIL BODY>>>` markers, and any copy of a marker inside a message is
removed first, so a message can't fake the end of its body. Free-text queries
are reduced to quoted words, so a query can't inject FTS syntax.

**Which profiles get it.** The bootstrap registers `mcp_servers.mail` with a
four-tool allowlist on the default (dashboard, CLI) and `browser` (WebUI)
profiles. It never registers it on `api-server`. Unattended sessions there
run without anyone watching, and mail is the easiest place for a stranger to
plant instructions. Because profiles are cloned from default, the bootstrap
removes any entry pointing at `mail-mcp` from `api-server`, and from every
profile when `MAIL_MCP_ENABLED` isn't `1`.

**Container.** It mounts only `MAIL_INDEX_DIR` (default `./data/mail`),
read-only. The root filesystem is read-only too, all capabilities are
dropped, and `no-new-privileges` is set. It runs as `HERMES_UID:HERMES_GID`.
On Linux that uid must own the `0700` index folder. Docker Desktop for Mac
doesn't enforce ownership across the mount. The indexer's `index` setting
must point inside `MAIL_INDEX_DIR`, at `MAIL_INDEX_FILE` (default
`index.sqlite`). The example config already does.

**Freshness.** The MCP serves whatever the last `make mail-index` wrote.
Re-run it (or schedule it) to pick up new mail. `mail_status` reports the last
run.

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
