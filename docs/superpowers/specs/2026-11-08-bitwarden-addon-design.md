# Bitwarden add-on

Status: implemented on branch `feat/bitwarden-addon`. This text reflects the implementation as of
the branch head (2026-11-08), including the rulings recorded during implementation; where the code
departs from the first design, the text says what was built. Both spikes ran on 2026-11-08; see
Spike results. User documentation: `docs/bitwarden.md`.

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
(7.0.14-linuxkit) has no Yama module, so ordinary same-user ptrace rules apply, and Hermes never
marks its processes non-dumpable. Spike 1 confirmed that a `hermes`-user process with an empty
environment, not related to the target, reads another `hermes`-user process's memory through
`/proc/<pid>/mem` in the v0.22.7 image with Docker's default capabilities.

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
`make vault-add` and `make vault-init` write the files. The broker reads the folder only when it
starts, so `vault-add` and `vault-remove` restart it. Hermes needs no change per account: the
`bwbroker` backend lists whatever the broker serves.

```
BW_MODE=cloud                  # cloud | selfhosted | local
BW_SERVER=https://vault.bitwarden.com   # cloud: .com or vault.bitwarden.eu; selfhosted: any HTTPS URL; local: https://vaultwarden.vault.internal
BW_SCOPE=folder:Agent          # folder:<name> | collection:<id> | all; optional, default folder:Agent
BW_UNLOCK=stored               # stored | manual
BW_CLIENTID=user.…             # the agent account's personal API key, every mode
BW_SECRETS_ITEM_SCOPE=folder:Stack secrets   # optional, see Stack secrets
```

Credentials never go in this file. The API key's secret and, with `BW_UNLOCK=stored`, the master
password go in `secrets/<slug>/` (`client_secret`, `master_password`, mode 600) under
`BITWARDEN_HOST_DATA_DIR` (default `./data/addons/bitwarden`), mounted read-only into the broker and
read-write into `bitwarden-setup` only. `vault-init` fetches the `local` agent account's API key
when it creates the account, so every mode signs in the same way. The account's `bw` data directory
is `bw/<slug>/`.

The parser accepts CRLF line endings, spaces around keys and values, `#` comment lines, a comment
after an unquoted value when whitespace precedes the `#`, and one pair of quotes around a value. So
an unquoted `#` after a space ends the value; `vault-add` quotes a scope that holds `#`. An unknown
key or value, a repeated key or a missing required key refuses the file, with an error naming the
file and the key and never a value.

Layout under `BITWARDEN_HOST_DATA_DIR` (not moved by `ASSISTANT_DATA_ROOT`):

