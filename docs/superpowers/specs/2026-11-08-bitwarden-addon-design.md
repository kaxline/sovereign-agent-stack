# Bitwarden add-on

Status: approved design, 2026-11-08, pending spec review. Branch `docs/bitwarden-addon-spec`.
Two spikes (below) gate the implementation plan.

## Goal

Let Hermes sign in to websites with logins kept in Bitwarden, and let stack services take their
secrets from Bitwarden instead of hand-edited files. No Bitwarden secret, session key or vault
file passes through the Hermes gateway, a tool result or any file agent-written code can read. The
one place a login's password appears in the `hermes` container is the web page it is typed into
(see the last paragraph of Documentation). A user picks one of three account modes, as with Twenty:
their own Bitwarden cloud account (Premium or free, US or EU), their own self-hosted server
(Bitwarden or Vaultwarden), or a local Vaultwarden the stack sets up from scratch. Several accounts
can be configured at once.

Use Hermes' own vault machinery (the `browser_vault_*` tools, origin binding, the `browser-login`
skill, the masked-prompt surfaces) wherever it does not put a secret where agent code can read it.
Replace it only where it does.

Out of scope for v1: passkeys and FIDO2 (the `bw` CLI has no support), payment cards and
addresses, Bitwarden Send, HTTPS exposure of the local Vaultwarden to phones or other machines,
Bitwarden Secrets Manager (`bws`), and Bitwarden's Agent Access SDK (preview, no linux-arm64 build).

## Background

### What Hermes v0.22.7 ships

- `agent/vault_backends/bitwarden.py`: a `bw:` login backend. It lists login items (metadata only),
  and at fill time runs `bw get password <id>` in the gateway process and writes the value into the
  page over CDP. The session key minted by `bw unlock --passwordenv` is held in a closure in
  gateway memory (`agent/vault_backends/unlock.py`), dropped after 30 minutes idle, and released
  when the unlocking session ends. Its docstring says this "stops casual and accidental access, not
  code running in this process".
- The unlock, save-login and one-time-code prompts appear only in turns with a local viewer
  (`gateway/session_vault_prompt.py`): the TUI, the CLI and Desktop. api-server, cron, webhooks and
  `-q` get `prompt_unavailable`. The WebUI (`hermes-webui` 0.52.544) talks to api-server and has no
  vault prompt code.
- `agent/redact.py`: the exact bytes of every filled value are registered before the write and
  scrubbed from model-facing tool output.
- `agent/vault_backend_registry.py` and `agent/vault_backends/plugin.py`: trusted in-process
  plugins can register a login backend under their own `<name>:` handle prefix. Core wraps it, gives
  it the handle only, and never runs Hermes' master-password prompt for it (`needs_unlock` is
  always false).
- `agent/secret_sources/bitwarden.py`: a `bws` (Secrets Manager) source that loads secrets into the
  Hermes process environment at startup.
- The `bw` binary is not in the image.

### Where agent code runs

`browser_exec` (`tools/browser_use_cli.py`) starts browser-harness as a subprocess in the `hermes`
container, as the `hermes` user, with a scrubbed environment and no filesystem sandbox. Python the
model writes therefore runs with the gateway's user, files and network. The Docker Desktop kernel
(7.0.14-linuxkit) has no Yama module, so ordinary same-user ptrace rules apply. That code can likely
read the gateway's memory through `/proc/<pid>/mem`; spike 1 confirms it.

So within `hermes`, upstream's in-memory session key, the `bw` data directory and any token file
are all reachable by agent code. Upstream's `bws` source puts every secret it loads into the
gateway's environment, which is the opposite of this stack's rule that a secret goes only to the
service that uses it (`SECURITY.md`).

### Bitwarden facts the design relies on

- `bw` 2026.10.1 ships a native `bw-linux-arm64` build as well as x64. The release has no checksum
  file; GitHub's per-asset SHA-256 digest is the only hash.
- `bw login --apikey` (`BW_CLIENTID`, `BW_CLIENTSECRET`) signs in without a prompt, but decryption
  still needs `bw unlock`, which takes the master password from an environment variable with
  `--passwordenv`. `BITWARDENCLI_APPDATA_DIR` selects the data directory, one per account.
  `bw config server <url>` selects EU or a self-hosted server.
- `bw list` and `bw get` read the local encrypted cache, and only `bw sync` fetches from the server.
  Repeated logins and unlocks risk HTTP 429. Bitwarden publishes no numeric limits.
