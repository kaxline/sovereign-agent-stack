# Email from Apple Mail (design)

Read-only email for Hermes, through Apple Mail instead of a login to a mail
server. A [host-side indexer](#indexer) builds a scoped index, and
the [`mail-mcp`](#mail-mcp) sidecar serves it to Hermes. By default the
indexer asks Mail.app through scripting, which needs only the Automation
permission for Mail. Reading Mail's files directly is an opt-in fast mode
that needs Full Disk Access (see [Decision](#decision-2026-10-13)).

Everything has been tested only against synthetic mail: a fake Mail.app for
the scripting source and [a synthetic store](#synthetic-mail-store) for the
file source. See [First run on real mail](#first-run-on-real-mail).
Hermes can't send email. With the separate [drafts](#drafts) opt-in it can
write drafts, which a host helper saves to Mail's Drafts for you to send.

Accounts that Mail.app doesn't have, or machines without it, can use the
optional [IMAP source](#imap-source) instead. It sits behind the same scope,
index and read-only MCP, and keeps the password away from the agent. See
[Providers](#providers) for how sources plug in.

Reading requires the **`mail`** compose profile (`./scripts/setup.sh --mail`).
Drafts require **`mail-drafts`** (`./scripts/setup.sh --mail-drafts`).

## Quick path

The first `make mail-index-mailboxes` makes macOS ask whether your terminal
may control Mail. Mail opens if it isn't running.

```bash
./scripts/setup.sh --mail          # profile, MAIL_MCP_ENABLED=1, mail-index.toml, data/mail (0700)
make mail-index-mailboxes          # account ids and mailbox names for mail-index.toml
make mail-index-doctor             # counts only; check before the first run
make mail-index                    # build or update the index (re-run to refresh)
make up
make hermes-reload
```

In WebUI: *Any unread mail from Priya this week?*

## Goals

- Hermes can search and read the mail you choose (accounts, mailboxes, a date
  window) without IMAP credentials or another copy of your mailbox online.
  Where IMAP is the only way in, the agent still never holds the password.
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
| Read source | Mail.app via JXA (Automation → Mail) feeding the scoped index (`source = "script"`, built) | Option B with Full Disk Access (`source = "store"`, built), granted to the reader app only |
| What Hermes sees | The same index through the read-only mail MCP | Same |
| Sending ([drafts](#drafts), built) | A separate helper creates a draft or opens a Mail compose window; the user clicks Send | Autonomous sending only from an agent-owned address, with a recipient allowlist and rate limit |

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

## Providers

Decided 2026-10-15. Mail can come from more than one kind of account: Mail.app,
an IMAP server, Proton through Bridge, and later Gmail or Microsoft 365. Each
kind is an adapter behind one core, so the agent sees one set of mail tools
and every account gets the same limits:

```
read adapters ──▶ indexer core ──▶ index ──▶ mail MCP ──▶ Hermes
(script, store,     scope, ids,
 imap, …)           threads, scrubbing

outbox adapters ◀── drafts helper core ◀── queue ◀── drafts MCP ◀── Hermes
(Mail.app, imap)    checks, rate limit,
                    routing by account
```

**The core holds the rules; adapters only talk to a provider.** The scope
filter (accounts, mailboxes, date window, Junk and Trash), index ids,
threading, the overwriting of deleted text, the untrusted-content markers, the
draft checks, the rate limit and the profile and cron fences are written once.
An adapter that forgets one can't weaken it.

We chose this over one MCP server per provider because:

- the agent gets one search across accounts and one way to reply, instead of
  a different tool set per account;
- the rules can't drift apart between providers, and the fences only need to
  know about `mail` and `drafts`;
- moving an account to another provider changes no tools, prompts or cron
  jobs;
- one synthetic test suite checks every adapter the same way.

### Read adapters

An adapter implements four calls. The `script` source
(`scripts/lib/mail_script.py`) is the reference.

| Call | Returns |
|---|---|
| `rows()` | A summary of every message in the in-scope mailboxes: source id, account, mailbox, received and sent dates, subject, sender, read and flagged. Also `accounts_missing` and `mailboxes_missing` |
| `fetch(rows)` | For each row, the parsed headers (plus body and attachment list when turned on), or `missing` or `unreadable` |
| `doctor()` | Counts, timings and booleans only |
| `mailboxes()` | Account ids and mailbox paths, for writing the config |

An adapter never changes the account. Each one has a test that fails if its
code gains a call that writes, moves, flags or deletes (as `mail_script.js`
already does). Mailbox paths always use `/`, whatever separator the provider
uses, so the same patterns work everywhere.

| | `script` | `store` | `imap` | `gmail` (planned) | `graph` (planned) |
|---|---|---|---|---|---|
| Reaches | Accounts in Mail.app | Accounts in Mail.app | One IMAP account, Proton through Bridge | One Google account | One Microsoft 365 or Outlook.com account |
| Read-only by | Our script only reads | Read-only snapshot | `EXAMINE` and `BODY.PEEK[]` | `gmail.readonly` scope | `Mail.Read` scope |
| Credential | Automation → Mail | Full Disk Access | Password or app password, in the Keychain | OAuth token | OAuth token |
| Junk and Trash found by | Name | Name | `SPECIAL-USE` flags, then name | System labels | Well-known folders |
| Mailboxes | Folders | Folders | Folders | Labels (one message, many labels) | Folders |
| Source id | Mail's message id | Mail's message id | `UIDVALIDITY.UID` | Message id | Immutable id |

For IMAP and the planned providers, credentials are the hard part. An IMAP
password, or a Gmail or Microsoft app password, opens the whole account: it
can read, delete and send. Fastmail's app passwords bundle IMAP and SMTP too.
So the read-only limit comes from our code, not the credential, and the
credential stays where the agent can't reach it ([below](#imap-source)).
Gmail and Microsoft Graph do offer narrow OAuth scopes, which is the main
reason to build those adapters instead of using IMAP for them.

### Index ids

The index assigns its own message ids (schema 2). Before, it used Mail's
message ids, which only mean something inside one Mail store.

- `id` counts up as messages are indexed and is never reused, so
  `newest_id` and `after_id` mean "indexed after" across every account.
  Schedulers keep working unchanged.
- Each row also keeps the adapter's `source_id`. `(account, mailbox,
  source_id)` is unique.
- Upgrading a schema 1 index keeps every message's id (its old Mail id) and
  carries on counting from the highest one, so nothing is fetched again and
  a cron job's last `after_id` stays valid.
- Search and read results carry a `reply_ref` naming the account, mailbox
  and source id. The drafts MCP takes it as `reply_to`, so the drafts helper
  can find the original without reading the index. The old
  `{id, account, mailbox}` form still works for Mail.app accounts.

### Outbox adapters

The drafts helper picks an outbox by account:

| Outbox | Saves drafts by | Replies |
|---|---|---|
| Mail.app (default) | Mail's scripting, as today | Mail's reply command; the helper reads nothing |
| `imap` | `APPEND` to the account's Drafts folder, flagged `\Draft` | Reads the original's Message-ID, References, From, Reply-To and Subject headers, never its body, to address and thread the reply |

A draft saved over IMAP shows up in every client signed in to that account,
including phones, and you send it from there. Nothing in the stack sends. An
autonomous sender stays a separate, later opt-in: SMTP from an address the
agent owns, with a recipient allowlist and a rate limit, using its own
credential.

### Planned: Gmail and Microsoft 365

Not built. Notes for whoever builds them:

- **Gmail.** `gmail.readonly` for reading, with the History API for changes.
  It's a restricted scope: an app we publish needs Google's security review,
  and a personal Google Cloud project left in testing mode makes sign-ins
  expire every 7 days. For drafts, `gmail.compose` can also send drafts, so
  drafts need the same care as sending. A message can carry several labels;
  the index stores it once per selected label, as it would a copy in two
  folders.
- **Microsoft Graph.** `Mail.Read` for reading, with delta queries for
  changes. Saving drafts needs `Mail.ReadWrite`, which can also delete, so
  the drafts outbox would hold that token on its own, apart from the reader.
- Both use an OAuth sign-in in a browser, run by the helper app, with the
  refresh token in the Keychain.

## IMAP source

Optional. For accounts that Mail.app doesn't have, machines without Mail.app,
or Proton through Bridge.

```toml
[[accounts]]
id = "work"                 # your name for the account; appears in results
source = "imap"
host = "imap.example.com"
port = 993                  # 993 with TLS (default), 143 with STARTTLS
security = "tls"            # "tls" (default) or "starttls"
user = "you@example.com"
address = "you@example.com" # From address for drafts; more under addresses = [...]
mailboxes = ["INBOX", "Projects/**"]
# password_env = "WORK_IMAP_PASSWORD"   # instead of the Keychain; terminal runs only
# drafts_mailbox = "Drafts"             # if the server doesn't mark its Drafts folder
# tls_ca = "~/certs/bridge.pem"         # a server that signs its own certificate
```

`source` defaults to the top-level `source` (itself `"script"` by default),
so existing configs keep working and one config can mix Mail.app and IMAP
accounts. `make mail-index-mailboxes` lists each IMAP account's mailboxes
with message counts and their special-use flags. `security = "plain"` (no
TLS) is accepted only for a server on this machine, such as a test server.
There's no setting for the password itself.

**What a run does.** For each in-scope mailbox, the indexer opens it with
`EXAMINE`, which the server treats as read-only for that session, and asks
for the messages received since `since`. It fetches flags, dates, sizes and
summary headers for all of them, and the full message only for new ones,
with `BODY.PEEK[]` so nothing is marked read. Messages flagged `\Deleted` are
skipped. If a mailbox's `UIDVALIDITY` changes, its messages are re-indexed
under new ids. Junk and Trash are found by the server's `SPECIAL-USE` flags
and then by name, and mailbox names are decoded from IMAP's modified UTF-7.

**Password.** The password lives in the Keychain, as a generic password with
service `local.assistant.mail-imap` (the bundle prefix, then `.mail-imap`) and
the account `id`. Install the apps first, then add it yourself:

```bash
macos/mail-apps/install.sh password work   # prompts for the password; nothing is echoed
```

The Keychain item trusts only the two installed mail apps; any other program
that asks for it makes macOS ask you first. When an app starts, its launcher
reads the items and writes them to the helper's stdin as JSON (setting
`MAIL_CREDENTIALS_FD=0`), so the password never appears in a config file,
`.env`, compose file, argument, environment variable, log or container. With
ad hoc signing (the default), each rebuild changes the apps' signature, so
macOS asks again; re-run `install.sh password` after a rebuild, or sign with
a stable identity. Use an app password where your provider offers one, so
you can revoke it on its own.

Run from a terminal while developing, there's no launcher, so set
`password_env` and export that variable for the run. Don't keep it in a
shell profile.

**Login.** If the server offers `AUTHENTICATE PLAIN`, the helpers use it,
which carries non-ASCII passwords; otherwise they use `LOGIN`. They never
try one after the other fails, since each try counts against the account.
With `security = "starttls"`, a server that doesn't offer STARTTLS is
refused before anything is sent.

**What this doesn't protect.** The password opens the whole account, so
the limits come from our code and from keeping the password out of the
agent's reach:

- The reader (`scripts/lib/mail_imap.py`) only reads: `LIST`, `STATUS`,
  `EXAMINE`, `UID SEARCH` and `UID FETCH` with `BODY.PEEK`. Its connection
  class has no other commands, refuses any other `FETCH` items, and a test
  fails if the file gains a write.
- The drafts outbox (`scripts/lib/mail_imap_drafts.py`) only appends to the
  Drafts folder and reads the reply headers above. A test fails if it gains
  anything else. Neither has SMTP.
- The shared code (`mail_imap_conn.py`) only logs in and lists mailboxes.
  The reader app doesn't bundle the drafts outbox, and the drafts app
  doesn't bundle the reader.
- Hermes never gets the password, the IMAP host or a network path that
  needs them. The mail MCP still reads only the index.

**First real run.** Everything is tested only against synthetic servers:
GreenMail, in the clear and over TLS with a certificate made for the test,
and a small fake server for STARTTLS and the login methods. The Keychain
handoff is tested through both apps with a throwaway keychain file, so the
`install.sh password` prompt and your login keychain are left for your first
run. Try a real account from a terminal first, then move the password to
the Keychain. A terminal run has no launcher, so it never sees Keychain
passwords.

1. Add the account to `mail-index.toml` with `password_env`, say
   `password_env = "WORK_IMAP_PASSWORD"`, and set the variable for this
   shell only: `read -rs WORK_IMAP_PASSWORD && export WORK_IMAP_PASSWORD`.
2. `make mail-index-mailboxes`, then set `mailboxes` from the listing.
   `special_use` shows which folders the server flags as Junk, Trash and
   so on.
3. `make mail-index-doctor`. Check `accounts[].details_readable` equals
   `details_sampled`. `server_special_use` says whether Junk and Trash can
   be found by flag; if it's `false`, name mailboxes exactly.
4. `make mail-index`, then `unset WORK_IMAP_PASSWORD`.
5. Remove `password_env` from the config. Build and install the reader
   ([macOS apps](#macos-apps)), then `macos/mail-apps/install.sh password
   work`.
6. `launchctl kickstart gui/$(id -u)/local.assistant.mail-reader`, then
   read `~/Library/Logs/assistant-mail/mail-reader.log`. A run that worked
   logs counts. A missing or refused password logs a `mail-index:` line
   naming the account, and
   `launchctl print gui/$(id -u)/local.assistant.mail-reader` shows
   `last exit code = 4`.

Exit code 4 means the server refused the login or no password was found,
and 5 means the server couldn't be reached.

**Proton.** Run Proton Mail Bridge (a paid Proton plan) and point an account
at it: `host = "127.0.0.1"`, `port = 1143`, `security = "starttls"`, the
Bridge password as the password, and `tls_ca` set to the certificate Bridge
exports, since it signs its own. The indexer runs on the host, so it reaches
Bridge on localhost.

**Other ways in.** Hermes has its own email paths that would sidestep this:
its email gateway (`EMAIL_*` settings), and any bundled mail-client skill
that runs from the terminal tool. Both give the agent the password. Don't
turn them on for an account you index here.

**Testing.** `make mail-imap-test` starts a throwaway GreenMail IMAP server in
Docker, loads the synthetic messages into it, and checks that the IMAP source
indexes them the same way as the other sources, that nothing on the server
changed, and that drafts land in Drafts. It also:

- makes a CA and server certificate with `openssl` and checks that indexing
  over IMAPS with `tls_ca` gives the same index, and that without `tls_ca`
  the run exits 5;
- checks STARTTLS, `AUTHENTICATE PLAIN`, `LOGIN` and `LOGINDISABLED`
  against a fake server in the test, including that no credentials are sent
  before TLS starts;
- on macOS, builds both apps with `MAIL_APPS_KEYCHAIN` pointing at a
  keychain file it creates and deletes, stores the test passwords there
  trusting only those apps, and runs the reader and the drafts helper with
  no `password_env`. It checks that your keychain search list is unchanged.

The GreenMail parts skip when Docker isn't running, the certificate parts
without `openssl`, and the app part off macOS or without `swiftc`. No real
account is ever used.

## Design

Both sources feed the same index, and everything after the index is the
same. The diagram shows option **B**, the store source; the script source
swaps the first arrow for Automation → Mail.app:

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

- **One narrow program holds the permission.** That program is the indexer's
  own app ([macOS apps](#macos-apps)), not a terminal, `python3`, `node` or the
  container runtime. Run from a terminal while developing, the terminal holds
  it instead. With the store source it opens the
  Envelope Index read-only from a snapshot copy (database plus `-wal`). It
  works out the schema with `PRAGMA table_info` and refuses to run on a
  layout it doesn't recognise. It finds message files by globbing, because
  folder layouts shift between releases.
- **The scripting code only reads.** `scripts/lib/mail_script.js` reads
  properties and nothing else. A test fails if it gains a send, move,
  delete, compose or similar call.
- **Scope is explicit, with no defaults.** The accounts, the mailboxes and
  the date window (and, for the store source, the store path) all come from
  configuration, and nothing is indexed until they're set. Bodies and
  attachments are separate opt-ins. Junk and Trash are excluded unless
  listed.
- **Hermes sees the index, never the store.** The index lives in the
  gitignored `data/` tree (see [User data directory](data-dir.md)) and is
  served through a read-only MCP with narrow tools: search, get one message,
  list a thread. Compose files never mount the Mail folder.
- **Mail content is untrusted input.** Messages can carry instructions aimed
  at the agent. Tool results mark message text as quoted data, and the MCP
  has no send, move or delete tools.
- **Incremental.** The indexer compares the message ids and flags it already
  has with what the source lists, so later runs fetch only new messages.
  Actions such as mark read or move would be a separate, opt-in tool.

## Indexer

`scripts/mail-index.py` runs on the host. Python 3.11 or newer, standard
library only. The library is `scripts/lib/mail_index.py`, with the scripting
source in `mail_script.py` and `mail_script.js`.

```bash
cp mail-index.toml.example mail-index.toml   # gitignored; fill in since and accounts
make mail-index-mailboxes                     # account ids and mailbox names, to write the config
make mail-index-doctor                        # checks the source and scope; counts and timings only
make mail-index                               # build or update the index
```

Use `MAIL_INDEX_CONFIG=path` with make, or `--config path` with the script,
to point at a different config.

### Config

| Key | Required | Meaning |
|---|---|---|
| `source` | no, `"script"` | The source for accounts that don't name their own. `"script"` asks Mail.app (Automation permission). `"store"` reads Mail's files (Full Disk Access). `"imap"` reads an IMAP server ([IMAP source](#imap-source)) |
| `store` | with a `"store"` account only | The folder holding Mail's `V<n>/` directory. No default. Setting it with no account using the store is an error, so a config can't switch source by accident |
| `index` | yes | The index file to write. Must be outside the store. Keep it in the data tree |
| `since` | yes | Only messages received on or after this date |
| `[[accounts]]` | yes, at least one | `id` (from `make mail-index-mailboxes` for Mail.app accounts; your own name for IMAP ones) and `mailboxes`. Optional `source`; IMAP accounts take the keys under [IMAP source](#imap-source). Ids must be unique |
| `bodies` | no, `false` | Store message bodies for search. The script source takes Mail's plain-text rendering; the store source converts HTML to text |
| `attachments` | no, `false` | Store attachment names, types and sizes. Contents are never copied |
| `max_body_chars` | no, `100000` | Longest body kept per message |
| `script_timeout` | no, `600` | Seconds to wait for each call to Mail (script source) |

`mailboxes` entries are exact names (`INBOX`, `Projects/Alpha`, matched
ignoring case) or patterns. In a pattern, `*` matches within one level and
`**` matches any number of levels:

| Entry | Matches |
|---|---|
| `Projects` | `Projects` only, not `Projects/Alpha` |
| `*` | every top-level mailbox |
| `Clients/*` | mailboxes directly under `Clients` |
| `Clients/**` | `Clients` and everything under it |
| `**` | every mailbox |

Patterns skip Junk and Trash folders (Junk, Spam, Trash, Deleted Messages,
Deleted Items, Bin); name one to include it.

Prefer exact names with the script source. An account whose entries are all
exact names is never listed in full: each mailbox is looked up by its path.
An account with a pattern has its whole mailbox tree walked on every run,
which can take tens of seconds on accounts with many mailboxes or labels.
`run` and `doctor` count exact names that weren't found as
`mailboxes_missing`.

### What a run does

With the **script source** (default):

1. Lists each in-scope account's mailboxes through Mail.app, walking nested
   mailboxes from their parent.
2. For each mailbox in scope, fetches every message's id and received date
   in one call per property, then subject, sender, read, flagged, deleted
   and Message-ID the same way. Asking for a property across the whole
   mailbox is far faster than asking each message or using a `whose`
   clause. If the message list changes between those calls, it starts the
   mailbox again so the values stay lined up.
3. Keeps messages received on or after `since` that aren't marked deleted.
4. For new messages only, in batches of 50, fetches the raw headers, plus
   Mail's plain-text body when `bodies` is on and attachment names, types and
   sizes when `attachments` is on. If Mail can't produce a message, it is
   still indexed from its summary.
5. Updates the index the same way as the store source (step 5 below).

With the **store source** (`source = "store"`):

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
`missing_files`, `unreadable`, `accounts_missing`, `mailboxes_missing`,
`compacted`) and never message content.
For the script source, `missing_files` counts messages Mail listed but
couldn't produce.

`doctor` never prints message content either. For the script source it
prints how many mailboxes and messages are in scope, the seconds spent
listing mailboxes and summaries, and the seconds per message to fetch
details for a sample of 20. For the store source it prints the version
folder, which optional columns exist, whether dates look like Unix seconds,
and how many messages and files are in scope.

### The index

One SQLite file, owner-only (`0600`, folder `0700`). Table `messages` is keyed
by the index's own id ([Index ids](#index-ids)), and also holds the source's
id for the message (`source_id`: Mail's message id for the script and store
sources, `UIDVALIDITY.UID` for IMAP), the account, mailbox, Message-ID,
thread id, dates, sender, recipients (JSON), subject, flags, the optional
body and attachment list. `messages_fts` is an FTS5 table over subject,
sender, recipients and body, with diacritics folded. `meta` records the
schema version, each account's source, a fingerprint of the scope and the
last run time. A schema 1 index is upgraded in place on the next run; for
any other schema change, delete the index and re-run. Moving an account to
another source empties that account's rows on the next run and indexes it
again, because ids, flags and thread ids come out differently.

**Deleted text is overwritten.** When a message leaves scope, or its body is
blanked because you turned `bodies` off, its text is overwritten in the
file, not just unlinked. SQLite's `secure_delete` zeroes the freed space, and
FTS5's `secure-delete` option drops the message's words from the search
index straight away (it needs SQLite 3.42 or newer, both for the indexer and
in `mail-mcp`). The index is also compacted (`VACUUM`) when that isn't
enough:

- once for an index written before this was added;
- after removals, on SQLite too old for FTS5 `secure-delete`, merging the
  search index first;
- when more than a quarter of the file is free space.

`VACUUM`'s scratch copy is written beside the index, not in the system temp
folder. None of this can reach copies already in a backup, iCloud or the
SSD's spare blocks. Use FileVault, keep the index folder out of iCloud
Drive, and exclude it from backups.

**Size and speed.** With `bodies` on, expect the index to be roughly twice
the size of the plain text it holds, because the search index keeps its own
copy. The first run fetches every in-scope body; later runs fetch only new
messages. Progress is saved every 50 messages, so a first run cut short by
sleep, Mail quitting or a timeout picks up where it stopped next time. The
reader app's 15-minute schedule doesn't start a second copy while one is
still running. With the script source, every run also asks Mail for a summary of
every message in each in-scope mailbox, which is how it spots new, removed
and re-flagged mail. On a large mailbox, check `make mail-index-doctor`
before widening the scope: `seconds_message_summaries` is the cost every run
pays, and `seconds_per_message_details` times the number of messages is the
first run's cost.

Threads use Mail's conversation id when the store source has it. Otherwise a
message joins the thread of an indexed message it replies to (`In-Reply-To`
or `References`), and starts its own thread if there is none.

### First run on real mail

Both sources are tested only against synthetic mail, so the first real run
checks the assumptions. Report counts and timings, not messages.

**Script source:**

1. Run `make mail-index-mailboxes` and allow your terminal to control Mail
   when macOS asks. Check that each account and nested mailbox appears once,
   with plausible counts. The fake Mail can't confirm how real Mail lists
   nested mailboxes.
2. Fill in `mail-index.toml` and run `make mail-index-doctor`.
   `accounts_missing` should be empty, `details_readable` should equal
   `details_sampled`, and the timings show how long a full run will take.
   Start with a short `since` window on a large mailbox.
3. Exit code 4 means macOS refused Automation access: allow it under System
   Settings > Privacy & Security > Automation. Exit code 5 means a call to
   Mail failed or timed out, and the error gives the Apple event code.

Run from a terminal, the Automation permission belongs to the terminal, so
anything run from it can control Mail. That's fine for this first check; for
everyday use, install the reader app ([macOS apps](#macos-apps)) and allow it
instead.

**Store source:**

1. Run `make mail-index-doctor`. `dates_look_like_unix_seconds` should be
   `true` and `message_files_found` should equal `messages_in_scope`. If not,
   the store's layout differs from the synthetic one.
2. Exit code 3 means the metadata layout wasn't recognised, and the error
   names the missing columns. Exit code 4 means macOS denied access.

macOS grants Full Disk Access per program. Running the indexer with the shared
`python3` means granting that interpreter access, and with it every Python
script on the machine. Grant it to the reader app instead
([macOS apps](#macos-apps)).

## mail-mcp

`compose/mail-mcp` is a small Python server (`mcp==1.30.0`, streamable HTTP
at `http://mail-mcp:8000/mcp`, compose-internal only). The query logic in
`mail_query.py` uses only the standard library.

| Tool | Returns |
|---|---|
| `mail_status` | Message count, date range, last index run, whether bodies are searchable, `newest_id`, each account/mailbox with total and unread counts |
| `mail_search` | Up to 50 summaries, newest first: id, date, sender, subject, account, mailbox, read/flagged, attachment count, a `reply_ref` for drafts, and a snippet when the query matched. Filters: `query` (every word must match, as a prefix), `sender`, `mailbox`, `since`/`until` (inclusive dates), `unread_only`, `flagged_only`, `limit`, `offset`, and `after_id` (only mail indexed after that id, such as a `newest_id` from an earlier `mail_status`) |
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

**Watermark.** `GET http://mail-mcp:8000/watermark` returns one line,
`newest_id=<n>`, which changes only when newer mail is indexed. Schedulers
that skip work while a source is unchanged can poll it. It never includes
message content, because a scheduler may paste it into a prompt as is.
Message ids are the index's own: they count up as mail is indexed, across
every account, and are never reused. A message moved to another mailbox
gets a new one.

## Proactive review (cron)

Hermes can check new mail on a schedule and send you a short report.

```bash
make mail-triage-cron                       # every 15 minutes, to Signal if SIGNAL_HOME_CHANNEL is set
make mail-triage-cron SCHEDULE="every 1h" DELIVER=local
make mail-triage-cron-show
make mail-triage-cron-remove
```

`make mail-triage-cron` (`scripts/mail-triage-cron.sh`) creates a Hermes cron
job named `mail-triage` on the default profile, replacing any earlier one:

- **Mail tools only.** Its tool list is just `mail`: no web, browser,
  terminal, drafts or other MCP servers. The script reads the job back and
  removes it if Hermes didn't store that list.
- **Runs only on new mail.** Hermes checks the watermark each tick and skips
  the model unless newer mail was indexed. The change it detects carries the
  previous `newest_id`, which the job passes to `mail_search` as `after_id`,
  so each run looks only at mail it hasn't seen. The first run looks at
  today's mail.
- **Reports to you only.** Delivery is Hermes's scheduler, which the model
  can't redirect, and cron jobs never get Hermes's messaging tools. When
  nothing needs you, the job answers `[SILENT]` and nothing is sent.
- **Prompt.** `compose/hermes/mail-triage-prompt.md`: what needs a reply
  today, what's worth knowing, and a count of the rest. It tells the model
  never to follow instructions in messages and never to pass on their links,
  phone numbers or payment details. Pass `--prompt-file` to the script to
  use your own; keep those rules.

The job needs the mail MCP on the default profile (`setup.sh --mail` and the
bootstraps) and a current `mail-mcp` image. `mail-mcp` is built locally and
`make up` doesn't rebuild it, so after pulling changes run
`docker compose up -d --build mail-mcp`. The script refuses to create the job
if Hermes can't reach `/watermark`, and `make doctor` flags an image without
it. The job runs as often as the reader app indexes (every 15 minutes by
default). If an agent run fails, that batch isn't retried: Hermes records the
new watermark before the run.

**Other cron jobs don't get mail.** A Hermes cron job with no tool list of
its own gets every MCP server on its profile, and a list that names no MCP
server gets them all too. So while mail or drafts is registered on a
profile, the bootstraps set `platform_toolsets.cron` to Hermes's default cron
tools plus every other MCP server, leaving out `mail` and `drafts`. That
list is recorded in `.mail-cron-toolsets.json` beside the profile's
`config.yaml`, and refreshed or removed on later runs. If you wrote your own
`platform_toolsets.cron`, it's kept with only mail and drafts taken out. Add
an MCP server by hand and re-run the bootstraps so cron jobs get it too.

A job with its own `enabled_toolsets` isn't covered by that list. If it
names no MCP server it still gets mail, so give it the MCP servers it needs
by name (or `no_mcp`). `make doctor` lists every cron job that can read mail
alongside anything else (`scripts/check-mail-cron.py`, which reads job names
and tool settings, never prompts).

## Drafts

Hermes writes drafts, and you send them. Nothing in the stack can send
email.

```
Hermes ──mail_draft_create──▶ mail-drafts-mcp (container) ──▶ MAIL_DRAFTS_DIR/queue
                                                                    │
                  scripts/mail-drafts.py (host, Automation → Mail) ◀┘
                                                                    │
                                          Mail.app Drafts ◀─────────┘ (you click Send)
```

```bash
./scripts/setup.sh --mail-drafts   # profile, MAIL_DRAFTS_ENABLED=1, data/mail-drafts (0700)
make up
make hermes-reload
make mail-drafts-doctor            # counts only; macOS asks to let your terminal control Mail
make mail-drafts-watch             # keep running: saves queued drafts every 30 s
```

In WebUI: *Draft a reply to Alex saying Thursday works.*

**Tools.** The `drafts` MCP has two tools, registered on the default and
`browser` profiles and never on `api-server`:

| Tool | Does |
|---|---|
| `mail_draft_create` | Queues a draft: `to`, `cc`, `bcc`, `subject`, `body`, an optional `from_address`, or `reply_to` with the `reply_ref` of a `mail_search` or `mail_get` result (the older `{id, account, mailbox}` form still works for Mail.app accounts). Returns a `request_id` |
| `mail_draft_status` | `queued`, `created`, `failed` or `rejected` (with a reason), or `unknown` |

Results remind the model that nothing was sent. The server's instructions
tell it to draft only what the user asked for, never because a message
said to.

**Rules, checked twice.** The container checks each request before queueing
it, and the host helper checks it again before Mail sees it, so a request
written straight into the queue gets no further:

- Addresses are plain `name@domain`, with no display names, commas or line
  breaks, and at most 20 recipients in all.
- The subject is one line. The body is at most 100,000 characters.
- There's no way to attach files.
- `from_address` must be one of your Mail accounts' addresses, or an
  `address`/`addresses` entry of an IMAP account in the config.
- Unknown fields are refused.
- The queue holds at most 50 requests, and the helper saves at most 20
  drafts an hour (`--max-per-hour`). Extra requests wait in the queue.

**Replies** use Mail's own reply command on the message the request names,
so Mail fills in the recipient, subject and threading headers. `body`
replaces the quoted text. The helper never reads the message: it only asks
Mail to reply to it.

**IMAP accounts.** Pass the indexer config with `--config` (or
`MAIL_INDEX_CONFIG`), and drafts for its `source = "imap"` accounts are
appended to the account's Drafts folder instead (found by its `\Drafts`
flag, then by name, or set `drafts_mailbox`). The helper picks the account
from `reply_to`'s account, then from `from_address`. A draft with neither
goes to Mail.app, or to the only IMAP account when `--no-mail-app` is set
(for machines without Mail.app). An IMAP reply reads the original's
Message-ID, References, From, Reply-To and Subject headers, never its body,
and goes to its Reply-To or From address with `Re:` before the subject. The
original isn't marked read or answered. The password comes from the
Keychain as for the [IMAP source](#imap-source). IMAP failures are recorded
as `failed` with `imap_login_refused`, `imap_password_missing`,
`imap_unreachable`, `drafts_mailbox_not_found` or `reply_message_not_found`.

**The host helper** (`scripts/mail-drafts.py`) has no access to the mail
index, and `scripts/lib/mail_drafts.js` reads no message content. A test fails
if that script gains a send, delete or move call, or reads a subject, sender,
body or headers. Drafts are saved hidden; `--open` also shows each one in a
compose window. After a draft is saved, the helper deletes its request and
writes `status/<id>.json` with the state and a short reason, never the
draft's text or recipients. Status files are pruned after 30 days. `run`
and `doctor` print counts only. Exit code 4 means macOS refused Automation
access; 5 means a call to Mail failed.

**Container.** `mail-drafts-mcp` mounts only `MAIL_DRAFTS_DIR`, with a
read-only root filesystem, all capabilities dropped and `no-new-privileges`.
It runs as `HERMES_UID:HERMES_GID`, which on Linux must own the `0700` folder.
Point the helper at the same folder: `make mail-drafts` uses
`MAIL_DRAFTS_DIR` (default `./data/mail-drafts`).

**Separate permission.** Installed as its own app
([macOS apps](#macos-apps)), the drafts helper holds its own Automation
permission, apart from the reader's. Run from a terminal with
`make mail-drafts`, it uses the terminal's.

**First real run.** The JXA that builds drafts is tested only against the
fake Mail. Try it with a draft to yourself first: ask Hermes for one, run
`make mail-drafts`, and check that it appears in Mail's Drafts with the right
recipients and that nothing was sent. Then try a reply.

## macOS apps

macOS gives permissions to the program that's responsible for a process. Run
from a terminal, that's the terminal, so a grant covers everything you run
there. To keep grants narrow, each helper ships as its own small app:

| App | Bundle id | Runs | Permissions it asks for |
|---|---|---|---|
| Assistant Mail Reader | `local.assistant.mail-reader` | the indexer, every 15 minutes | Automation → Mail; Full Disk Access only if you use `source = "store"`; the Keychain items you add for IMAP accounts |
| Assistant Mail Drafts | `local.assistant.mail-drafts` | the drafts helper, continuously | Automation → Mail (unless `--no-mail-app`); the same Keychain items |

Each app is a compiled launcher (`macos/mail-apps/Launcher.swift`) plus only
its own helper's scripts, sealed by its code signature. The reader app
doesn't contain the drafts code and the drafts app doesn't contain the
reader's. A changed script breaks the signature, so editing the repository
can't change what a granted app does. The launcher runs the scripts with
`python -I -B` and a minimal environment (no `PYTHONPATH`, no user site,
no bytecode written into the bundle). The apps have no Dock icon.

```bash
make mail-apps        # build both into build/mail-apps (needs swiftc and Python 3.11+)
macos/mail-apps/install.sh reader --config "$PWD/mail-index.toml"
macos/mail-apps/install.sh drafts --dir "$PWD/data/mail-drafts"
macos/mail-apps/install.sh password work   # only for IMAP accounts, once per account
```

`install.sh` copies the app to `~/Applications` and loads a LaunchAgent
(`~/Library/LaunchAgents/local.assistant.mail-<name>.plist`) that runs it:
the reader every `--interval` seconds (default 900), the drafts helper
continuously (`--open`, `--max-per-hour`, `--config` and `--no-mail-app`
pass through; give the drafts app `--config` for IMAP drafts). Because launchd
starts the app, macOS asks on the app's behalf: allow it to control Mail,
and, if the repository is under Documents, to read that folder. Logs, which
hold counts only, go to `~/Library/Logs/assistant-mail/`. Add `--dry-run` to
see the agent's plist without installing anything, and
`install.sh uninstall reader` (or `drafts`) to remove one. Run the agent
now with `launchctl kickstart gui/$(id -u)/local.assistant.mail-reader`.

Build settings:

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_APPS_PYTHON` | `python3` on `PATH` | Interpreter the launcher runs. The build records the path the interpreter reports, which for Homebrew survives patch upgrades |
| `MAIL_APPS_SIGN_IDENTITY` | `-` (ad hoc) | Signing identity. macOS ties grants to the signature, so with ad hoc signing each rebuild asks again. A stable identity (a free Apple Development certificate is enough) keeps them |
| `MAIL_APPS_BUNDLE_PREFIX` | `local.assistant` | Bundle id and launchd label prefix. Set the same value for `install.sh` |
| `MAIL_APPS_DIR` | `~/Applications` | Where `install.sh` puts the apps |
| `MAIL_APPS_KEYCHAIN` | unset | A keychain file the launcher searches instead of your keychain search list. For tests; `install.sh password` doesn't use it |

`make mail-apps-test` builds both apps into a temporary folder, checks their
ids, signatures, entitlements and contents, checks that a changed script
breaks the seal, and runs the bundled reader against a synthetic store and
the drafts helper over an empty queue. It installs nothing and never reaches
Mail.

**First real run.** After installing the reader, kickstart it and check its
log for counts, and that System Settings > Privacy & Security > Automation
lists *Assistant Mail Reader* under Mail. Your terminal no longer needs that
permission; you can turn it off there.

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

The same specs also feed a fake Mail.app for the script source.
`synthetic_mail.script_fixture()` turns them into accounts, nested mailboxes
and messages, and `scripts/lib/fake_mail_app.js` serves that model to
`mail_script.js` under Node, the way JXA does: callable element collections,
batch property calls and `byId`. It can also refuse Automation access, list
nested mailboxes at the top level, and add a message partway through a
listing. Account ids and message ids match the synthetic store, so the
tests check that both sources index the same messages the same way.

```bash
make mail-script-test   # needs node; nothing here runs osascript
make mail-drafts-test   # drafts: validation, queue, and the helper against the fake Mail
```

For drafts, the fake also makes outgoing messages, starts replies and
appends each saved draft to a file the test reads. It has no way to send.

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