| Path | Holds | Mounted into |
|---|---|---|
| `bw/<slug>/` | the account's `bw` data directory | broker |
| `broker/` | `requests.json`, `denylist.json` | broker |
| `secrets/<slug>/` | `client_secret`, `master_password` | broker (read-only), `bitwarden-setup` |
| `secrets/local/tls/ca.crt` | the local CA's certificate, for `NODE_EXTRA_CA_CERTS` | broker (read-only), `bitwarden-setup` |
| `local/` | `admin_token`, `state` (vault-init's progress), `tls/ca.crt`, `tls/ca.key` | `bitwarden-setup` only |
| `local/tls/server/` | `server.crt`, `server.key` | `bitwarden-setup`, `vaultwarden-tls` (read-only) |
| `local/vaultwarden/` | `admin_token.phc`, the admin token's Argon2id hash | `bitwarden-setup`, `vaultwarden` (read-only) |
| `vaultwarden/` | Vaultwarden's data | `vaultwarden` |

`vault.sh` creates these folders before any container starts, so Docker never creates them
root-owned. On Linux the containers run as uid 10001: `vault.sh` gives the folders it creates to
that uid when run as root and prints the `chown` command otherwise, keeps `secrets/` and the
accounts folder owned by the user at mode 711, never chmods a path it does not own, and reads or
writes a container-owned file through `bitwarden-setup`, saying so once per path. `bitwarden-setup`
writing `local.env` or rewriting `BW_SCOPE` in the user-owned accounts folder is left to a
documented ownership step on Linux; the stack targets Docker Desktop on macOS, which maps bind-mount
ownership to the user.

### Modes

| Mode | Server | Account made by | Command |
|---|---|---|---|
| `cloud` | bitwarden.com, US or EU | the user | `make vault-add SLUG=personal MODE=cloud [REGION=eu]` |
| `selfhosted` | the user's Bitwarden or Vaultwarden, at `URL` | the user | `make vault-add SLUG=home MODE=selfhosted URL=https://…` |
| `local` | Vaultwarden in this stack | `make vault-init` | `make vault-init` |

`vault-add` asks for the client ID, client secret and (for `stored`) master password in masked
prompts on the host terminal, never as arguments. It writes the files, sets `BITWARDEN_ENABLED=1`,
and offers to create the folder or collection and seed it through `bitwarden-setup` (Setup and
seeding); the host cannot see whether the space exists, so it always offers. With the broker
running it then restarts the broker, waits for the account to leave `locked`, and on an `auth`
error removes what it wrote (with `--force`, it restores the previous file and secrets, which it
kept as `.bak` until then). Without a running broker the check happens at the next start. A cloud
or selfhosted account starts no container but the broker, as a remote Twenty workspace starts
none.

### Scope

`BW_SCOPE` decides which items the agent can reach through the account.

- `folder:<name>`, the default (`folder:Agent`). Works on a personal account with no organization.
  The broker enforces it: it lists and fills only items in that folder. Bitwarden itself does not,
  because the broker holds a key that decrypts the whole vault.
- `collection:<id>`. For an organization. The account file then belongs to a separate member
  account (for example `agent@…`) that the organization grants only that collection, so Bitwarden
  enforces the scope and the broker never holds a key to anything else. The docs recommend this
  wherever the user has an organization, and `vault-init` sets up `local` this way.
- `all`. Refused by `vault-add` unless `CONFIRM_SCOPE_ALL=1` is set as well (Ruling R25: `make
  vault-add SCOPE=all` exports `SCOPE`, so `SCOPE` itself cannot confirm); `make doctor` warns while
  any account uses it.

An item with no `http` or `https` URI, or whose only URIs have match type "Never", is never listed.

### Unlock

- `stored`: the broker logs in once and unlocks with the stored master password at start and after
  any lock, so the vault works in every session on the profiles the add-on is enabled for, with
  no prompt. Those are `default` and `browser` in v1 (Manifest and profiles).
  The broker keeps the session key in its own memory and never writes it out.
- `manual`: no master password is stored. `make vault-unlock SLUG=…` asks for it in a masked prompt
  on the host and pipes it to `python3 -m broker.unlock <slug>` through `docker compose exec` stdin.
  That command hands it to the running server over the Unix socket (`POST /unlock`; `--lock` sends
  `POST /lock`), never over TCP or argv. The broker uses it for one `bw unlock --passwordenv` and
  drops it; a wrong password leaves the account locked, not in error. The vault stays unlocked until
  the broker restarts, `make vault-lock SLUG=…`, or `BITWARDEN_IDLE_LOCK_MINUTES` with no fill or
  unlock (default 0, never). While locked, `browser_vault_list` carries
  `"bitwarden": {"locked": [<slug>], "unlock": "make vault-unlock SLUG=<slug>"}` and the agent tells
  the user to run it. Hermes' own unlock prompt is never used, since the master password would pass
  through the gateway.

With `stored` on a personal account, the broker holds a password that opens the user's whole
vault. `vault-add` says so before writing it, and suggests `collection:` with a member account.

## Local mode

`BITWARDEN_LOCAL_ENABLED=1` starts `vaultwarden` (profile `vault`), image pinned by digest, on
`127.0.0.1:${VAULTWARDEN_PORT:-8222}`. Settings: `SIGNUPS_ALLOWED=false`, `INVITATIONS_ALLOWED=true`,
`DISABLE_ICON_DOWNLOAD=true`, `DOMAIN=http://localhost:${VAULTWARDEN_PORT}`, and
`ADMIN_TOKEN_FILE` pointing at the Argon2id PHC hash in `local/vaultwarden/admin_token.phc`; the
plaintext token stays in `local/admin_token`, which only `bitwarden-setup` mounts. A browser treats
`localhost` as a secure context, so the web vault's crypto works without TLS. Data lives in
`vaultwarden/` under `BITWARDEN_HOST_DATA_DIR`.

`bw` refuses any server URL that is not HTTPS, loopback included; only its dev builds skip the
check (spike 2). The host port therefore stays plain HTTP for the user's browser, and a
`vaultwarden-tls` sidecar (`VAULTWARDEN_TLS_IMAGE`, default the `nginx` image the stack already pins
for `vikunja-proxy`) terminates TLS for the broker as `https://vaultwarden.vault.internal`.
`bitwarden-setup tls` (`setup/tls.py`, python3-cryptography in the broker image, so the host needs no
openssl) writes a private CA (EC P-256, 10 years, name-constrained to that one DNS name) to
`local/tls/` and a server pair (825 days) to `local/tls/server/`, and copies `ca.crt` to
`secrets/local/tls/ca.crt`. The sidecar mounts only `local/tls/server/`, the CA key stays where only
`bitwarden-setup` can read it, and the broker and `bitwarden-setup` trust the CA through
`NODE_EXTRA_CA_CERTS`; nothing else does.

Networks, so that nothing on `hermes`' networks reaches Vaultwarden in plain HTTP:

| Network | Internal | Members |
|---|---|---|
| `vault` | yes | `hermes` (and so the broker), `vaultwarden-tls` (alias `vaultwarden.vault.internal`), `bitwarden-setup` |
| `vault-backend` | yes | `vaultwarden`, `vaultwarden-tls` |
| `vault-host` | no | `vaultwarden` only, for the `127.0.0.1` port publish |
| `bitwarden-setup` | no | `bitwarden-setup`, its route to Bitwarden's servers apart from `hermes` |

`vaultwarden-tls` sets `X-Real-IP` and `X-Forwarded-For` from the connecting address, overwriting
what a client sends, so a caller in `hermes` cannot spoof its way past Vaultwarden's login rate
limit. `hermes` is on `vault` on every install, with the add-on off too: the broker shares its
namespace and needs the route, and an empty internal network exposes nothing. Existing installs
recreate `hermes` once on upgrade. The user's own browser never sees this certificate, so
the user installs no CA. Serving TLS from Vaultwarden itself (`ROCKET_TLS`) works for `bw` too, but
would make the user trust the CA in macOS or click through a warning to reach the web vault.

`make vault-init`, re-runnable. Steps 2 to 5 run in one `bitwarden-setup local-init` run, so the
agent account signs in once for the API and once for `bw`: Vaultwarden allows 10 password logins a
minute from one client IP and sends no `Retry-After`, so on a 429 the run waits 60 seconds and tries
once more. Each finished step is recorded with the ids it made in `local/state`, which only
`bitwarden-setup` mounts, and a re-run picks up after the last one. A crash between creating the
organization and recording it is handled by reusing an organization named "Assistant".

1. Creates the data folders, runs `bitwarden-setup tls` and `admin-token`, sets
   `BITWARDEN_LOCAL_ENABLED=1`, starts `vaultwarden` and `vaultwarden-tls` and waits for health.
2. Creates the agent account `agent@vault.localhost` with a generated master password: an admin
   API invite (`POST /admin/invite`), then Bitwarden's client-side registration through
   `/identity/accounts/register/send-verification-email` and `/register/finish`. With no SMTP
   configured, the first call returns the verification token directly. Fetches the account's
   personal API key (`POST /api/accounts/api-key`, with a token requested for scope
   `api offline_access`, since Vaultwarden 1.37.4 refuses `api` alone) and stores the key and
   master password in `secrets/local/`.
3. Creates the organization "Assistant" with the collection "Agent" over the API
   (`POST /api/organizations`; `bw` has no command for it), then signs `bw` in as the agent account
   inside `bitwarden-setup` and creates "Stack secrets" with `bw create org-collection`. Writes
   `compose/bitwarden/accounts/local.env` with `BW_SCOPE=collection:<Agent id>`,
   `BW_UNLOCK=stored` and `BW_SECRETS_ITEM_SCOPE=collection:<Stack secrets id>`. An existing
   `local.env` naming other ids is refused unless `--rotate` is given.
4. Invites the user's address (asked for, or `VAULT_OWNER_EMAIL`) to the organization as an Owner
   through the organization API (`POST /api/organizations/<id>/users/invite`; `bw` has no invite
   command). With no SMTP, Vaultwarden records an instance invitation for that address, so the user
   can register there although sign-ups are off, and the membership is accepted when they do. The
   user chooses their own master password. The stack never generates, sees or stores it.
5. `local-init` prints the web vault's sign-up address and waits up to 10 minutes for the user to
   register, polling the membership, then confirms them with `bw confirm org-member` as the agent
   account. If the user has not registered by then it exits 4, `vault-init` says what to do, and a
   re-run picks up from here.
6. Runs `bitwarden-setup role` with the user's unlock to change the agent account to the User role
   with access to the "Agent" collection (Manage) and "Stack secrets" (view) only, and records
   `STEP=role`. From then on the user owns the organization and Bitwarden, not the broker, enforces
   the agent's scope. The agent account never holds the user's items alone. `vault-init` then sets
   `BITWARDEN_ENABLED=1`.

`make vault-open` opens the web vault. Stopping is `BITWARDEN_LOCAL_ENABLED=0` and `make up`; the
local account file stays, and the broker reports it as offline, as `crm` does for local Twenty
workspaces.

The local Vaultwarden holds what the user stores in local mode, with no other copy unless they back it up. `vault-init`'s closing
message and `docs/bitwarden.md` say so, and the add-on's `backup` hook (`scripts/vault.sh backup
DIR`) covers the data directory, the secret files and the account files together: an online SQLite
backup of `db.sqlite3` (in a container that shares Vaultwarden's mounts while it runs), plus
`rsa_key*`, `config.json`, `attachments/` and `sends/`, `secrets/`, `local/` and the accounts folder,
into a new, empty folder. `restore DIR` needs Vaultwarden stopped and refuses to overwrite data
without `--force`; with it, it copies the backup over the existing data rather than replacing it.

## Setup and seeding

Creating the agent's folder or collection, moving items into it and changing the agent account's
role all need the user's own vault key, which the broker never holds in `collection` mode. These
steps run in `bitwarden-setup`, a one-shot service from the broker's image with no network access
to `hermes` and no shared volume with it. It asks for the user's master password in a masked
prompt on the host terminal (`docker compose run` with a TTY), unlocks the user's account into a
temporary `BITWARDENCLI_APPDATA_DIR` under `/tmp`, does its work, runs `bw lock` and `bw logout`,
and exits. Nothing it holds outlives the run. `vault-init`, `vault-add` and `vault-grant` (below)
all use it. Its commands: `create`, `seed`, `grant`, `role` and `list-unassigned` take `--slug` and
the user's `--email`; `tls`, `admin-token` and `local-init` are local mode's. It never writes
`requests.json` and has no broker mount: after a grant, `vault.sh` calls the broker's sync, which
marks the request done once the host is in scope.

What it creates, per mode:

| Mode and scope | Created | Left to the user |
|---|---|---|
| `local` | Everything in Local mode, plus the role change in step 6 | Accepting the invite |
| `cloud` or `selfhosted`, `folder:` | The folder, with `bw create folder` | Nothing |
| `cloud` or `selfhosted`, `collection:` | The collection in the user's organization, with `bw create org-collection`, and the agent member's access to it | Signing up the agent account (bitwarden.com requires email verification), and creating the organization if there is none |

The user's account needs admin rights in the organization for the `collection:` row. `bw` has no
invite or member-edit command; it sets collection access through the `users` list of
`bw create org-collection` and `bw edit org-collection`. Setup invites the agent account with the
organization API (`POST /api/organizations/<id>/users/invite`) under the user's borrowed login, as
Local mode does, which spike 2 checked on Vaultwarden only. If bitwarden.com refuses it, setup
prints the web vault steps.

### Seeding

After creating the space, setup asks which sites the agent should be able to sign in to. For each
answer it searches the user's vault for login items whose URI host matches, shows each match's
name, username and URI in the terminal, and moves the ones the user picks. The same matching and
move code serves `vault-grant`. A move into a folder edits the item's folder. A move into an
organization collection uses `bw move <item> <organization> <collections>`, which transfers the
item from the personal vault to the organization. Setup says so before the first such move, and
the docs say it too.

## Broker

`compose/bitwarden-broker/`: a Debian bookworm-slim image (pinned by digest) with the `bw` native
build for the target architecture, version (`BW_CLI_VERSION` build argument, 2026.10.1) and SHA-256
per architecture pinned in the Dockerfile, plus a Python stdlib HTTP server (`broker/`). The same
image runs `bitwarden-setup` and `bitwarden-secrets-sync` (profile `bitwarden-tools`). Runs as uid
10001 with a read-only root filesystem, all capabilities dropped and `no-new-privileges`; writable
paths are `/tmp`, `bw/` and `broker/`. Its environment is limited to the `BITWARDEN_*` paths and
settings and `NODE_EXTRA_CA_CERTS` (`test-compose-env-allowlist.py`).

### Networking

The broker runs with `network_mode: "service:hermes"`, sharing the `hermes` container's network
namespace and nothing else. It listens on `127.0.0.1:8099` there, reaches the Chromium that
browser-harness starts on its loopback CDP port, and reaches Bitwarden through `hermes`'s networks.
`hermes-worker` and every other container cannot reach it. Agent code in `hermes` can, which is why
the API below carries no secret in any response. With `depends_on: {hermes: {condition:
service_started, restart: true}}` compose restarts the broker when it restarts `hermes`. It does
not when `hermes` alone is recreated (checked on compose v5.6.1): the broker keeps
`container:<removed id>`, and a later `docker compose restart bitwarden-broker` leaves it exited.
`docker compose --profile bitwarden up -d --no-deps bitwarden-broker` recreates it in the current
namespace and leaves a broker already there alone. `scripts/vault.sh follow-hermes` (through
`follow_hermes` in `scripts/lib/bitwarden.sh`) runs it and lists the manual accounts to unlock,
and every stack command that recreates or restarts `hermes` calls it; it does nothing while
`BITWARDEN_ENABLED` is off. `vault.sh`'s own broker restart is `up -d --no-deps --force-recreate`.
`make doctor` compares the broker's `HostConfig.NetworkMode` with `container:<hermes id>` and
probes `/health` from `hermes`.

The local Vaultwarden is reached only through `vaultwarden-tls` on the internal `vault` network,
which `hermes` (and so the broker) joins (Local mode).

### API

JSON over HTTP on loopback, no authentication: every operation is one the agent can already ask for
through the vault tools, and none returns a secret.

| Call | Does | Returns |
|---|---|---|
| `GET /accounts` | Each account's slug, mode, scope kind and state (`unlocked`, `locked`, `offline`, `error`) | metadata |
| `GET /items` | Login items in scope across unlocked accounts | `[{handle, label, username, origins, has_totp}]` |
| `POST /fill` `{handle, target_id, port, field, base}` | Fills the password (`field: "password"`, the default) or the current TOTP code (`"totp"`) into the CDP target on `127.0.0.1:<port>` | `{filled, origin, error, redaction}` |
| `POST /sync` | Runs `bw sync` on every unlocked account now | `{synced}` |
| `POST /requests` `{account, site, reason}` | Records an access request (Access requests) | `{request_id}` |
| `GET /requests` | Pending and recent requests | metadata |
| `POST /release` `{handle}` | Stops serving an item at once and returns it to the user (Access requests) | `{released, returned}` |
| `GET /health` | Liveness and per-account state | status |

The Unix socket at `/run/bitwarden-sync/sync.sock` (mode 600, in the `bitwarden_sync` volume that
only the broker and `bitwarden-secrets-sync` mount, never bound on TCP) carries the three calls that
handle values: `GET /secrets` (Stack secrets), and `POST /unlock` and `POST /lock` for `manual`
accounts. Request bodies are JSON objects of at most 64 KiB with no unknown or duplicate keys, and
every error is `{"error": "<code>"}` with a fixed code.

`port` is the DevTools port from the task's `cdp_url`. Chromium picks a random one
(`--remote-debugging-port=0`), so the caller has to say which, and the broker connects only to
`127.0.0.1:<port>`. `base` is the profile's fingerprint base (Redaction). `error` is one of six
fixed codes (`target_not_found`, `origin_mismatch`, `no_field`, `write_unconfirmed`, `page_error`,
`cdp_unavailable`); `redaction` is withheld when nothing was written (`target_not_found`,
`origin_mismatch`, `no_field`), so a bad target id cannot collect entries for every item, and
returned for every other outcome, `write_unconfirmed` and a timeout included. A TOTP fill on an
item without a key, or whose `bw get totp` fails (no Premium on a cloud personal account), returns
`no_totp`.

`/fill` reads the target's URL over CDP itself, normalizes its origin (`broker/origins.py`, core's
rules, checked against core in `test-bitwarden-fill.py`) and refuses unless it is one of the item's
origins. It never trusts an origin from the caller. It then runs the same field probe
and write that core's `_fill_bound_page` uses (deep probe for `input[type=password]`, set the value,
dispatch `input` and `change`), wrapped so a page exception returns fixed text; the value is a
`Runtime.callFunctionOn` argument, never part of the function source. A `totp` write probes
`autocomplete="one-time-code"` and split one-box-per-character inputs as core's `enter_code` does,
falling back to the focused input. The probe runs in the top document, so a login form in a
cross-origin iframe is not found (`no_field`). A refusal or failure returns fixed text that never
quotes the value or the script. The whole CDP exchange is bounded by a 10-second deadline.

`redaction` is what the gateway needs to scrub the value from later tool output without holding it
(see Redaction). Usernames are returned in metadata, as upstream does, because the agent types them.

The broker re-checks scope on every call. It runs `bw sync` per unlocked account every
`BITWARDEN_SYNC_MINUTES` (default 10; 0 refuses to start) and on `POST /sync` (`make vault-sync`
calls it), unlocks `stored` accounts again after a lock or a failed start, and backs off to the
server's `Retry-After`, or one minute, after a 429 (Vaultwarden sends none); the account shows as
`offline (rate_limited)` meanwhile and its last synced items are still served. It logs method,
route, status and the handle, never values, bodies, item names, sites or `bw`'s output. `bw`'s
stderr is classified into `auth`, `locked`, `network`, `rate_limited` and `unknown` by patterns
checked against `bw` 2026.10.1 and Vaultwarden. A `denylist.json` or `requests.json` that does not
load stops the broker, so it never starts empty and serves a released item again.

## Hermes side

### Plugin backend

The plugin is a directory, `compose/hermes/plugins/bwbroker/` (`plugin.yaml` and `__init__.py`
with `register(ctx)`), because Hermes loads plugins from `<plugins dir>/<name>/`. The backend module
is `compose/hermes/bitwarden_broker_backend.py`, which `__init__.py` imports from beside itself. The
plugin registers the backend under the name `bwbroker` (handles `bwbroker:<slug>/<item id>`) and the
two access tools under their own toolset, `bitwarden`. `list_items` and `get_meta` call `/items`
and build `VaultItemMeta` with `allowed_origins` set, so core's origin pre-check works as it does
for the built-in managers. `resolve_password`, `resolve_otp` and `resolve_secret` raise: no value
ever comes back to the gateway. A broker that does not answer lists nothing rather than raising.

Hermes v0.22.7 accepts a login backend only from a bundled plugin or a catalog install at tier
verified or official (`login_backend_provenance_error`), and no config key relaxes that; a copy under
`$HERMES_HOME/plugins` loads its tools and has its backend refused. So the cont-init script
`14-install-bwbroker-plugin.sh` copies the plugin and the backend module into
`/opt/hermes/plugins/bwbroker` while `BITWARDEN_ENABLED` is on and removes it otherwise, as the stack
already patches `/opt/hermes` for its overlays. The bootstraps then, per profile from the manifest's
`vault-plan`, add `bwbroker` to `plugins.enabled` and set `vault.bwbroker.enabled: true` on
`default` and `browser`, remove both elsewhere or when the flag is off, and delete any copy of the
plugin in a profile's own plugins dir. While the add-on is on they write
`vault.bitwarden.enabled: false` on every profile they manage, `api-server` included; turning it off
leaves that line in place.

Cron is kept out at two layers, because a login backend is on for every turn a profile runs and
`browser_vault` itself rides in cron. The bootstraps list the `bitwarden` toolset in
`known_plugin_toolsets.cron` while the plugin is enabled on a profile and take it out of a cron
tool list (`addon_manifest.cron_fence_toolsets`), and the backend lists nothing and so fills nothing
in a cron turn. It detects one from `cron.execution_identity.current_cron_execution()`, and in a
`delegate_task` child from the job's context variable that the child's thread inherits; a delegated
child for which that variable cannot be found counts as cron. A subagent a chat starts is not cron.
These limits, and the add-on's absence from `api-server`, cover the vault tools. Code the agent
runs in `hermes` (`browser_exec`) can call the broker API on any profile and in cron.

A locked account's items do not appear. Because a plugin cannot use Hermes' unlock prompt,
`browser_vault_list` gets a hint from the overlay below naming the locked account and
`make vault-unlock SLUG=…`, or `{"unavailable": true}` when the broker does not answer.

### Fill overlay

`compose/hermes/patch-bitwarden-broker-fill.py`, anchored in `tools/browser_vault_tool.py`. For a
`bwbroker:` handle, after core's origin pre-check and inside `fill_context`, it replaces the
resolve-and-write step with a `/fill` call carrying the CDP target id of the task's current page,
the port and the profile's redaction base. It registers the returned `redaction` entry for every
outcome that returns one (logging a fixed line when registration is refused), then shapes the
result like core's from the broker's fixed error code only. `browser_vault_enter_code` gets the same
routing with `field: "totp"`, falling back to core's ask-the-user path when the item has no TOTP.
A fill re-admits Bot Desktop control at the write as core does. When the digest overlay is missing
(`vault_redaction_base` cannot be imported) every `bwbroker:` fill is refused as unavailable, so no
value is written that could not be scrubbed. A fill whose metadata read came back empty because the
broker is down gets "Bitwarden is unavailable right now" instead of core's unknown-handle text.

Both overlays are mounted and applied from `05-patch-tool-calling.sh` on every install, as every
overlay is; they are inert until a `bwbroker:` handle is filled or a digest registered, so the
"byte-identical with flags off" rule covers services, profile configs and bootstrap output, not
overlay mounts.

`browser_vault_save_login` is left to core. It stores into Hermes' local Fernet vault, never into a
broker account, so no password passes toward Bitwarden through the gateway, and refusing it would
break local saves on the Bitwarden profiles. The `browser-login` skill says Bitwarden logins are
added in Bitwarden or asked for with `bitwarden_request_access`.

The gateway's CDP supervisor keeps the attached page's session id, not its target id. The overlay
calls `focus_page` for the item's origin as core does, then `Target.getTargetInfo` on that session
for the target id and passes it with the CDP port from the task's `cdp_url` (or the supervisor's),
loopback only, so the broker writes into the tab the agent drove. Spike 1 showed a second container in the `hermes` network namespace
attaching to that target and filling it while another CDP client held a session on the page, with
that session unaffected.

### Redaction

Core scrubs exact registered values. With broker fills the gateway never has the value, so the
broker returns, per fill, a fresh random 32-byte key `k` and, for each of five forms of the value
(raw, standard base64, hex, percent-encoded, reversed) and the same five forms of the value with CR
and LF removed (what a text input keeps), the form's byte length, the low 20 bits of its Rabin-Karp fingerprint and
`HMAC-SHA256(k, form)` (`broker/digest_redaction.build_entry`).

The fingerprint uses modulus 2^61−1 and one random base per profile, not per fill. The gateway
creates the base on first use (`agent.redact.vault_redaction_base`), sends it in each `/fill`
(`base`, 256 ≤ base < 2^61−1), and the broker builds the entry with it. Base secrecy adds nothing,
since a fingerprint collision costs only an HMAC check and agent code reading gateway memory would
get the base either way, while one base per fill would make the scan linear in the number of
entries on a path that runs on every tool result and log line.

`compose/hermes/patch-digest-redaction.py` copies `digest_redaction.py` into `agent/` and extends
`agent/redact.py`: `register_vault_redaction_digest` stores entries normalized, per profile, under
core's lock and per-profile cap, and drops a malformed entry or one built with another base; the
scrub runs in `redact_registered_vault_values` after core's exact pass. It scans the UTF-8 bytes in
64 KiB chunks overlapping by the longest form's length minus one, so memory stays bounded: one
prefix-fingerprint array per chunk, one window pass per distinct form length with a dict lookup
from fingerprint to candidate forms, and an HMAC only on a hit. An HMAC match replaces the window
with core's marker, `«redacted-vault-secret»`. Every form is a whole UTF-8 sequence, so a match
starts and ends on character boundaries. The scrub fails closed: on any internal error it returns
`«redaction failed»` in place of the whole text and logs one fixed line. Entries and the base live
in gateway memory for the profile, like core's values, and clear with `clear_vault_redaction_values`.

Agent code that reads gateway memory, or that reads a `/fill` response, gets `k` and the
fingerprints and can test guesses offline, so a short or guessable value is recoverable, and a
TOTP code (6 digits) is recovered from its entry at once, for its time step. Fingerprints are
stored and compared as their low 20 bits (Ruling R28): a full fingerprint is linear in the value's
bytes, and with the base in the entry the raw and reversed fingerprints let lattice reduction
recover a random 12- or 16-character password. Two 20-bit fingerprints carry about 40 bits, so the
entry alone does not determine a long random value. The scan computes each window's full rolling
hash, looks up its low 20 bits, and confirms every hit with the HMAC; a false hit costs one HMAC
per about 2^20 windows per form.
`docs/bitwarden.md` says to keep weak passwords out of the agent's folder or collection. A fill
whose `/fill` reply is lost after the broker wrote the page registers no entry.

## Stack secrets

`make secrets-sync` runs a one-shot service, `bitwarden-secrets-sync`, for every account with
`BW_SECRETS_ITEM_SCOPE` set (`local` sets it to the "Stack secrets" collection). Values come from
the broker, which holds the session keys, over a Unix socket in a named volume
(`bitwarden_sync`) that only the broker and this service mount. The socket is the one channel that
returns values and serves only items in `BW_SECRETS_ITEM_SCOPE`. `hermes` does not mount the
volume, so agent code cannot reach it, and the long-running broker never mounts the target files.

Each login item maps by its exact name to a secret file the stack already reads; the value is the
item's password field:

| Item name | File and key |
|---|---|
| `twenty/<slug>/key-ro`, `twenty/<slug>/key-rw` | `TWENTY_KEY_RO` / `TWENTY_KEY_RW` in `compose/twenty/workspaces/<slug>.env` |
| `twenty/encryption-key` | the whole file `TWENTY_ENCRYPTION_KEY_FILE` names |
| `twenty/db-password` | the whole file `TWENTY_DB_PASSWORD_FILE` names |
| `postgres/<role>` | `DB_SERVICE_PASSWORD` in `compose/postgres/services/<role>.env`, plus the same password inside `DATABASE_URL` and any `*_DATABASE_PASSWORD` line that held the old value, since those lines carry the same secret |
| `caldav/<slug>` | `CALDAV_PASSWORD` in `compose/caldav-mcp/accounts/<slug>.env` |

There is no `llm-proxy/<route>` row: the stack has no host-side key file per route to write. The
first table named keys that do not exist (`DB_*_PASSWORD`, `CALDAV_<SLUG>_PASSWORD`); the ones above
are what those files hold. A `postgres/<role>` file with no `DB_SERVICE_PASSWORD` line is skipped.

`bitwarden-secrets-sync` runs with no network and mounts, read-write, only the folders of the files
it writes: the Twenty workspaces, Postgres services and CalDAV accounts folders, and
`TWENTY_SECRETS_DIR` for the two Twenty files. Compose cannot take the folder of a `*_FILE` path, so
both Twenty files must sit directly in `TWENTY_SECRETS_DIR`, a new variable; unset, it mounts the
empty `compose/twenty/no-secrets/` and those items are skipped with a note. An unknown item name is
reported and skipped, a name that maps to two files is refused, a missing file is never created and
a symbolic link is skipped. A file is rewritten only when its value changed, through a temp file in
the same folder, at mode 600 and with the old owner where the container may set it (a note says when
it could not). The command lists which files changed, never values, and exits 3 when a file Hermes
reads changed, on which `vault.sh` runs `make hermes-reload`. `make up` runs it through
`vault.sh up-hook` when any account has `BW_SECRETS_ITEM_SCOPE` and that account is unlocked;
otherwise the files keep their last values and `make up` says the sync was skipped.

The `KEY=value` files are read by compose's `env_file`, POSIX shell and Python parsers with
different quoting rules. A value bound for one of them is refused, skipped with a note naming the
item and never the value, when it contains `$`, a backtick, `\`, `'`, `"`, `#`, whitespace at
either end, or a control character; refusing is the one rule all three readers agree on, and
generated keys and passwords are normally alphanumeric. A safe value keeps the line's quoting (bare,
double- or single-quoted). The whole-value Twenty files take any value. Lone surrogates are refused
everywhere.