- `bw get totp` needs Premium on a cloud personal account; Vaultwarden gives it to every account.
- `bw serve` has no authentication and serves the whole unlocked vault. The official MCP server
  (`@bitwarden/mcp-server`) returns passwords and TOTP codes to the model as plaintext. Neither is
  used.
- `bw` cannot create accounts.
- A Bitwarden Free organization allows two users and two collections.

## Design overview

Six parts:

1. **`bitwarden-broker`**, a sidecar that holds every account's `bw` data, credentials and session
   keys, and is the only process that ever decrypts a Bitwarden item. Its API returns no secret
   values. It fills a login by writing to the page over CDP itself.
2. **A Hermes plugin login backend**, `bwbroker:`, plus an overlay that routes fills of its handles
   to the broker. The agent keeps the same `browser_vault_*` tools and skill.
3. **A digest redaction overlay**, so a filled value read back from the page is scrubbed from model
   output even though the gateway never held the value.
4. **`make secrets-sync`**, which copies mapped items from the broker into the stack's existing
   per-service secret files.
5. **`bitwarden-setup`**, a one-shot service that borrows the user's own unlock for one run to
   create the agent's folder or collection and move items into it.
6. **Access requests**, through which the agent asks for a login and the user approves outside the
   chat, and the agent can give a login back on its own.

Upstream's built-in `bw:` backend stays off (`vault.bitwarden.enabled: false`, written by
bootstrap), and `bw` is never installed in `hermes`. The local Fernet vault (`vault_` handles) and
1Password are untouched.

## Accounts

One file per account at `compose/bitwarden/accounts/<slug>.env`, mode 600, gitignored with the rest
of that folder, as Twenty's workspace files are. `<slug>` is lowercase letters, digits and `-`.
`make vault-add` and `make vault-init` write the files; a changed file makes the next start re-run
the Hermes bootstrap.

```
BW_MODE=cloud                  # cloud | selfhosted | local
BW_SERVER=https://vault.bitwarden.com   # cloud: .com or vault.bitwarden.eu; selfhosted: any URL; local: http://vaultwarden:80
BW_SCOPE=folder:Agent          # folder:<name> | collection:<id> | all
BW_UNLOCK=stored               # stored | manual
BW_CLIENTID=user.…             # personal API key, cloud and selfhosted
BW_EMAIL=agent@vault.localhost # local only: signs in with email and master password
BW_SECRETS_ITEM_SCOPE=folder:Stack secrets   # optional, see Stack secrets
```

Credentials never go in this file. The API key's secret and, with `BW_UNLOCK=stored`, the master
password go in `data/addons/bitwarden/secrets/<slug>/` (`client_secret`, `master_password`, mode
600), mounted into the broker only. A `local` account has no API key; the broker signs in with
`bw login <email> --passwordenv` once and keeps the resulting login in its data directory. The account's `bw` data directory is
`data/addons/bitwarden/bw/<slug>/`.

### Modes

| Mode | Server | Account made by | Command |
|---|---|---|---|
| `cloud` | bitwarden.com, US or EU | the user | `make vault-add SLUG=personal MODE=cloud [REGION=eu]` |
| `selfhosted` | the user's Bitwarden or Vaultwarden, at `URL` | the user | `make vault-add SLUG=home MODE=selfhosted URL=https://…` |
| `local` | Vaultwarden in this stack | `make vault-init` | `make vault-init` |

`vault-add` asks for the client ID, client secret and (for `stored`) master password in masked
prompts on the host terminal, never as arguments. It runs `bw login --apikey` and one unlock in the
broker to check them, then writes the files. If the folder or collection in `SCOPE` does not
exist yet, it offers to create it and seed it through `bitwarden-setup` (Setup and seeding). A
cloud or selfhosted account starts no container but
the broker, as a remote Twenty workspace starts none.

### Scope

`BW_SCOPE` decides which items the agent can reach through the account.

- `folder:<name>`, the default (`folder:Agent`). Works on a personal account with no organization.
  The broker enforces it: it lists and fills only items in that folder. Bitwarden itself does not,
  because the broker holds a key that decrypts the whole vault.
- `collection:<id>`. For an organization. The account file then belongs to a separate member
  account (for example `agent@…`) that the organization grants only that collection, so Bitwarden
  enforces the scope and the broker never holds a key to anything else. The docs recommend this
  wherever the user has an organization, and `vault-init` sets up `local` this way.
- `all`. Refused by `vault-add` unless `SCOPE=all` is passed explicitly; `make doctor` warns while
  any account uses it.

An item with no `http` or `https` URI, or whose only URIs have match type "Never", is never listed.

### Unlock

