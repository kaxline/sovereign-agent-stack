# Messages from iMessage

Read-only iMessage and SMS history for Hermes, and a heads-up when new
messages arrive. Hermes reads your Mac's own Messages database, not a login
anywhere. A host-side app indexes the chats you choose. The
**`messages-mcp`** sidecar serves that index to Hermes. New messages fire a
triage cron job that has the messages tools and nothing else. Nothing in the
stack can send, react, mark read, edit or delete messages.

Everything has been tested only against synthetic data (see
[First real run](#first-real-run)). The code lives in
[`imessage/`](../imessage/README.md), which is self-contained so it can become
its own repository. Its README covers the indexer, watcher, app and MCP in
detail. This page covers how the stack wires them in, the design behind them,
and the projects they were compared with (research as of October 2026).

Requires the **`messages`** compose profile (`./scripts/setup.sh --messages`).

## Quick path

```bash
./scripts/setup.sh --messages      # profile, MESSAGES_MCP_ENABLED=1, webhook secret, data folders (0700), messages-index.toml
# Fill in messages-index.toml: store (your Messages folder), since, chats. Then build and install the reader app:
imessage/macos/build.sh
imessage/macos/install.sh --config "$PWD/messages-index.toml"
# System Settings > Privacy & Security > Full Disk Access > + > ~/Applications/Assistant Messages Reader.app
make up
make hermes-reload
make messages-triage-cron          # the job new messages fire; reports to Signal when SIGNAL_HOME_CHANNEL is set
make doctor                        # the Messages section
```

To pick chat ids for the config, `make messages-index-chats` lists them. It
reads Messages' database, so it needs Full Disk Access too. Run it from a
terminal that has the grant, or skip it and use patterns like `"*;-;*"`.

In WebUI: *What did Priya say about the lease?*

## How it fits together

```
Messages' database ──(Full Disk Access, read-only)──▶ Assistant Messages Reader.app (host, LaunchAgent)
                                                         │ index (scoped)         │ new-message events (metadata only)
                                                         ▼                        ▼
                                       data/messages/index.sqlite     data/messages-events/*.json
                                                         │ (read-only mount)      │
                                          messages-mcp (container) ◀──────────────┘ forwarder: check, sign, POST
                                         │ MCP tools                 │ http://hermes:8646/webhooks/messages
                                         ▼                           ▼
                               Hermes: default + browser    default profile webhook route
                               profiles (never api-server)  → fires the messages-triage cron job
                                                              (messages tools only) → report to Signal
```

- **The app** (`imessage/macos`) is the only thing with Full Disk Access. It
  keeps the index current and writes an event file when new messages arrive
  in the chats listed in `[notify]`. Events say which chat, how many and from
  whom, never what the messages say.
- **`messages-mcp`** mounts the index read-only and the events folder. It
  serves five read tools to the default and `browser` profiles, plus
  `messages_triage_batch` on the default profile only, where the triage job
  runs. Its forwarder checks each event file strictly, signs it with
  `MESSAGES_WEBHOOK_SECRET`, and POSTs it inside the compose network. No
  port is published.
- **The webhook route** (`messages`, on the default profile, port 8646 inside
  the container) fires the `messages-triage` cron job. The route's prompt
  carries only the message count, because anyone in a group can set its name.
  The bootstraps write the route and keep any routes you added. The
  container's own gateway serves the default profile under both
  `HERMES_GATEWAY_TOPOLOGY` settings, so the listener and the route work the
  same in `standalone` and `multiplex` ([Gateway topology](hermes.md#gateway-topology)).
- **The triage job** (`make messages-triage-cron`) has the messages tools and
  nothing else, so a message that tries to steer it has nowhere to send
  anything. Its report goes where its `deliver` says (Signal by default when
  set up), which the model can't change. It answers `[SILENT]` when nothing
  needs you.
- **Nothing gets lost.** Hermes skips an event run if the job is already
  running. `messages_triage_batch` keeps its own cursor, so the next run picks
  up whatever an earlier run didn't see. The job's schedule (every 30 minutes)
  is a safety net: Hermes checks the MCP's content-free `/watermark` and runs
  the model only if something is still unreported. While event runs keep up,
  the watermark stays at `none` and scheduled ticks cost nothing.
- **Other cron jobs don't get messages.** The bootstraps' cron fence (shared
  with mail) keeps `messages` out of `platform_toolsets.cron`, and `make
  doctor` flags any cron job that can read messages alongside other tools.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `MESSAGES_MCP_ENABLED` | `0` | Register the messages MCP and the webhook route |
| `MESSAGES_INDEX_DIR` / `MESSAGES_INDEX_FILE` | `./data/messages` / `index.sqlite` | Where the app writes the index; mounted read-only |
| `MESSAGES_EVENTS_DIR` | `./data/messages-events` | The watcher's outbox; set the same folder as `notify.outbox` |
| `MESSAGES_WEBHOOK_SECRET` | generated by setup | Shared by the forwarder and the webhook route. Empty turns alerts off |
| `MESSAGES_WEBHOOK_PORT` | `8646` | The default profile's webhook listener inside the hermes container |

On Linux, `HERMES_UID`/`HERMES_GID` must own both folders. Messages is
macOS-only, so in practice this stack runs on a Mac.

### First real run

Everything above has run only against the synthetic store, a throwaway
Hermes config and the container in isolation. The first real run checks the
rest. Report counts, not messages:

1. `make messages-index-doctor` from a terminal with Full Disk Access, or
   the app's log after you grant it. `dates_look_like_nanoseconds` should be
   `true` and `attributed_body_failed` should be `0` or close to it.
2. `launchctl procinfo $(pgrep -f assistant-messages-reader | head -1)`
   should name the app as the responsible process.
3. `make doctor`: the Messages section should be all green.
4. Have someone text you in a watched chat. Within seconds the triage job
   should run, and its report should reach Signal. If nothing arrives,
   check for event files waiting in `data/messages-events`
   (doctor warns about old ones) and `docker compose logs messages-mcp`
   (counts only).

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

The watcher signs its POSTs the way that gateway's generic V2 check expects
(`X-Webhook-Signature-V2` over `"<timestamp>.<body>"`, `X-Request-ID` for
duplicates, `type: "messages.new"` for route filters). The gateway listens
inside the `hermes` container, and its port isn't published to the host,
where the watcher runs. Two ways through were considered:

- **A loopback-only published port** for the webhook route, like the
  Vikunja loopback proxy. The watcher POSTs to `http://127.0.0.1:<port>`.
- **The watcher's outbox folder** plus a small forwarder in the messages MCP
  container that signs each file and POSTs it to `http://hermes:<port>`
  in-network.

The stack uses the outbox. It publishes nothing new, keeps the secret inside
the compose network, keeps events on disk while Hermes is down, and matches
the mail drafts queue. It costs about a second of delay. The watcher's
direct HTTP output stays available for people using `imessage/` with their
own receiver.

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

- **A small signed app holds the Full Disk Access grant,** packaged the way
  the [mail apps](email.md#macos-apps) are: a compiled launcher plus the
  helper's own Python scripts, sealed by the app's code signature and started
  by launchd. Not `node`, `python3`, a terminal or the container runtime:
  granting an interpreter access hands it to every script that interpreter
  runs. The first plan was a Swift binary. The mail apps showed that a
  sealed bundle gives the same narrow grant while keeping the helper in
  stdlib Python, testable against the synthetic store, and in one language
  with the rest of `imessage/`.
- **Watcher: port openclaw/imsg's `MessageWatcher` design** (MIT, with
  attribution) to Python's `select.kqueue`: kqueue on `chat.db`, `-wal`,
  `-shm` and the folder; a 250 ms debounce; a 5 s fallback poll; re-attach
  when a file's inode changes; a persisted `ROWID` cursor; retry rows that
  have no chat yet. None of imsg's send or AppleScript code comes along.
- **Edits and unsends: add BlueBubbles' second pass.** On each wake,
  re-query a recent window (by the indexed `date`) and compare
  `date_edited`, `date_retracted`, `date_read` and `date_delivered` against
  the index.
- **Decoder: our own typedstream reader** (`imessage/lib/typedstream.py`).
  The text always comes first in the archive, so it returns the text even
  when an attribute uses a class it doesn't parse. Its writer matches
  Foundation byte for byte, and the tests decode every synthetic body with
  Foundation's `NSUnarchiver` as a cross-check. `doctor` counts failures.
- **Read-only and careful with WAL.** Open with `mode=ro` and a 5 s busy
  timeout. Run each wake in a fresh read transaction, never hold one open,
  and never checkpoint. When there's no `-wal` or `-shm` file (Messages
  closed), open as immutable, because a read-only connection would otherwise
  create those files in the Messages folder. A reader that checkpoints becomes a second writer
  (see the 2026 SQLite WAL-reset bug, fixed in 3.51.3). Probe columns with
  `PRAGMA table_info` and stop on a layout it doesn't recognise, as the mail
  indexer does.
- **Scope is explicit, with no defaults.** The store path, the chats (chat
  ids or patterns such as `"*;-;*"` for every 1:1 chat), the `since` date and
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
- **Its own app, not the mail reader app.** The first plan was to share one
  helper with mail's store mode. Mail has since shipped its own sealed app
  (`Assistant Mail Reader`), which asks for Full Disk Access only in store
  mode. Messages gets a separate app with the same launcher pattern, so:
  - it can move to its own repository without taking mail code along
  - each app contains only its own scripts
  - someone who reads mail through scripting never grants Full Disk Access
    to anything mail-related

  Two apps holding Full Disk Access reach no more than one would. What keeps
  either safe is that its contents are small, sealed and reviewed.
- **No script hash check needed.** The mail apps answered the "anything that
  can edit the repo gets Full Disk Access" worry: the scripts live inside the
  signed bundle, so editing the repository can't change what a granted app
  runs.
- **Watch Apple's planned changes.** Apple said on 2026-10-02 that it will make
  Full Disk Access harder to grant. If a future macOS stops granting it to a
  self-signed helper, this design needs a Developer ID-signed, notarised
  build.

TCC details to confirm on the first real run, for both projects (the mail
apps rely on the same assumptions, and their store mode hasn't been tried on
real data either):

- An app started by launchd is the responsible process, so Full Disk Access
  granted to the app covers the Python child it starts. Confirm with
  `launchctl procinfo <pid>`.
- With ad hoc signing every rebuild asks again. A stable signing identity
  keeps the grant across rebuilds.

### Phases

1. **Synthetic store** (done). A fake `chat.db` under `synthetic-messages/`
   with `attributedBody` blobs, long messages, group chats, reactions, edits,
   unsends, attachments, the join race, a prompt-injection message, and a
   fake contacts database. See [imessage/README.md](../imessage/README.md#synthetic-store).
2. **Helper: index** (done). Scoped, read-only, incremental indexing with
   `doctor` and `chats`, tested against the synthetic store. See
   [imessage/README.md](../imessage/README.md#indexer).
3. **Helper: watch, events and app** (done). Watcher, edit pass, signed
   webhook and outbox events, and the sealed app with its LaunchAgent,
   tested by appending rows to the synthetic store. See
   [imessage/README.md](../imessage/README.md#watcher).
4. **MCP and Hermes wiring** (done). `messages-mcp` with its forwarder, the
   `messages` profile, registration on the default and browser profiles, the
   webhook route and the triage job. See [How it fits together](#how-it-fits-together).
5. **First real run** (the owner runs it). See [First real run](#first-real-run).

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
messages-send MCP (container) ──▶ queue folder ──▶ messages-send app (host, Automation only)
   │                                                     │ osascript: send text to chat id
   │ approval code (out of band, e.g. Signal)            ▼
   └──────────────── you ─────────────────────────  Messages.app
                                                         │ new outgoing row in chat.db
                              messages helper (watcher) ◀┘ confirms delivery or failure
```

- **`messages-send` host app.** Sealed and started by launchd like the mail
  apps, with Automation permission only. Its one job is to send this text to
  this chat id. It refuses a chat that isn't on the allowlist unless the
  request carries a valid approval code. Containers can't run `osascript`,
  so it has to run on the host.
- **A queue folder, not a network port,** as [mail drafts](email.md#drafts)
  does it. The MCP writes each request to a shared folder. The host app
  checks it again before acting, writes a status file without the text, and
  deletes the request. The container mounts only that folder, so there's no
  listener to secure and no token to manage.
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