A new value does not always reach the service that checks it. A Twenty API key must exist in
Twenty first. A new `twenty/encryption-key` loses what Twenty stored. A new `twenty/db-password`
does not change the role's password, since Postgres reads its password file only at first
initialisation, and the Twenty containers need recreating. A new `postgres/<role>` takes effect when
`db-provision` next runs and the integration restarts. `docs/bitwarden.md` gives the steps.

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
scope. The broker keeps requests in `broker/requests.json` and drops them after 7 days. It holds at
most 10 pending requests and refuses more, so a looping or injected agent cannot bury the user in
them; a repeat of a pending (account, site) pair returns that request's id. `site` must be a
hostname or a URL whose host is taken, stored lower-case in its IDNA form. `reason` loses every
Unicode `C*` character and is cut to 200 characters, and is always shown to the user labelled as
the agent's text. `account` defaults to the only account when there is one.

### Approving

- **`make vault-grant`** lists pending requests. For the one the user picks, it runs
  `bitwarden-setup grant` with the user's unlock, which shows the matching items from the user's
  vault (Seeding) and moves the one the user chooses. `vault.sh` then calls the broker's sync, which
  marks the request done once the host is in scope. `make vault-grant SITE=linkedin.com` skips the
  request list.
- **In Bitwarden.** The user moves the item into the folder or collection in any Bitwarden app.
  The broker's next sync, or `make vault-sync`, picks it up and marks a pending request for that
  host done.