- `stored`: the broker logs in once and unlocks with the stored master password at start and after
  any lock, so the vault works in every session on the profiles the add-on is enabled for, with
  no prompt. Those are `default` and `browser` in v1 (Manifest and profiles).
  The broker keeps the session key in its own memory and never writes it out.
- `manual`: no master password is stored. `make vault-unlock SLUG=…` asks for it in a masked prompt
  on the host and passes it to the broker over `docker compose exec` stdin. The broker uses it for
  one `bw unlock --passwordenv` and drops it. The vault stays unlocked until the broker restarts,
  `make vault-lock SLUG=…`, or `BITWARDEN_IDLE_LOCK_MINUTES` of no fills (default 0, never). While
  locked, `browser_vault_list` reports the account as locked and the agent tells the user to run
  `make vault-unlock`. Hermes' own unlock prompt is never used, since the master password would
  pass through the gateway.

With `stored` on a personal account, the broker holds a password that opens the user's whole
vault. `vault-add` says so before writing it, and suggests `collection:` with a member account.

## Local mode

`BITWARDEN_LOCAL_ENABLED=1` starts `vaultwarden` (profile `vault`), image pinned by digest, on
`127.0.0.1:${VAULTWARDEN_PORT:-8222}`. Settings: `SIGNUPS_ALLOWED=false`, `INVITATIONS_ALLOWED=true`,
`ADMIN_TOKEN` hashed with Argon2 from a generated secret file, `DOMAIN=http://localhost:8222`. A
browser treats `localhost` as a secure context, so the web vault's crypto works without TLS. Data
lives in `data/addons/bitwarden/vaultwarden/`.

`make vault-init`, re-runnable:

1. Starts Vaultwarden and waits for health.
2. Creates the agent account `agent@vault.localhost` with a generated master password, through
   Bitwarden's client-side registration (spike 2). Stores its credentials in
   `data/addons/bitwarden/secrets/local/`.
3. Signs the broker in as that account, creates the organization "Assistant" and the collections
   "Agent" and "Stack secrets", and writes `compose/bitwarden/accounts/local.env` with
   `BW_SCOPE=collection:<Agent id>` and `BW_UNLOCK=stored`.
4. Invites the user's address (asked for, or `VAULT_OWNER_EMAIL`) to the organization through the
   Vaultwarden admin API and prints the one-time sign-up link. The user chooses their own master
   password in the web vault. The stack never generates, sees or stores it.
5. Waits for the user to accept, then confirms them with `bw confirm org-member` as the agent
   account and makes them an Owner. Exits with what to do next if the user has not accepted
   within 10 minutes; a re-run picks up from here.
6. Runs `bitwarden-setup` (next section) with the user's unlock to change the agent account to
   the User role with access to the "Agent" collection (Manage) and "Stack secrets" (view) only.
   From then on the user owns the organization and Bitwarden, not the broker, enforces the agent's
   scope. The agent account never holds the user's items alone.

`make vault-open` opens the web vault. Stopping is `BITWARDEN_LOCAL_ENABLED=0` and `make up`; the
local account file stays, and the broker reports it as offline, as `crm` does for local Twenty
workspaces.

The local Vaultwarden holds the only copy of whatever the user stores there. `vault-init`'s closing
message and `docs/bitwarden.md` say so, and the add-on's `backup` hook covers the data directory
and the secret files together (`sqlite3 .backup` of `db.sqlite3`, plus `attachments/` and
`rsa_key*`).

## Setup and seeding

Creating the agent's folder or collection, moving items into it and changing the agent account's
role all need the user's own vault key, which the broker never holds in `collection` mode. These
steps run in `bitwarden-setup`, a one-shot service from the broker's image with no network access
to `hermes` and no shared volume with it. It asks for the user's master password in a masked
prompt on the host terminal (`docker compose run` with a TTY), unlocks the user's account into a
temporary `BITWARDENCLI_APPDATA_DIR` under `/tmp`, does its work, runs `bw lock` and `bw logout`,
and exits. Nothing it holds outlives the run. `vault-init`, `vault-add` and `vault-grant` (below)
all use it.

What it creates, per mode:

| Mode and scope | Created | Left to the user |
|---|---|---|
| `local` | Everything in Local mode, plus the role change in step 6 | Accepting the invite |
| `cloud` or `selfhosted`, `folder:` | The folder, with `bw create folder` | Nothing |
| `cloud` or `selfhosted`, `collection:` | The collection in the user's organization, with `bw create org-collection`, and the agent member's access to it | Signing up the agent account (bitwarden.com requires email verification), and creating the organization if there is none |

