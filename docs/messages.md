# Messages from iMessage (design)

**Status: design only.** Nothing in the stack reads messages yet. This page
records how Hermes should answer questions about iMessage and SMS history,
and hear about new messages, by reading Messages.app's local database. It
also records which open-source projects were evaluated and why none of them
is adopted as-is. Research is as of October 2026.

## Goals

- Hermes can search and read the conversations you choose (chats, a date
  window) and answer questions about them.
- Hermes learns about a new message in those conversations within a few
  seconds, without polling from a chat turn.
- Nothing in the stack can send, react, mark read, edit or delete.
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

### Sharing the Full Disk Access helper with mail

The email design already calls for "one narrow program" with Full Disk
Access, and lists packaging it as a follow-up: the mail indexer currently
runs under the shared `python3`. Messages should use **the same helper**:

- One signed binary, one TCC entry, one LaunchAgent, with a subcommand or
  module per source (`mail index`, `messages watch`). Each source reads only
  its own config and writes only to its own `data/` folder.
- One grant versus two doesn't change what can be reached. Full Disk
  Access is all-or-nothing, and any program that holds it can read both
  stores. What keeps the setup safe is that the program holding it is small,
  pinned and reviewed, so one well-kept helper beats two.
- The helper becomes a long-running LaunchAgent, because messages need a
  watcher. Mail indexing can run on a timer inside it.
- Mail can join without a rewrite: the helper runs the existing Python
  indexer as its child, so the indexer inherits access for that run only.
  It must check the script's hash before running it. Otherwise anything that
  can edit the repo gets Full Disk Access on the next run. Porting the mail
  indexer into the binary later removes that check.

TCC details to confirm in a spike on macOS 26:

- A plain binary started by launchd is its own responsible process, so the
  grant goes to that binary. Confirm with `launchctl procinfo <pid>`.
- A child process inherits access from it.
- How re-signing an upgraded binary affects a grant that has gone stale.

### Phases

1. **Synthetic store.** Generate a fake `chat.db` under `synthetic-messages/`:
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
- **No send path.** Don't link send, AppleScript or IMCore code into the
  helper or the MCP.
- **Keep coding agents out of real messages.** Block them from
  `~/Library/Messages` and related stores with their own permission rules,
  and don't grant Full Disk Access to the terminal they run in.