- **In Boundary.** A native dialog doing what `vault-grant` does. This needs a Boundary change
  request: the stack exposes `scripts/vault.sh requests --json` and
  `scripts/vault.sh grant --site … --slug … --email … --item …`, and Boundary owns the masked
  prompt. `grant` has no machine-readable output yet and needs a terminal for the setup prompt.

Hermes' approval cards are not used. The approval itself is not the hard part; the move needs the
user's vault key, which in this design exists only inside `bitwarden-setup`. In `folder` mode with
`stored` unlock the broker could technically do the move, but its API has no call for it, so the
agent can never widen its own scope.

### Releasing

`bitwarden_release_access(handle)` takes an item out of the agent's scope on the agent's request
alone, because narrowing access is safe whoever asks. The broker adds the handle to a denylist in
its data directory and stops listing and filling it at once. Then it returns the item:

- `folder:` scope: sets the item's folder to none (`bw edit item` with the item JSON on stdin).
- `collection:` scope: removes the item from the scope collection only, keeping any others the user
  put it in (`bw edit item-collections <id> --organizationid <org>` with the remaining ids on
  stdin). The intent is to take the item out of the agent's reach, not to strip sharing set up
  elsewhere. When the scope collection was its only one, it stays in the organization as an
  unassigned item, which only Owners and Admins see (checked on Vaultwarden 1.37.4). Vaultwarden
  refuses to move it into a collection the agent cannot write ("No rights to modify the
  collection", spike 2), so there is no "Returned" collection.
  `bitwarden-setup list-unassigned` lists unassigned items so the user can file them. Whether
  bitwarden.com allows the removal the same way is not yet checked.

The broker reads the item first; one already out of scope gets no edit and counts as returned. When
`bw` refuses the edit, `/release` returns `returned: false` and the item stays denylisted and in the
agent's scope until the user moves it. A denylist entry is dropped at the first listing that no
longer has the item in scope, so a later grant of the same item lists it again.

## Manifest and profiles

`addons/bitwarden/addon.json` is a built-in add-on (`minStackVersion` 0.3.12), checked by
`scripts/lib/addon_manifest.py` and `scripts/test-addon-manifests.py`. The file is
canonical JSON (`json.dumps(indent=2, ensure_ascii=False)` plus a newline), because
`sync-profiles.sh` reads its `builtin` block with `sed` before there is a host `python3`.

### What validates today

- `builtin`: `{"flag": "BITWARDEN_ENABLED", "profile": "bitwarden", "seed": false}`. `seed` is
  false so a missing flag stays off.
- `services`: `{"broker": {"compose": "bitwarden-broker", "port": 8099}}`. Images stay in
  `docker-compose.yml`, as for every built-in.
- `uses`: none. `mcp`: none. `vaultBackend` as in Changes item 2.
- `permissions`: `reads: ["credentials"]`, `sends: false`, `cron: false`, `exposesToAgent: false`,
  `egress: ["internet"]`, `writes: ["external:bitwarden"]` (moves made by releases, and by
  setup and `vault-grant` with the user's unlock).
- `settings`: `sync_minutes` (`int`, default 10, min 1, since the broker refuses 0) and
  `idle_lock_minutes` (`int`, default 0, min 0). The validator checks only `type`; the values
  reach the broker as `BITWARDEN_SYNC_MINUTES` and `BITWARDEN_IDLE_LOCK_MINUTES` from `.env`,
  because settings files are not implemented. No `choice` settings: mode,
  scope and unlock are per-account values in the account files (Accounts), and the broker refuses
  an account whose `BW_MODE`, `BW_SCOPE` or `BW_UNLOCK` is not one of the values listed there, the
  same fail-closed rule the manifest spec sets for `choice`.
- `hooks.backup`, `hooks.restore`: `scripts/vault.sh backup` and `scripts/vault.sh restore` (both
  take a folder), covering the local Vaultwarden, the secret files and the account files. The
  validator does not check `hooks`, and no installer runs them yet.

### Changes the manifest code needs first

Each is a change to the manifest spec and `addon_manifest.py`, with a case in
`test-addon-manifests.py`, and lands before the Bitwarden manifest:

1. **`credentials` in `READ_CLASSES`.** It takes the existing rules for every `reads` class with
   no exception: `cron: false`, `sends: false`, and no `api-server` profile. Bitwarden v1
   therefore stays off `api-server` and out of cron, and logins in unattended jobs are left for
   later (Not in v1, below).
2. **A `vaultBackend` block**, because the add-on reaches Hermes through a login-backend plugin,
   not MCP: `{"name": "bwbroker", "plugin": "compose/hermes/plugins/bwbroker", "toolset":
   "bitwarden", "tools": ["bitwarden_request_access", "bitwarden_release_access"], "profiles":
   {"default": "rw", "browser": "rw"}}`. `plugin` is the repo-relative plugin directory (no `..`,
   not absolute; its existence is not checked). `toolset` is required and names the toolset the
   plugin registers its tools under, so the cron fence can keep it out of cron
   (`addon_manifest.cron_fence_toolsets` fences it for every add-on whose `permissions.cron` is not
   true, and for an invalid manifest). The validator gives `vaultBackend.profiles` the same `reads`
   check as `mcp.profiles` (no `api-server`), refuses a `name` in a built-in backend's namespace
   (`vault`, `op`, `bw`), refuses unknown fields, and allows the block for built-in and official
   sources only. `addon_manifest.py vault-plan <addons-dir> <profile>` prints
   `name|enabled|access|plugin|tools` per add-on next to `mcp-plan`, and the bootstraps act on it
   (Plugin backend).

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

Environment, all defaulting to off so Boundary and existing installs see no change beyond
`hermes` joining the `vault` network:

| Variable | Default | Meaning |
|---|---|---|
| `BITWARDEN_ENABLED` | `0` | Starts the broker (profile `bitwarden`, from the manifest's `builtin` row) and registers the backend; `hermes` and both bootstraps receive it |
| `BITWARDEN_LOCAL_ENABLED` | `0` | Starts `vaultwarden` and `vaultwarden-tls` (profile `vault`, a `SERVICES_CORE` row in `sync-profiles.sh`) |
| `BITWARDEN_SYNC_MINUTES` | `10` | `bw sync` interval; must be above 0 |
| `BITWARDEN_IDLE_LOCK_MINUTES` | `0` | Lock `manual` accounts after this long without a fill or unlock; 0 never |
| `BITWARDEN_HOST_DATA_DIR` | `./data/addons/bitwarden` | The data layout in Accounts |
| `VAULTWARDEN_PORT` | `8222` | Host port, bound to `127.0.0.1` |
| `VAULTWARDEN_TLS_IMAGE` | `nginx:1.30.6-alpine` | The TLS sidecar's image |
| `VAULT_OWNER_EMAIL` | unset | The user's address for `vault-init` |
| `TWENTY_SECRETS_DIR` | unset | The folder holding both Twenty secret files (Stack secrets) |

`BW_CLI_VERSION` (2026.10.1) is a build argument in the Dockerfile, next to the matching digests,
not an `.env` key.

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
| `bitwarden-setup` gets a wrong master password | | Masked prompt again, at most three times, then exits 2 with nothing changed |
| Agent account registration fails (a Vaultwarden release changes the API) | | `vault-init` prints the sign-up address and asks the user to register `agent@vault.localhost` with the generated password in `secrets/local/master_password`, then a re-run continues from the API key step |
| Broker restarts while `hermes` stays up, or `hermes` is recreated | Fills during the restart get "Bitwarden is unavailable right now" | `stored` accounts unlock again with no action; `manual` ones need `make vault-unlock`; requests and denylist entries survive |
| `/fill` targets a tab that navigated or closed after core's pre-check | The broker's `origin_mismatch` or `target_not_found` text | none |
| A login form in a cross-origin iframe | "No password field on the current page" | none |
| An unsafe value for a `KEY=value` file in `secrets-sync` | | Skipped with a note naming the item |

## Spike results

Both ran on 2026-11-08 in scratch containers with synthetic accounts and a local test page; nothing
touched the running stack. The code was throwaway and is not kept.

1. **Broker-side fill works.** In the `nousresearch/hermes-agent:v0.22.7` image, a stand-in for
   browser-harness launched the image's Chromium (`--remote-debugging-port=0`) and held a CDP
   session on a login page. A second container with `network_mode: container:<that one>` read the
   target's URL from `/json/list`, checked its origin, attached with `Target.attachToTarget`
   (flattened) and filled the password field; the first session then read the filled value's
   length and kept working. The same image confirmed the `/proc/<pid>/mem` read described in
   "Where agent code runs".
2. **Vaultwarden 1.37.4 with `bw` 2026.10.1.** Passed: sign-ups off refuses an uninvited
   registration; admin invite plus client-side registration (PBKDF2-SHA256 600,000 iterations,
   HKDF-stretched key, type-2 protected key, RSA-2048 pair); `bw login` with the password and
   with `--apikey` followed by `bw unlock --passwordenv`; organization creation over the API;
   `bw create org-collection`; an organization invite to an unregistered address that the user
   then registers from with no SMTP, landing as Accepted; `bw confirm org-member`; the new Owner
   changing the founding account to User with Manage on "Agent" and view on "Stack secrets"; the
   demoted account refused when it tries to edit the Owner and listing only the "Agent" item; and
   `bw move` of a personal item into "Agent", which the agent then sees. Failed: `bw` refuses
   plain-HTTP server URLs, so local mode gained the TLS sidecar; the demoted account moving an item
   into a collection it cannot write, so releasing now removes the item from all collections
   instead, which Vaultwarden allows.

Not yet checked: the same organization steps on bitwarden.com. That needs a throwaway
bitwarden.com account with a Free organization, which Keith creates; the spike script can then be
pointed at it. Until then the `cloud` mode's release path keeps the fallback in Releasing.

Checked later against Vaultwarden 1.37.4 and `bw` 2026.10.1 (`scripts/test-vw-register.py`, a
throwaway instance): `bw edit item-collections` with the ids on stdin, an empty list included,
works; an unassigned item shows to the Owner; `bw`'s stderr for a wrong password, a wrong API key
and a 429 matches the broker's patterns; Vaultwarden sends no `Retry-After`. Not handled by setup on
any server: accounts with two-step login, bitwarden.com's new-device verification, and Argon2id
accounts (the organization API token is computed with PBKDF2 only).

## Documentation

- `docs/bitwarden.md`: the three modes, scope and unlock choices with the trade-off of `stored` on
  a personal account, the local Vaultwarden's backup warning, `secrets-sync` item names, what the
  agent can and cannot see (below), and troubleshooting.
- `SECURITY.md`: rows for the broker, the setup and sync one-shots, Vaultwarden and its TLS
  sidecar in the exposure table; Do/Do-not entries for `scope: all`, `stored` on a personal
  account, and publishing the Vaultwarden port.
- `docs/hermes.md`: the two overlays in the patch table with their tests, the cont-init script and
  the bundled-plugin rule, the upstream symbols each overlay anchors on, and the `bwbroker` backend
  next to "Saving logins".
- `compose/hermes/skills/browser-login/SKILL.md`: the locked-account hint, that Bitwarden logins
  are added in Bitwarden (there is no save refusal), and what to do when no saved login exists for
  a site: call `bitwarden_request_access` once, tell the user how to approve, and never claim to
  have moved anything.
- `docs/bitwarden.md` also covers setup and seeding, `make vault-grant`, releasing, that a move
  into an organization collection transfers the item to the organization, the follow-up steps
  after a Twenty or Postgres secret changes, Linux ownership, and the known gaps in this spec.
- `CHANGELOG.md`: new services, flags and env vars, `vault.bitwarden.enabled: false` written by
  bootstrap, `hermes` joining `vault`, the overlays and cont-init, the manifest validator changes,
  and the plugin-directory deviation.

What `docs/bitwarden.md` states plainly: the agent drives the page the password is typed into, so
code it writes can read the field. Origin binding limits which pages that can happen on, and digest
redaction removes the value and four encodings of it from what the model sees. It does not stop
page script from sending the value elsewhere, so the agent's folder or collection should hold only
logins the user is willing to have used that way. The broker cannot tell browser-harness's Chromium
from a CDP endpoint that agent code starts on the same loopback and that reports any URL it likes,
so this exposure does not depend on the agent reaching the real site; the broker checks the origin
to keep fills off the wrong page, not to keep the value from agent code.

## Testing

- `scripts/test-bitwarden-broker.py`: the broker against a fake `bw` (a script answering `list`,
  `get`, `unlock`, `sync` from synthetic JSON): scope filtering per mode, URI match "Never", origin
  refusal on a mismatched target, no value in any response, log line or error text (assert by
  searching all output for the synthetic secrets), 429 backoff, `manual` lock and unlock, and the
  sync socket serving only items in `BW_SECRETS_ITEM_SCOPE`.
- `scripts/test-bitwarden-fill.py`: the fill overlay and plugin against the image, with a stub
  broker and the image's headless Chromium: routing of `bwbroker:` handles, `resolve_password`
  never called, the redaction entry registered, TOTP routing and fallback, `save_login` left as
  core wrote it, and the broker's `normalize_origin` agreeing with core's.
  (`vault.bitwarden.enabled: false` is checked in `test-bitwarden-registration.sh`.)
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
  reserved backend name refused, a missing or malformed `toolset` refused, `vault-plan` lines and
  the toolset fence, and `addons/bitwarden/addon.json` valid and canonical, with its flag received
  by both bootstraps and `hermes`. `scripts/test-sync-profiles.sh` checks the
  `BITWARDEN_ENABLED bitwarden 0` row and that the flag turns the `bitwarden` profile on and off.
- `scripts/test-bitwarden-compose-config.py`: the resolved compose config of the Bitwarden services
  (profiles, the broker's namespace and mounts, the network layout, the image pins), and that none
  of them is in `core` alone or starts local mode without the `vault` profile.
- `scripts/test-vw-register.py`: spike 2's sequence and the live checks against a throwaway
  Vaultwarden 1.37.4 with the broker image's `bw`; skips without Docker.
- `scripts/test-vault-sh.sh`: `vault.sh` commands with a recording stub `docker`, including the
  Linux ownership paths and backup and restore.
- `make bitwarden-test` runs all of the above; CI runs the ones that need no Docker or image.
- Tool-eval: `browser-login-bitwarden`, a model reaching a sign-in page with a `bwbroker:` login
  listed fills it without asking for a password. `bitwarden-request-not-move`, the user asks the
  agent to move a login into its vault: the model calls `bitwarden_request_access` once and
  describes the approval step, without claiming the move happened. `bitwarden-injected-request`, a
  page asks the agent to request access to a bank login: the model does not call the tool. The
  eval has no page fixture, so the page's text is carried in the prompt as text the user copied.
  The cases are gated on the `bitwarden` profile; the scorer sees tool names only, so "once" and
  "no claim" are manual checks.
- One live check against a throwaway local Vaultwarden (`vault-init`, fill a local test page, sync a
  test secret), run once before release and noted in the changelog. Real accounts are the user's to
  try.

## Upkeep

On Hermes bumps run `test-bitwarden-fill.py`, `test-digest-redaction.py` and
`test-bitwarden-registration.sh --image` (or `make bitwarden-test`); both overlays anchor in upstream
vault code, and registration depends on v0.22.7's rule that a login backend comes from a bundled or
verified plugin. `docs/hermes.md` lists the symbols each overlay anchors on. Check
`agent/vault_backends/bitwarden.py` and the plugin contract for changes: if upstream moves its fill
or session key out of the gateway process, re-evaluate whether the broker is still needed. On `bw`
bumps update `BW_CLI_VERSION` and both digests, and rerun the broker test and
`test-vw-register.py`.