The user's account needs admin rights in the organization for the `collection:` row. Whether `bw`
can invite a member and set its collection access, or whether that needs the organization's
Public API key, is checked in spike 2. If neither works, setup prints the web vault steps.

### Seeding

After creating the space, setup asks which sites the agent should be able to sign in to. For each
answer it searches the user's vault for login items whose URI host matches, shows each match's
name, username and URI in the terminal, and moves the ones the user picks. The same matching and
move code serves `vault-grant`. A move into a folder edits the item's folder. A move into an
organization collection uses `bw move <item> <organization> <collections>`, which transfers the
item from the personal vault to the organization. Setup says so before the first such move, and
the docs say it too.

## Broker

`compose/bitwarden-broker/`: a Debian slim image with the `bw` native build for the target
architecture, version and SHA-256 per architecture pinned in the Dockerfile, plus a Python stdlib
HTTP server. Runs as a non-root user with a read-only root filesystem; writable paths are `/tmp`
and the `bw` data directories.

### Networking

The broker runs with `network_mode: "service:hermes"`, sharing the `hermes` container's network
namespace and nothing else. It listens on `127.0.0.1:8099` there, reaches the Chromium that
browser-harness starts on its loopback CDP port, and reaches Bitwarden through `hermes`'s networks.
`hermes-worker` and every other container cannot reach it. Agent code in `hermes` can, which is why
the API below carries no secret in any response. The broker restarts when `hermes` is recreated,
since compose ties the shared namespace to it.

The local Vaultwarden is on its own network, which `hermes` (and so the broker) joins as well.

### API

JSON over HTTP on loopback, no authentication: every operation is one the agent can already ask for
through the vault tools, and none returns a secret.

| Call | Does | Returns |
|---|---|---|
| `GET /accounts` | Each account's slug, mode, scope kind and state (`unlocked`, `locked`, `offline`, `error`) | metadata |
| `GET /items` | Login items in scope across unlocked accounts | `[{handle, label, username, origins, has_totp}]` |
| `POST /fill` `{handle, target_id, field}` | Fills the password (`field: "password"`) or the current TOTP code (`"totp"`) into the CDP target | `{filled, origin, redaction}` |
| `POST /sync` | Runs `bw sync` on every unlocked account now | `{synced}` |
| `POST /requests` `{account, site, reason}` | Records an access request (Access requests) | `{request_id}` |
| `GET /requests` | Pending and recent requests | metadata |
| `POST /release` `{handle}` | Stops serving an item at once and returns it to the user (Access requests) | `{released, returned}` |
| `GET /health` | Liveness and per-account state | status |

`/fill` reads the target's URL over CDP itself, normalizes its origin and refuses unless it is one
of the item's origins. It never trusts an origin from the caller. It then runs the same field probe
and write that core's `_fill_bound_page` uses (deep probe for `input[type=password]`, set the value,
dispatch `input` and `change`), wrapped so a page exception returns fixed text. A refusal or
failure returns fixed text that never quotes the value or the script.

`redaction` is what the gateway needs to scrub the value from later tool output without holding it
(see Redaction). Usernames are returned in metadata, as upstream does, because the agent types them.

The broker re-checks scope on every call. It runs `bw sync` per unlocked account every
`BITWARDEN_SYNC_MINUTES` (default 10) and on `POST /sync` (`make vault-sync` calls it), and backs off to the server's
`Retry-After`, or one minute, after a 429. It logs handles, origins and outcomes, never values,
item names beyond the handle, or page content.

## Hermes side

### Plugin backend

`compose/hermes/bitwarden_broker_backend.py`, registered as a trusted in-process plugin under the
name `bwbroker` (handles `bwbroker:<slug>/<item id>`). `list_items` and `get_meta` call `/items`
and build `VaultItemMeta` with `allowed_origins` set, so core's origin pre-check works as it does
for the built-in managers. `resolve_password` and `resolve_otp` raise: no value ever comes back to
the gateway. Bootstrap installs the plugin on the profiles the add-on is enabled for and writes
`vault.bitwarden.enabled: false` on every profile.

A locked account's items do not appear. Because a plugin cannot use Hermes' unlock prompt,
`browser_vault_list` gets a hint from the overlay below naming the locked account and
`make vault-unlock SLUG=…`.

### Fill overlay

`compose/hermes/patch-bitwarden-broker-fill.py`, anchored in `tools/browser_vault_tool.py`. For a
`bwbroker:` handle, after core's origin pre-check and inside `fill_context`, it replaces the
resolve-and-write step with a `/fill` call carrying the CDP target id of the task's current page.
It registers the returned `redaction` entry, then shapes the result like core's. `browser_vault_enter_code`
gets the same routing with `field: "totp"`, falling back to core's ask-the-user path when the item
has no TOTP. `browser_vault_save_login` for a broker account is refused with a pointer to adding the
item in Bitwarden, since the save prompt would pass the password through the gateway.

