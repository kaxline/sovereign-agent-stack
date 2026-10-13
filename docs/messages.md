# Messages from iMessage (design)

**Status: design only.** Nothing in the stack reads messages yet. This page
records how Hermes should answer questions about iMessage and SMS history,
and hear about new messages, by reading Messages.app's local database. It
also records which open-source projects were evaluated and why none of them
is adopted as-is. Research is as of October 2026. Only
[the synthetic store](#synthetic-message-store) exists so far.

## Goals

- Hermes can search and read the conversations you choose (chats, a date
  window) and answer questions about them.
- Hermes learns about a new message in those conversations within a few
  seconds, without polling from a chat turn.
- The reader can't send, react, mark read, edit or delete. Sending is a
  [separate, opt-in service](#sending-separate-service) with its own
  permission.
- Nothing can read conversations you didn't scope in, and no container
  mounts the Messages folder.
- Contributors, and any coding agent they use, never need real messages to
  build or test it.

## Where Messages keeps data

Messages.app keeps everything in `~/Library/Messages/chat.db`, a SQLite
database in WAL mode (`chat.db-wal`, `chat.db-shm`), with attachments under
`~/Library/Messages/Attachments/`. The tables that matter are `message`,
`handle`, `chat`, `chat_message_join`, `chat_handle_join`, `attachment` and
`message_attachment_join`. Things a reader has to get right:

- **Message text is usually not in `message.text`.** Since macOS 13, most
  bodies sit in `message.attributedBody`, a `NSAttributedString` serialised
  in Apple's legacy *typedstream* format. Readers that only use `text` miss
  most messages. Naive byte scanners break on long messages.
- **Dates** are nanoseconds since 2001-01-01 UTC.
- **Reactions** are their own rows (`associated_message_type` ≠ 0).
- **Edits and unsends update existing rows** (`date_edited`,
  `date_retracted`, history in `message_summary_info`). They don't create new
  `ROWID`s, so a `ROWID` cursor alone misses them.
- **A new row can be visible before its `chat_message_join` row**, so a
  reader has to retry rows with no chat instead of skipping past them.
- macOS 26 adds `message.ck_chat_id` and new group id formats.

Apple doesn't document any of this, and the schema changes between macOS
releases. Everything above comes from the open-source readers listed below.
macOS only lets processes with **Full Disk Access** read the folder.

## Projects evaluated

About 30 projects read `chat.db`. The ones that matter for this design:

| Project | What it is | Licence | Good at | Against |
|---|---|---|---|---|
| [anipotts/imessage-mcp](https://github.com/anipotts/imessage-mcp) | MCP server (Node 24) | MIT | No send code at all. Best decoder among the MCP servers (bounds-checked typedstream and keyed-archiver parsers). `sync_messages` change cursor covers new, edited, unsent and deleted messages. Privacy modes. Tests and CI | No push or watcher. Reads the live store, so the Node runtime needs Full Disk Access. Small project (27 stars) |
| [openclaw/imsg](https://github.com/openclaw/imsg) (was steipete/imsg) | Swift CLI and `IMsgCore` library, JSON-RPC over stdio | MIT | Most mature new-message watcher (kqueue on the database, WAL, SHM and folder, 250 ms debounce, 5 s fallback poll, inode re-attach, resumable `ROWID` cursor, overflow signal). Very active (1.4k stars). OpenClaw's iMessage channel runs on it | Large send and IMCore-injection surface with no off switch. Heuristic decoder. Misses edits and unsends (issue #340). Releases every few days |
| [ReagentX/imessage-exporter](https://github.com/ReagentX/imessage-exporter) (`imessage-database`, `crabstep`) | Rust exporter and library | GPL-3.0 | Most complete and correct decoding (full typedstream parser, edit history, RCS, polls). Explicit macOS 26 and 27 support. 5.7k stars | No watcher or incremental API. GPL doesn't fit this MIT repo. One maintainer |
| [cyberpapiii/imessage-max](https://github.com/cyberpapiii/imessage-max) | MCP server (Swift) | MIT | Only MCP server with a built-in watcher and push | `send` is always registered and sends immediately. Hard-coded database path. Heuristic decoder. Push only over HTTP SSE, and HTTP has no auth |
| [carterlasalle/mac_messages_mcp](https://github.com/carterlasalle/mac_messages_mcp) | MCP server (Python) | MIT | Most popular (331 stars), `since_rowid` cursor | Send, schedule and create-contact tools always on. "Wait for new messages" blocks the turn |
| [BlueBubbles server](https://github.com/BlueBubblesApp/bluebubbles-server) | Electron relay | Apache-2.0 | Change detection that also catches edits, unsends and receipts (re-query a date window, compare edit and receipt columns). Hermes ships a BlueBubbles adapter | Sends by design, opens the database read-write, optional SIP-off helper. No release since May 2025 |
| [photon-hq/imessage-kit](https://github.com/photon-hq/imessage-kit) | TypeScript SDK | MIT | Handles the macOS 26 schema (`ck_chat_id`) and the join race | Sends. Watcher has no fallback poll |

Ruled out: `mautrix-imessage` and `bagoup` (AGPL), `mimessage` (no licence,
OpenAI), `iMessageGPT` (no `attributedBody`, OpenAI, SQL injection),
`adelaidasofia/imessage-mcp` (telemetry ping, send), `wyattjoh/imessage-mcp`
(read-only and clean but a crude decoder and no change feed), and a long tail
of small or abandoned servers.

Useful as references only: Apple's own `NSUnarchiver`, which decodes
typedstream natively and is what
[iMessagePrinter](https://blog.fsck.com/agent-blog/2026/02/19/imessageprinter/)
uses. Also `pytypedstream` (LGPL) and caleb531's `imessage-conversation-analyzer`,
which uses it.

**No project fits as-is.** The ones that watch for new messages can also
send, and the one with no send code can't watch. Every one of them expects
the program that serves the agent to hold Full Disk Access itself.

## How Hermes would hear about new messages

An MCP server can't start a Hermes turn. Hermes's MCP client only acts on
`tools/list_changed` notifications (for dynamic tool discovery). It ignores
resource subscriptions and custom notifications, so "push" from imessage-max
or a forked anipotts would go nowhere.

Hermes does ship a **webhook gateway** (`gateway/platforms/webhook.py`). It
takes HMAC-signed POSTs with timestamp replay protection, has per-route rate
limits and an idempotency cache, and can either run a prompt template or fire
an existing cron job with the event as context. A new-message event should
arrive that way.

The built-in BlueBubbles adapter is not an option. It needs a BlueBubbles
server, which sends messages and opens the database read-write.

## Recommended design

Build the same shape as [the email design](email.md), with a watcher added:

```
chat.db ──(Full Disk Access, read-only)──▶ messages helper (host, signed binary)
                                              │ scoped by config     │ new-message event
                                              ▼                      ▼ (metadata only, HMAC)
                              data/messages/ index (SQLite + FTS)   Hermes webhook gateway
                                              │                      │ starts an unattended turn
                                messages MCP (read-only, container) ◀┘ agent calls tools
                                              ▼
                                           Hermes
```

### The helper

- **A small signed native binary, written in Swift, holds the Full Disk
  Access grant.** Not `node`, `python3`, a terminal or the container runtime.
  Granting an interpreter access hands it to every script that interpreter
  runs. Swift gives native kqueue, code signing and Apple's own typedstream
  decoder.
- **Watcher: port openclaw/imsg's `MessageWatcher` design** (MIT, with
  attribution): kqueue on `chat.db`, `-wal`, `-shm` and the folder; a 250 ms
  debounce; a 5 s fallback poll; re-attach when a file's inode changes; a
  persisted `ROWID` cursor; retry rows that have no chat yet. Copy only the
  watcher and query code, not the `IMsgCore` module, so that no send or
  AppleScript code is linked into a binary with Full Disk Access.
- **Edits and unsends: add BlueBubbles' second pass.** On each wake,
  re-query a recent window (by the indexed `date`) and compare
  `date_edited`, `date_retracted`, `date_read` and `date_delivered` against
  the index.
- **Decoder: `NSUnarchiver` first**, with a length-aware byte scanner as a
  fallback. Count fallbacks and failures in `doctor` output.
- **Read-only and careful with WAL.** Open with `mode=ro` and a 5 s busy
  timeout. Run each wake in a fresh read transaction, never hold one open,
  and never checkpoint. A reader that checkpoints becomes a second writer
  (see the 2026 SQLite WAL-reset bug, fixed in 3.51.3). Probe columns with
  `PRAGMA table_info` and stop on a layout it doesn't recognise, as the mail
  indexer does.
- **Scope is explicit, with no defaults.** The store path, the chats (by
  chat GUID, or "all 1:1 chats with saved contacts"), the `since` date and
  whether attachment names are kept all come from configuration. Nothing is
  indexed until they're set. Attachment contents are never copied.
- **Events carry metadata only:** chat id, contact display name, message
  count and `ROWID` range, never message text. Each event is signed with a
  per-route secret and sent to the webhook gateway on the loopback host.
  Only chats listed as `notify` produce events, with a quiet-hours option.

### The MCP

- Runs in a container like the other MCP sidecars and mounts only
  `data/messages/` read-only. It never sees `chat.db` and needs no special
  permission.
- Narrow, read-only tools modelled on anipotts's design: search, get a
  conversation (paged), list conversations, `changes_since(cursor)`, resolve
  a contact. It has no send, react or mark-read tools.
- Message text is untrusted input. Tool results mark it as quoted data from
  a third party, the same as mail.
- Unattended (webhook-started) sessions get an allowlist like the CalDAV and
  LightRAG ones: the read tools, plus whatever the turn is meant to do
  (e.g. notify you on Signal). A stranger's text must not be able to steer a
  session that holds write tools.

### Full Disk Access, and sharing the helper with mail

Mail no longer needs Full Disk Access by default. Since the
[2026-10-13 decision](email.md#decision-2026-10-13), the mail indexer reads
through Mail.app scripting (Automation → Mail), and reading Mail's files is an
opt-in fast mode (`source = "store"`). That mode's own signed executable is
still a follow-up.

Messages has no equivalent. Messages.app's scripting dictionary can send
messages and list chats, but it doesn't expose message history, and no other
supported API does. Reading messages therefore needs Full Disk Access, which
makes it the only part of the stack that needs it by default. So:

- **Reading messages is opt-in,** behind its own compose profile and off by
  default. The setup docs say plainly that it needs Full Disk Access and that
  the permission can't be scoped: the helper could read Mail, browser history
  and every other app's data.
- **The messages helper is the signed executable mail's store mode is
  waiting for.** Someone who opts in to both runs one helper, with a
  subcommand per source (`messages watch`, `mail index`), one TCC entry and
  one LaunchAgent. Each source reads only its own config and writes only to
  its own `data/` folder. Full Disk Access is all-or-nothing, so one grant
  reaches no more than two would. What keeps it safe is that the program
  holding it is small, pinned and reviewed.
- **Mail's store mode can join without a rewrite.** The helper runs the
  existing Python indexer as its child, so the indexer inherits access for
  that run only. The helper must check the script's hash first. Otherwise
  anything that can edit the repo gets Full Disk Access on the next run.
  Porting the store reader into the binary later removes that check.
- **Watch Apple's planned changes.** Apple said on 2026-10-02 that it will make
  Full Disk Access harder to grant. If a future macOS stops granting it to a
  self-signed helper, this design needs a Developer ID-signed, notarised
  build.

TCC details to confirm in a spike on macOS 26:

- A plain binary started by launchd is its own responsible process, so the
  grant goes to that binary. Confirm with `launchctl procinfo <pid>`.
- A child process inherits access from it.
- How re-signing an upgraded binary affects a grant that has gone stale.

### Phases

1. **Synthetic store** (done, see [below](#synthetic-message-store)). Generate a fake `chat.db` under `synthetic-messages/`:
   `attributedBody` blobs, long messages, group chats, reactions, edits,
   unsends, attachments, the join race, a prompt-injection message. Add a
   fake contacts database too.
2. **Helper: index.** Scoped, read-only, one-shot indexing with `doctor`,
   tested against the synthetic store.
3. **Helper: watch and events.** Watcher, edit pass, signed webhook events.
   Test it by appending rows to the synthetic store.
4. **MCP and Hermes wiring.** Container, tools, the unattended allowlist, a
   webhook route and a profile, and a CHANGELOG entry.
5. **First real run** (the owner runs it): `doctor` counts only, then the
   watcher.

## Sending (separate service)

Sending is optional, off by default, and built as its own service beside
the reader, not inside it. The reader and the sender share the index and the
watcher, but they don't share a binary, a permission or an MCP.

### Why it's separate

- **Different permission.** The only workable way to send is AppleScript
  asking Messages.app to send, which needs the **Automation → Messages**
  permission. The private-API route (IMCore injection) needs SIP disabled
  and is out of scope. If one binary held both Full Disk Access and
  Automation, any bug in it could read everything and send as you. Split,
  the reader can't send and the sender can't read history.
- **Different callers.** Read tools can be given to unattended,
  webhook-started sessions. Send tools go only to sessions you're actively
  in (WebUI), through the per-profile allowlists the stack already uses.
- **Separate off switch.** Turning sending off leaves reading untouched.

The sender resolves chats and recipients from the index, so it needs no Full
Disk Access. It confirms delivery through the watcher.

### Shape

```
Hermes (interactive profile)
   │ draft_message / send_draft
   ▼
messages-send MCP (container) ──loopback + token──▶ messages-send (host, Automation only)
   │                                                     │ osascript: send text to chat id
   │ approval code (out of band, e.g. Signal)            ▼
   └──────────────── you ─────────────────────────  Messages.app
                                                         │ new outgoing row in chat.db
                              messages helper (watcher) ◀┘ confirms delivery or failure
```

- **`messages-send` host binary.** Signed, started by launchd, with Automation
  permission only. Its one job is to send this text to this chat id. It
  refuses a chat that isn't on the allowlist unless the request carries a
  valid approval code. It listens only on the loopback address and requires a
  token. Containers can't run `osascript`, so it has to run on the host.
- **`messages-send` MCP.** Separate from the read MCP. Tools:
  `draft_message`, `list_drafts` and `send_draft`. It's enabled only on
  interactive profiles, and off unless you turn it on.

### Pitfalls and mitigations

| Pitfall | Mitigation |
|---|---|
| **Prompt-injection exfiltration.** An incoming text tells the agent to forward your history to a stranger. An agent that can both read untrusted messages and send will do it | Sessions started by a message event never get send tools. Sends outside the allowlist need your approval. The approval arrives on a channel the agent can't forge: a one-time code you send back on Signal, or a WebUI button. The agent never sees the code |
| **Wrong recipient.** Two contacts share a name, or a reply goes to a whole group | Send only to a chat id or exact handle taken from the index, never to a free-text name. Ambiguity is an error, not a guess. Group chats always need approval. The draft shows the resolved name and number |
| **Silent failure.** `osascript` reports success when the message fails, when Messages is signed out, or when it falls back to SMS | The watcher confirms each send: an outgoing row appears in that chat, and its error and delivered fields are checked. The result is reported as success, failure or timeout. No automatic retry without that check |
| **Duplicate sends** on retries or re-run turns | An idempotency key per draft, and each approved draft is sent at most once. A send log in `data/messages/` keeps ids, recipient, time and outcome, without the text |
| **Reply loops.** The agent's own sent message shows up in `chat.db` and triggers a new event | The watcher never raises events for outgoing (`is_from_me`) rows. Rate limits per recipient and per day |
| **It's your identity.** Recipients can't tell the agent wrote it | Recipient allowlist, length cap, quiet hours and a dry-run mode. The first phase is drafts only: the agent writes and you send |
| **AppleScript quirks.** iMessage vs SMS selection, new conversations needing an account and participant, attachments blocked by Messages' sandbox, behaviour changing between macOS releases | The first version sends text only, to existing chats. Attachments are a later, separately tested feature. Tests pin behaviour with a mocked `osascript`. A synthetic send self-test runs after macOS updates |
| **Automation prompts and stale grants.** The prompt goes to the responsible process, and grants go stale after upgrades | A signed binary started by launchd. Confirm which process gets the grant in the same macOS 26 spike as the reader |
| **Reaching the host from a container** | Check that Docker Desktop can reach a loopback-only host service through `host.docker.internal` before relying on it |
| **Edit, unsend, tapbacks, typing indicators** | Need the private API with SIP off. Not supported |

### Phases

1. **Drafts only.** The agent writes a draft and you send it yourself. If
   Messages on macOS 26 honours the `body` field of an `sms:` link, the
   draft can open as a prefilled compose window for you to send, which plays
   the part Mail's drafts do for email (unverified).
2. **Approved sends.** Allowlisted chats, each send approved by a one-time
   code over Signal or a WebUI button, with delivery confirmed by the
   watcher.
3. **Trusted chats** (optional). Sending without per-message approval to a
   short list of chats you name, still rate-limited and still logged.

## Synthetic message store

`scripts/lib/synthetic_messages.py` writes a fake Messages store with 28
made-up messages in five chats, plus a fake Contacts database:

```
<out>/Messages/chat.db                          WAL mode, subset of tables and columns
<out>/Messages/Attachments/<xx>/<nn>/<guid>/…   attachment files
<out>/AddressBook/Sources/<uuid>/AddressBook-v22.abcddb
<out>/manifest.json                             what was generated, for test assertions
```

It covers:

- bodies only in `attributedBody`, in both `text` and `attributedBody`, and
  only in `text` (old rows)
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

`attributedBody` blobs come from `scripts/lib/typedstream.py`, which writes
NSAttributedString archives that match Foundation's NSArchiver byte for byte.
The tests check that against archives Foundation produced, and on macOS with
`swiftc` they also decode every body with Foundation's NSUnarchiver.

Attachment `filename` values keep Messages' `~/Library/Messages/Attachments/…`
form. A reader has to map that prefix onto its configured attachment root and
must never expand `~`.

```bash
python3 scripts/make-synthetic-messages.py /tmp/synthetic-messages/store
make synthetic-messages-test
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

## Rules for contributors

- **Never use real messages or contacts as fixtures,** not even "anonymised".
  Use made-up names, `example.com` addresses and 555-01xx numbers.
- **Never give a Messages, contacts or backup path a default** that resolves
  to a real store. Never mount `~/Library/Messages` in a committed compose
  file.
- **Learn the format from documentation and open-source code,** not by
  opening a real `chat.db`, schema included. If a bug only shows up on real
  data, the person who owns the messages extracts a minimal case and rewrites
  it as a synthetic one.
- **No send path in the reader.** Don't link send, AppleScript or IMCore
  code into the helper or the read MCP. Sending lives only in
  [its own service](#sending-separate-service).
- **Test sending against a mocked `osascript`.** Never send to a real
  contact from a test, and never send to real contacts on the owner's
  behalf while developing.
- **Keep coding agents out of real messages.** Block them from
  `~/Library/Messages` and related stores with their own permission rules,
  and don't grant Full Disk Access to the terminal they run in.