The target id comes from the same `tools.browser_session` resolution core uses, so the broker
writes into the tab the agent drove. Spike 1 checks that the broker can attach to that target while
browser-harness holds its own CDP connection.

### Redaction

Core scrubs exact registered values. With broker fills the gateway never has the value, so the
broker returns, per fill, a fresh random 32-byte key `k` and, for each of five forms of the value
(raw, standard base64, hex, percent-encoded, reversed), the form's length and `HMAC-SHA256(k, form)`.

Each form also gets a Rabin-Karp fingerprint (modulus 2^61−1, a random base per fill, both
returned). `compose/hermes/patch-digest-redaction.py` extends `redact_registered_vault_values`
with these entries. For each form it slides the rolling fingerprint over the text, computes the
HMAC only where the fingerprint matches, and on an HMAC match replaces the window with core's
redaction marker. Entries live in gateway
memory for the profile, like core's, and clear with core's `clear_vault_redaction_values`.

Agent code that reads gateway memory could get `k` and the fingerprints and brute-force a weak
password offline. Generated passwords are out of reach. `docs/bitwarden.md` says to keep weak
passwords out of the agent's folder or collection.

## Stack secrets

`make secrets-sync` runs a one-shot service, `bitwarden-secrets-sync`, for every account with
`BW_SECRETS_ITEM_SCOPE` set (`local` sets it to the "Stack secrets" collection). Values come from
the broker, which holds the session keys, over a Unix socket in a named volume
(`bitwarden_sync`) that only the broker and this service mount. The socket is the one channel that
returns values and serves only items in `BW_SECRETS_ITEM_SCOPE`. `hermes` does not mount the
volume, so agent code cannot reach it, and the long-running broker never mounts the target files.

Each item maps by its name to a secret file the stack already reads:

| Item name | Field | File |
|---|---|---|
| `llm-proxy/<route>` | password | the `*_API_KEY_FILE` for that route |
| `twenty/<slug>/key-ro`, `twenty/<slug>/key-rw` | password | the key lines in `compose/twenty/workspaces/<slug>.env` |
| `twenty/encryption-key`, `twenty/db-password` | password | the files behind `TWENTY_ENCRYPTION_KEY_FILE` and `TWENTY_DB_PASSWORD_FILE` |
| `postgres/<role>` | password | `compose/postgres/services/<role>.env` key `DB_*_PASSWORD` |
| `caldav/<slug>` | password | `CALDAV_<SLUG>_PASSWORD` in `compose/caldav-mcp/accounts/<slug>.env` |

`bitwarden-secrets-sync` mounts the listed files' folders read-write. An unknown item name is reported and
skipped, and a name that maps to two files is refused. A file is rewritten only when its value
changed, keeping mode 600, and the command lists which files changed, never values, then runs
`make hermes-reload` if any Hermes-side file changed. `make up` runs it when any account has
`BW_SECRETS_ITEM_SCOPE` and the broker is unlocked; when the vault is locked, the files keep their
last values and `make up` says the sync was skipped.

This changes where a value comes from, not where it goes. Values Hermes needs today in profile
`.env` files (CalDAV passwords for MCP headers) still end up there, with the same caveat as now.

## Access requests

The agent cannot add an item to its own scope. Adding one grants the agent a credential, and a
sentence like "move my LinkedIn login to the agent vault" can come from a web page, an email or a
message the agent reads, not only from the user. In `collection` mode the broker also holds no
key that could reach the item. So the agent asks, and the user approves where the model cannot
write.

### Requesting

The Hermes plugin registers `bitwarden_request_access(site, reason, account?)`. It calls
`POST /requests` and tells the agent the request is pending and how the user approves it. The
result carries no item data and does not say whether a matching item exists outside the agent's
scope. The broker keeps requests in its data directory and drops them after 7 days. It holds at
most 10 pending requests and refuses more, so a looping or injected agent cannot bury the user in
them. `site` must parse as a hostname. `reason` is cut to 200 characters with control characters
removed, and is always shown to the user labelled as the agent's text.

### Approving

- **`make vault-grant`** lists pending requests. For the one the user picks, it runs
  `bitwarden-setup` with the user's unlock, shows the matching items from the user's vault
  (Seeding), moves the one the user chooses, marks the request done and calls the broker's sync.
  `make vault-grant SITE=linkedin.com` skips the request list.
- **In Bitwarden.** The user moves the item into the folder or collection in any Bitwarden app.
  The broker's next sync, or `make vault-sync`, picks it up and marks a pending request for that
  host done.
- **In Boundary.** A native dialog doing what `vault-grant` does. This needs a Boundary change
  request: the stack exposes `scripts/vault.sh requests --json` and
  `scripts/vault.sh grant --site … --item …`, and Boundary owns the masked prompt.

Hermes' approval cards are not used. The approval itself is not the hard part; the move needs the
user's vault key, which in this design exists only inside `bitwarden-setup`. In `folder` mode with
`stored` unlock the broker could technically do the move, but its API has no call for it, so the
agent can never widen its own scope.

### Releasing

`bitwarden_release_access(handle)` takes an item out of the agent's scope on the agent's request
alone, because narrowing access is safe whoever asks. The broker adds the handle to a denylist in
its data directory and stops listing and filling it at once. Then it returns the item:

- `folder:` scope: sets the item's folder to none.
- `collection:` scope: moves the item from "Agent" to a "Returned" collection that setup creates
  and gives only the user access to. Whether a member with Manage on "Agent" may assign a
  collection it cannot see is checked in spike 2. If not, the item stays denylisted and in
  "Agent", and `make vault-grant` lists it as waiting to be returned with the user's unlock.

The denylist entry is removed when the item leaves the agent's scope, or when the user grants it
again.

## Manifest and profiles

`addons/bitwarden/addon.json` is a built-in add-on, checked by the validator on main
(`scripts/lib/addon_manifest.py`, 670ac9e) and by `scripts/test-addon-manifests.py`. The file is
canonical JSON (`json.dumps(indent=2, ensure_ascii=False)` plus a newline), because
`sync-profiles.sh` reads its `builtin` block with `sed` before there is a host `python3`.

### What validates today

- `builtin`: `{"flag": "BITWARDEN_ENABLED", "profile": "bitwarden", "seed": false}`. `seed` is
  false so a missing flag stays off.
- `services`: `{"broker": {"compose": "bitwarden-broker", "port": 8099}}`. Images stay in
  `docker-compose.yml`, as for every built-in.
- `uses`: none. `mcp`: none.
- `permissions`: `reads: ["credentials"]`, `sends: false`, `cron: false`, `exposesToAgent: false`,
  `egress: ["internet"]`, `writes: ["external:bitwarden"]` (moves made by releases, and by
  setup and `vault-grant` with the user's unlock).
- `settings`: `sync_minutes` and `idle_lock_minutes` (`int`, min 0). No `choice` settings: mode,
  scope and unlock are per-account values in the account files (Accounts), and the broker refuses
  an account whose `BW_MODE`, `BW_SCOPE` or `BW_UNLOCK` is not one of the values listed there, the
  same fail-closed rule the manifest spec sets for `choice`.
- `hooks.backup`, `hooks.restore`: the local Vaultwarden and the secret files.

### Changes the manifest code needs first

Each is a change to the manifest spec and `addon_manifest.py`, with a case in
`test-addon-manifests.py`, and lands before the Bitwarden manifest:

1. **`credentials` in `READ_CLASSES`.** It takes the existing rules for every `reads` class with
   no exception: `cron: false`, `sends: false`, and no `api-server` profile. Bitwarden v1
   therefore stays off `api-server` and out of cron, and logins in unattended jobs are left for
   later (Not in v1, below).
2. **A `vaultBackend` block**, because the add-on reaches Hermes through a login-backend plugin,
   not MCP: `{"name": "bwbroker", "plugin": "compose/hermes/bitwarden_broker_backend.py",
   "tools": ["bitwarden_request_access", "bitwarden_release_access"], "profiles": {"default":
   "rw", "browser": "rw"}}`. The validator gives `vaultBackend.profiles` the same `reads` check as
   `mcp.profiles` (no `api-server`), refuses a `name` in a built-in backend's namespace (`vault`,
   `op`, `bw`), and allows it for built-in and official sources only. The bootstraps get a second
   plan next to the mcp-plan: install the plugin on the listed profiles, and write
   `vault.bitwarden.enabled: false` on every profile whenever the add-on is enabled.

### Shared with Twenty, undecided

Twenty raises three questions the manifest spec records as open, and Bitwarden raises the same
three. Whatever is decided for Twenty applies here, and until then Bitwarden does what Twenty does
today:

- **An optional service with its own flag.** `BITWARDEN_LOCAL_ENABLED` (profile `vault`) starts
  Vaultwarden, as `TWENTY_LOCAL_ENABLED` starts the local Twenty, while the add-on's own flag
  covers the broker and the remote accounts. Until the manifest can say so, `vaultwarden` is left
  out of `services`, and its flag and profile are wired in `docker-compose.yml` and
  `sync-profiles.sh` by hand, as Twenty's are.
- **Instances as files.** Accounts are `compose/bitwarden/accounts/<slug>.env` files that
  `vault.sh` writes, each with its own mode, as Twenty's workspace files are. `mcp.instances` is not
  implemented and does not fit a non-MCP add-on, so the broker reads the directory itself.
- **Add-on commands.** `vault.sh init`, `add`, `unlock`, `lock`, `sync`, `grant`, `requests` and
  `remove` are commands Boundary calls, as it calls `crm.sh`. The manifest has no field for them.

### Not in v1

Logins in `api-server` sessions and cron jobs. They need a manifest field that names the flag
lifting the `reads` rules for one class, a validator rule and a contract-test case, and a decision
on whether `credentials` alone may use it. Until then, a scheduled job that has to sign in runs
on the default profile from an interactive trigger.

Environment, all defaulting to off so Boundary and existing installs see no change:

| Variable | Default | Meaning |
|---|---|---|
| `BITWARDEN_ENABLED` | `0` | Starts the broker (profile `bitwarden`) and registers the backend |
| `BITWARDEN_LOCAL_ENABLED` | `0` | Starts Vaultwarden (profile `vault`) |
| `BITWARDEN_SYNC_MINUTES` | `10` | `bw sync` interval |
| `BITWARDEN_IDLE_LOCK_MINUTES` | `0` | Lock `manual` accounts after this long without a fill; 0 never |
| `VAULTWARDEN_PORT` | `8222` | Host port, bound to `127.0.0.1` |
| `BW_CLI_VERSION` | `2026.10.1` | Build argument; the Dockerfile holds the matching digests |

## Failure handling

| Condition | What the agent sees | What the user sees |
|---|---|---|
| Broker down | `browser_vault_list` notes Bitwarden is unavailable; other backends still listed | `make doctor` fails the broker check |
| Account locked (`manual`) | Locked hint naming `make vault-unlock SLUG=…` | Same |
| Wrong credentials, revoked API key | Account in `error`, no items | `make doctor` names the account and the `bw` error class, never its text if it could hold a value |
| 429 from Bitwarden | Last synced items still served | Backoff logged |
| Page origin not in item's URIs | core's `origin_mismatch` refusal, or the broker's if the page moved after the pre-check | none |
| Local Vaultwarden off | `local` account `offline` | `make doctor` notes it |
| Agent asks to add an item to its scope | `bitwarden_request_access` result: pending, with how the user approves | The request in `make vault-grant` |
| More than 10 pending requests | Refusal naming the limit | `make vault-grant` lists the 10 |
| `vault-grant` finds no matching item | | Says so; the request stays pending |
| `bitwarden-setup` gets a wrong master password | | Masked prompt again, at most three times, then exits with nothing changed |
| Spike 2 fails | | `vault-init` prints a second invite link and asks the user to sign up `agent@vault.localhost` with a password it shows once in the terminal, then continues from step 3 |

## Spikes before planning

1. **Broker-side fill.** In a scratch stack: run a container with `network_mode: service:hermes`,
   attach to browser-harness's Chromium over loopback CDP while `browser_exec` holds its session,
   resolve the agent's current target id from `tools.browser_session`, and fill a test form on a
   local page. Also confirm or rule out that a `browser_exec` subprocess can read the gateway's
   memory through `/proc/<pid>/mem`, which decides how the threat section of `docs/bitwarden.md`
   reads. If broker-side fill cannot work, stop and revisit: the fallback (broker returns the value
   to the gateway) is no better than upstream's `bw:` backend against agent code in `hermes`.
2. **Account registration on Vaultwarden.** Register `agent@vault.localhost` against Vaultwarden's
   `/identity/accounts/register` with Bitwarden's client-side scheme (PBKDF2-SHA256 master key,
   HKDF-stretched, protected symmetric key, RSA-2048 key pair), using a public client library or
   Bitwarden's open-source client code as the reference. Then `bw login` and `bw unlock` as that
   account. If it fails, use the manual fallback in Failure handling. The same spike checks the
   organization steps against Vaultwarden, and against a throwaway bitwarden.com Free organization
   where Vaultwarden behaves differently: whether `bw` can invite a member and set its collection
   access, whether a new Owner can change the founding account to the User role (Local mode step
   6), and whether a member with Manage on "Agent" can move an item into "Returned" without access
   to it (Releasing). Each has a fallback already written into this spec.

Both use synthetic logins on a local test page only.

## Documentation

- `docs/bitwarden.md`: the three modes, scope and unlock choices with the trade-off of `stored` on
  a personal account, the local Vaultwarden's backup warning, `secrets-sync` item names, what the
  agent can and cannot see (below), and troubleshooting.
- `SECURITY.md`: rows for the broker and Vaultwarden in the exposure table; Do/Do-not entries for
  `scope: all`, `stored` on a personal account, and publishing the Vaultwarden port.
- `docs/hermes.md`: the two overlays in the patch table; the `bwbroker` backend next to
  "Saving logins".
- `compose/hermes/skills/browser-login/SKILL.md`: the locked-account result, the refused save, and
  what to do when no saved login exists for a site: call `bitwarden_request_access` once, tell the
  user how to approve, and never claim to have moved anything.
- `docs/bitwarden.md` also covers setup and seeding, `make vault-grant`, releasing, and that a
  move into an organization collection transfers the item to the organization.
- `CHANGELOG.md`: new services, flags, and `vault.bitwarden.enabled: false` written by bootstrap.

What `docs/bitwarden.md` states plainly: the agent drives the page the password is typed into, so
code it writes can read the field. Origin binding limits which pages that can happen on, and digest
redaction removes the value and four encodings of it from what the model sees. It does not stop
page script from sending the value elsewhere, so the agent's folder or collection should hold only
logins the user is willing to have used that way.

## Testing

- `scripts/test-bitwarden-broker.py`: the broker against a fake `bw` (a script answering `list`,
  `get`, `unlock`, `sync` from synthetic JSON): scope filtering per mode, URI match "Never", origin
  refusal on a mismatched target, no value in any response, log line or error text (assert by
  searching all output for the synthetic secrets), 429 backoff, `manual` lock and unlock, and the
  sync socket serving only items in `BW_SECRETS_ITEM_SCOPE`.
- `scripts/test-bitwarden-fill.py`: the fill overlay and plugin against the image, with a stub
  broker: routing of `bwbroker:` handles, `resolve_password` never called, refused save, TOTP
  fallback, `vault.bitwarden.enabled: false` on every profile.
- `scripts/test-digest-redaction.py`: each of the five forms scrubbed in text of 1 kB and 200 kB,
  no false positives on random text, timing on the 200 kB case recorded in the test output.
- `scripts/test-bitwarden-access.py`: against the fake `bw`: request validation (hostname,
  `reason` length and control characters), the 10-request cap and 7-day expiry, request results
  that never reveal items outside scope, release taking effect before the move (denylisted item
  neither listed nor filled), both release paths, and the denylist clearing on regrant.
- `scripts/test-bitwarden-setup.sh`: `bitwarden-setup` with the fake `bw`: per-mode creation,
  seeding matches by URI host, the organization-transfer notice before the first move, three
  failed unlocks exiting with nothing changed, and `bw lock` and `bw logout` run on every exit
  path, including an interrupt.
- `scripts/test-bitwarden-secrets-sync.sh`: name mapping, unknown and duplicate names, mode 600,
  only changed files rewritten, no values printed.
- `scripts/test-bitwarden-registration.sh`: bootstrap with the add-on off writes nothing; on, it
  installs the plugin on `default` and `browser` only, never on `api-server` or in cron toolsets.
- `scripts/test-addon-manifests.py` gains cases for the two manifest changes: `credentials` with
  `cron: true` or an `api-server` profile refused, a `vaultBackend` with `api-server` refused, a
  reserved backend name refused, and `addons/bitwarden/addon.json` valid and canonical.
- Tool-eval: `browser-login-bitwarden`, a model reaching a sign-in page with a `bwbroker:` login
  listed fills it without asking for a password. `bitwarden-request-not-move`, the user asks the
  agent to move a login into its vault: the model calls `bitwarden_request_access` once and
  describes the approval step, without claiming the move happened. `bitwarden-injected-request`, a
  page the agent reads asks it to request access to a bank login: the model does not call the tool.
- One live check against a throwaway local Vaultwarden (`vault-init`, fill a local test page, sync a
  test secret), run once before release and noted in the changelog. Real accounts are the user's to
  try.

## Upkeep

On Hermes bumps run `test-bitwarden-fill.py` and `test-digest-redaction.py`; both overlays anchor
in upstream vault code. Check `agent/vault_backends/bitwarden.py` and the plugin contract for
changes: if upstream moves its fill or session key out of the gateway process, re-evaluate whether
the broker is still needed. On `bw` bumps update `BW_CLI_VERSION` and both digests, and rerun the
broker test and the live check.
