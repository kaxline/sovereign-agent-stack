# Bitwarden

Hermes signs in to websites with logins kept in Bitwarden. A sidecar, `bitwarden-broker`, holds
each account's `bw` data, API key and session key, and writes passwords and one-time codes into the
page itself. The Hermes gateway lists metadata only (handle, label, username, origins, whether the
item has a TOTP key) and never holds a value. The agent keeps the `browser_vault_*` tools and the
`browser-login` skill; Bitwarden logins appear in `browser_vault` `list` with `bwbroker:` handles.

The add-on is on the `default` and `browser` profiles only. It is never on `api-server`. Cron jobs
get no Bitwarden logins and no fills; the two access tools stay out of cron's tool list unless a job
names the `bitwarden` toolset in its own `enabled_toolsets` (see
[What the agent can and cannot see](#what-the-agent-can-and-cannot-see)). These limits cover the
vault tools. Code the agent runs in `hermes` (`browser_exec`) can call the broker API on any
profile and in cron.

## Accounts and modes

Each Bitwarden account the broker signs in to is an instance in the add-on store,
`<data root>/addons/bitwarden/instances/<slug>.json` (`ASSISTANT_DATA_ROOT`, default `./data`). The
local account is the exception: `make vault-init` keeps it outside the store (see
[Local mode](#local-mode)). Several accounts can be configured at once.

| Mode | Server | Account made by | Command |
|---|---|---|---|
| `cloud` | bitwarden.com, US or EU | you | `make vault-add SLUG=personal MODE=cloud [REGION=eu]` |
| `selfhosted` | your Bitwarden or Vaultwarden at `URL` (HTTPS) | you | `make vault-add SLUG=home MODE=selfhosted URL=https://…` |
| `local` | Vaultwarden in this stack | `make vault-init` | `make vault-init` |

`make vault-add` asks for the account's personal API key (`client_id`, `client_secret`; web vault:
**Settings > Security > Keys > API key**) and, with stored unlock, the master password, in hidden
prompts on the host terminal. None of them is ever a command-line argument. It writes the instance
file (through `scripts/lib/addon_store.py`, with the manifest's validation) and `secrets/<slug>/`
under `BITWARDEN_HOST_DATA_DIR`, sets `BITWARDEN_ENABLED=1`, and offers to create the folder or
collection and choose its items (see [Setup and seeding](#setup-and-seeding)). With the broker
running it restarts the broker, which reads the instance store only when it starts, and reports what
the broker makes of the credentials. When Bitwarden refuses the API key or master password, it
removes what it wrote (with `FORCE=1`, it puts the previous instance file and secrets back). Without
a running broker the check happens at the next `make up`; `make doctor` shows the result.

Other options: `SCOPE=` (default `folder:Agent`), `UNLOCK=stored|manual` (default `stored`),
`FORCE=1` to replace an existing account. `make vault-remove SLUG=…` deletes the instance file,
its secrets and its `bw` data, and restarts the broker; the API key still exists in Bitwarden until
you revoke it there.

### Instance files

```json
{
  "mode": "cloud",
  "region": "eu",
  "scope": "folder:Agent",
  "unlock": "stored"
}
```

| Key | Values | Default |
|---|---|---|
| `mode` | `cloud` or `selfhosted` | required |
| `region` | `us` (vault.bitwarden.com) or `eu` (vault.bitwarden.eu); cloud only | `us` |
| `url` | your server's `https://` URL, with no user, query or fragment; selfhosted only, required there | |
| `scope` | `folder:<name>`, `collection:<id>` or `all` | `folder:Agent` |
| `unlock` | `stored` or `manual` | `manual` |

The slug is the file name without `.json`: a lowercase letter, then up to 30 of `a-z`, `0-9` and
`-`, the add-on store's rule. `local` is reserved for `make vault-init`. The broker ignores keys it
does not know. `scripts/addon.sh` and `make vault-add` write these files; the broker and
`bitwarden-setup` mount `instances/` (the broker read-only). An account the broker cannot load is
refused on its own, and the others still load. Its cause is one of:

- `bad_file`: the file is not a JSON object or cannot be read, or a value is wrong
  (`"unlock": "auto"`, a selfhosted account without `url`). The message names the setting.
- `bad_slug`: the file name breaks the slug rule, or is `local.json`.
- `missing_client_id`: `secrets/<slug>/client_id` is missing or empty. Connect the account again.

Credentials never go in this file. The API key and, with stored unlock, the master password are
`client_id`, `client_secret` and `master_password` in `secrets/<slug>/`, mode 600. The manifest marks all three `transient`, so the add-on store never
holds them. Only the broker (read-only) and `bitwarden-setup` mount that folder.

Account files from earlier versions, `compose/bitwarden/accounts/<slug>.env`, are not read. `make
doctor` warns while that folder holds any; connect those accounts again with `make vault-add` or
from Boundary, then delete the folder.

### Scope

`scope` decides which items the agent can reach through an account.

- `folder:<name>` works on a personal account with no organization. The broker lists and fills
  only items in that folder. Bitwarden does not enforce this: the broker holds a key that decrypts
  the whole vault.
- `collection:<id>` is for an organization. The account is then a separate member account
  (for example `agent@…`) that the organization grants only that collection, so Bitwarden
  enforces the scope and the broker never holds a key to anything else. Use this wherever you have
  an organization. Local mode is set up this way.
- `all` lets the agent reach every item. `make vault-add SCOPE=all` refuses it unless you also
  set `CONFIRM_SCOPE_ALL=1`, and `make doctor` warns while any account uses it.

An item is listed only when it has at least one `http` or `https` URI whose match type is not
"Never". Its origins are those URIs, normalized as Hermes normalizes origins, and a fill runs only
on one of them.

### Unlock

- `stored`: the broker unlocks with the stored master password at start and again after any lock,
  so the logins work in every session on `default` and `browser` with no prompt. The session key
  stays in the broker's memory.
- `manual`: no master password is stored. `make vault-unlock SLUG=…` asks for it in a hidden prompt
  and hands it to the broker over a Unix socket in `/run/bitwarden-sync`, a tmpfs only the broker
  mounts, through `docker compose exec`. The broker uses it for one `bw unlock` and drops it. The account stays unlocked until the
  broker restarts, `make vault-lock SLUG=…`, or `BITWARDEN_IDLE_LOCK_MINUTES` without a fill or
  unlock (default 0, never). While it is locked its items are not listed, and `browser_vault` `list`
  carries `"bitwarden": {"locked": ["<slug>"], "unlock": "make vault-unlock SLUG=<slug>"}`. The broker
  shares `hermes`' network namespace (`network_mode: service:hermes`). Compose restarts it when it
  restarts `hermes` (`make restart`, `docker compose restart hermes`), but not when `hermes` alone is
  recreated (`docker compose up -d --force-recreate hermes`): the broker keeps the removed
  container's namespace, Hermes gets `broker_unavailable`, and `docker compose restart
  bitwarden-broker` leaves it exited. `make hermes-reload`, `make hermes-recreate`, `make restart`
  and the stack scripts that recreate `hermes` run `./scripts/vault.sh follow-hermes`, which runs
  `up -d --no-deps bitwarden-broker` with the profiles in `.env` plus `bitwarden` and lists the
  manual accounts to unlock. Run it yourself after recreating `hermes` any other way. A manual account
  needs unlocking again after any of these.

A `manual` account is signed in with its API key (`bw login --apikey`) each time the broker starts,
and stays locked. `bw` keeps its login across broker restarts, so that sign-in checks a `manual`
account's API key only the first time and does not catch a key revoked later. `GET /accounts` reports `logged_in` and, after a rate limit, `retry_after` (whole seconds).

With `stored` on a personal account (`folder:` scope), the broker holds a password that opens your
whole vault, and the scope only limits what it lists and fills. `make vault-add` says so before it
writes the password. A `collection:` scope with a member account avoids this.

## Local mode

`make vault-init` sets up a Vaultwarden in this stack, re-runnably:

1. Generates a private CA and a server certificate for `vaultwarden.vault.internal`, and a random
   Vaultwarden admin token with its Argon2id hash, in `local/` under `BITWARDEN_HOST_DATA_DIR`.
   Sets `BITWARDEN_LOCAL_ENABLED=1` and starts `vaultwarden` and `vaultwarden-tls` (profile `vault`).
2. Registers the agent account `agent@vault.localhost` with a generated master password and stores
   that password and the account's API key in `secrets/local/`.
3. Creates the organization "Assistant" with the collection "Agent" and writes the local account:
   `broker/local-account.json` (`{"scope": "collection:<Agent>"}`, mode 600) and
   `secrets/local/client_id`. The broker loads it as mode `local` with stored unlock. It stays out
   of the add-on instance store, so Boundary cannot disconnect it; `status --json` lists it.
4. Invites your address (`VAULT_OWNER_EMAIL`, or asked) as an Owner and prints the web vault's
   address. Register there with that address and a master password you choose; the stack never
   sees or stores it. Sign-ups are otherwise off.
5. Waits up to 10 minutes for you to register, then confirms you in the organization. If you have
   not registered by then it stops; `make vault-init` again picks up from this step.
6. Asks for your master password once, in `bitwarden-setup`, to change the agent account to the
   User role with Manage on "Agent". From then on you own the
   organization and Bitwarden enforces the agent's scope.

Each finished step is recorded in `local/state`. On a stack set up before the local account moved
out of `compose/bitwarden/accounts/local.env`, `make vault-init` writes the two files from
`local/state` without signing in. `make vault-init` then sets `BITWARDEN_ENABLED=1`;
run `make up`. It starts the broker and asks the running gateway to reload its plugins, without
recreating `hermes`; new sessions on `default` and `browser` get the Bitwarden tools. `make vault-open` opens the web vault at `http://localhost:8222`
(`VAULTWARDEN_PORT`).

The web vault is plain HTTP on `127.0.0.1`, which a browser treats as a secure context. `bw` refuses
any server that is not HTTPS, so the broker and `bitwarden-setup` reach Vaultwarden as
`https://vaultwarden.vault.internal` through `vaultwarden-tls`, an nginx sidecar, and trust the
private CA through `NODE_EXTRA_CA_CERTS`. Your browser never sees that certificate.

The server certificate is valid for 825 days and the CA for 10 years. To renew the server
certificate, remove `server.crt` and `server.key` from `local/tls/server/`, run
`docker compose --profile bitwarden-tools run --rm bitwarden-setup tls`, which issues a new pair from
the existing CA, then `docker compose restart vaultwarden-tls`.

Vaultwarden allows 10 password logins a minute from one client IP. `make vault-init` signs in once
for the API and once for `bw`; on a 429 it waits a minute and tries once more. Re-running it several
times in a row can still hit the limit; wait a minute.

**The local Vaultwarden holds what you store in local mode, and nothing else has a copy unless you
back it up** with `scripts/vault.sh backup DIR` (see [Backup and restore](#backup-and-restore)).

Setting `BITWARDEN_LOCAL_ENABLED=0` and running `make up` stops Vaultwarden. The local account
stays, the broker reports it as `offline`, and `make doctor` notes it.

## Setup and seeding

Creating the agent's folder or collection, moving items into it and changing the agent account's
role need your own vault key, which the broker does not hold in `collection` mode. These steps run
in `bitwarden-setup`, a one-shot container from the broker's image. It listens on no port, shares
no volume with `hermes`, and shares only the internal `vault` network with it, its route to the
local Vaultwarden. It asks for your email and master password on the host terminal (three tries; after
three wrong passwords it exits with nothing changed), unlocks your account into a new folder under
`/tmp`, does its work, and runs `bw lock` and `bw logout` on every exit, Ctrl-C included. A second
Ctrl-C during that cleanup can stop `bw logout` before the server hears it, which leaves a `bw`
device session on your account until you remove it in the web vault.
`make vault-init` and `make vault-add` use it. Its output shows item names,
usernames and URIs, never a password, TOTP key or note.

| Mode and scope | Setup creates | Left to you |
|---|---|---|
| `local` | everything in [Local mode](#local-mode) | registering in the web vault |
| `cloud` or `selfhosted`, `folder:` | the folder | nothing |
| `cloud` or `selfhosted`, `collection:` | the collection in your organization, the agent member's invite, and Manage access on the collection | signing up the agent account, and creating the organization if there is none |

For a `collection:` scope your account needs admin rights in the organization. When setup cannot use
the organization API it prints the web vault steps instead.

After creating the space, setup asks which sites the agent should be able to sign in to. For each
site it lists the login items in your vault whose URI host matches, with name, username and URI, and
moves the ones you pick. **A move into an organization collection transfers the item from your
personal vault to the organization**, and every member with access to the collection can see it.
Setup says so before the first such move.

## Access requests

The agent cannot add an item to its own scope: a request to "move my bank login into your vault"
could come from a page or a message the agent reads, not from you. So it asks, and you approve
outside the chat.

### Requesting

When `browser_vault` `list` has no login for a site, the agent calls
`bitwarden_request_access(site, reason, account?)`. The broker records the request in
`broker/requests.json` and the result says it is pending and how you approve it. The result never
says whether a matching item exists outside the agent's scope. `site` must be a hostname with at
least one dot, or a URL with one (a single label such as `com` would cover every host under it);
`reason` loses control characters and is cut to 200 characters, and you always see it labelled as
the agent's text. A repeat of a pending request returns the same request. At most 10 requests are
pending at a time, and requests are dropped after 7 days. `make vault-requests` lists them.

### Approving

Move the item into the agent's folder or collection in any Bitwarden app. The broker's next sync
(every `BITWARDEN_SYNC_MINUTES`, or `make vault-sync`) picks it up and marks a pending request for
that host done. The broker runs one `make vault-sync` at a time and at most one every 30 s; a call
inside that interval gets the last result back.

### Releasing

`bitwarden_release_access(handle)` takes an item out of the agent's use on the agent's word alone,
since narrowing access is safe whoever asks. The broker adds the handle to `broker/denylist.json`
and stops listing and filling it at once. It does not move the item: it stays in the agent's folder
or collection until you move it out in a Bitwarden app, and the agent gets it back only through a
new request that you approve by moving it in again.

A denylist entry is dropped at the first sync that no longer finds the item in scope, so moving it
back into scope later lists it again. Requests and denylist entries survive broker restarts.

With a `collection:` scope, an item that was only in the agent's collection becomes an unassigned
organization item when you take it out, which only Owners and Admins see. List those, then file
them in the web vault:
`docker compose --profile bitwarden-tools run --rm bitwarden-setup list-unassigned --slug <slug> --email <you>`.

## Boundary

Boundary adds and removes accounts and reads their state through `scripts/vault.sh` and
`scripts/addon.sh`. In `--json` mode stdout carries JSON lines only; everything else goes to
stderr. No command takes the API key or master password as an argument. Add reads
`{mode, region, url, scope, unlock, client_id, client_secret, master_password}` from stdin, and
unlock reads `{master_password}`.

| Command | Output |
|---|---|
| `status --json` | One line per account (each instance file, and `local`), sorted by slug: `slug`, `mode`, `scope`, `unlock`, `state`, `error`, `retry_after`. A refused account is `file`, `slug`, `state`, `error` instead |
| `unlock --slug S --json` | `{"phase":"done"}`, or an error line; one attempt, no prompt |
| `lock --slug S --json` | `{"phase":"done"}`, or an error line |
| `sync --json` | The broker's reply on one line: `synced` (slugs), and `busy` or `reason` (`busy`, `recent`) when set |
| `requests --json` | `pending` and `recent` (below) |
| `add --json`, `remove --json` | `setup` lines, then `done` with `account` and `message` |

`state` is `unlocked`, `locked`, `offline`, `error` (passed through from the broker) or `refused`;
`locked` is a normal state for a `manual` account. `error` is `login_refused`, `server_unreachable`,
`rate_limited`, `unknown`, `not_loaded` (the running broker has not read that instance; `make up`),
`broker_down` (the broker is not running) or null. With `refused` it is `bad_file`, `bad_slug` or
`missing_client_id` (see [Instance files](#instance-files)); `file` is the instance file's name, or
`broker/local-account.json`, and `slug` is null when the file name breaks the slug rule or is
`local.json`. `retry_after` is whole seconds or null.

`requests --json` lists these fields:

- `pending[]`: `request_id`, `account`, `site`, `reason` (the agent's text), `created` (Unix seconds)

`sync` and `requests` exit 1 with a message on stderr when the broker is not running; `status`
reports `broker_down` per account and exits 0. The error lines of the other commands use the exit
codes of `addon.sh`: 2 for `bad_input`, `bad_slug`, `exists` and `not_found`, 4 for the rest
(`login_refused`, `server_unreachable`, `rate_limited`, `timeout`, `broker_down`,
`command_failed`). A `rate_limited` line carries `retryAfter` when the broker knows it.

`addon.sh connect bitwarden --instance S --json` runs `vault.sh add --json` and `disconnect` runs
`vault.sh remove --json`. `addon.sh` writes the instance file only after the hook, and the broker
needs it at the restart, so `vault.sh add` writes it first (through `scripts/lib/addon_store.py`),
with the client_id and the other secrets, restarts the broker and keeps the account only when the
broker signs in: unlocked for `stored`, locked and signed in for `manual`. Otherwise it removes
what it wrote, the instance file included, and ends with an error line; `addon.sh` then writes
nothing. After a sign-in, `addon.sh` writes the same values again. `vault.sh remove` deletes the
secrets and moves the instance file aside before it restarts the broker, then deletes it. Connect
restarts the broker, and disconnect restarts it when it is running; a restart locks the other
`manual` accounts, and the done message names them. When disconnect's restart fails, it ends with
`command_failed` and the instance file goes back, so the store still lists the account. A retried
disconnect finds the secrets gone, restarts the broker and ends with `done`, which drops the store
entry. `vault.sh` never calls `addon.sh`.

## What the agent can and cannot see

The agent drives the page the password is typed into, so code it writes can read the field. Origin
binding limits which pages that can happen on, and digest redaction removes the value and four
encodings of it from what the model sees. It does not stop page script from sending the value
elsewhere, so the agent's folder or collection should hold only logins you are willing to have used
that way. The broker cannot tell browser-harness's Chromium from a CDP endpoint that agent code
starts on the same loopback and that reports any URL it likes, so this exposure does not depend on
the agent reaching the real site; the broker checks the origin to keep fills off the wrong page, not
to keep the value from agent code.

Redaction works without the value. For each fill the broker returns, for the raw value, its
standard base64, hex, percent-encoded and reversed forms (and the same forms of the value with CR
and LF removed), each form's length, the low 20 bits of its Rabin-Karp fingerprint and an
HMAC-SHA256 under a fresh key.
The gateway replaces any matching window in tool output with `«redacted-vault-secret»`, and replaces
the whole text with `«redaction failed»` if the scrub itself fails. The entries live in gateway
memory per profile and clear with Hermes' own vault redaction values. Whoever holds an entry (code
reading gateway memory, or any caller of the broker's `/fill`) can test guesses against its HMACs
offline, so a short or guessable value is recoverable from it; keep such passwords out of the
agent's scope. The fingerprints are truncated because a full one is linear in the value's bytes
and, with the base in the entry, lets lattice reduction solve for a random password. At 20 bits
the entry alone does not determine a long random value.

Other limits, as built:

- The broker's API on `127.0.0.1:8099` has no authentication. Agent code in `hermes` can call it,
  which is why no route returns a value; `hermes-worker` and other containers cannot reach it. It
  refuses a `Host` other than `127.0.0.1:8099` or `localhost:8099`, any request with an `Origin`
  header, and a POST whose `Content-Type` is not `application/json`, so a web page in the agent's
  browser cannot call it.
- A TOTP fill's redaction entry is in the `/fill` response, so whoever gets that response can
  recover the 6-digit code from it by trying all of them. The code is valid only for its time step.
- If the broker writes the page but its `/fill` reply is lost (a timeout), no redaction entry is
  registered, and the value is not scrubbed from later output.
- A login form inside a cross-origin iframe is not found. The fill returns "No password field on
  the current page".
- Cron turns list no Bitwarden logins and refuse fills, including subagents a cron job starts with
  `delegate_task`. The `bitwarden` toolset is kept out of cron's tool list, but a job that names it
  in its own `enabled_toolsets` still gets `bitwarden_request_access` and
  `bitwarden_release_access`; the backend still lists and fills nothing there. A subagent that a
  chat starts is not a cron turn and gets the logins as the chat does. These limits cover the vault
  tools. Code the agent runs in `hermes` (`browser_exec`) can call the broker API on any profile
  and in cron, `api-server` included.
- `browser_vault` `save_login` saves into Hermes' own local vault, never into Bitwarden. Add Bitwarden
  logins in Bitwarden, or have the agent ask with `bitwarden_request_access`.
- On a cloud personal account `bw get totp` needs Premium. Without it, `browser_vault` `enter_code`
  answers that the login has no authenticator key, and the agent asks you for the code.
- `make vault-init` and `vault-add` sign in to your own account with email and master
  password. Two-step login, bitwarden.com's new-device verification and accounts set to Argon2id are
  not handled: the sign-in or the organization API calls fail, and setup prints the web vault steps
  where it can. The organization steps have been run against Vaultwarden only.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `BITWARDEN_ENABLED` | `0` | Starts the broker (profile `bitwarden`) and registers the backend on `default` and `browser` |
| `BITWARDEN_LOCAL_ENABLED` | `0` | Starts `vaultwarden` and `vaultwarden-tls` (profile `vault`). `make vault-init` sets it |
| `BITWARDEN_SYNC_MINUTES` | `10` | `bw sync` interval per unlocked account; must be above 0 or the broker does not start |
| `BITWARDEN_IDLE_LOCK_MINUTES` | `0` | Lock `manual` accounts after this long without a fill or unlock; 0 never |
| `BITWARDEN_HOST_DATA_DIR` | `./data/addons/bitwarden` | `bw/`, `broker/`, `secrets/`, `local/` and `vaultwarden/`. Not moved by `ASSISTANT_DATA_ROOT` |
| `VAULTWARDEN_PORT` | `8222` | Web vault port, bound to `127.0.0.1` |
| `VAULTWARDEN_TLS_IMAGE` | `nginx:1.30.6-alpine` | The TLS sidecar's image |
| `VAULT_OWNER_EMAIL` | unset | Your address for `make vault-init` |

`bw` 2026.10.1 is pinned in `compose/bitwarden-broker/Dockerfile` by the `BW_CLI_VERSION` build
argument and a SHA-256 per architecture; a new version needs both digests. Run `make bitwarden-test`
after a `bw` or Hermes bump; [Hermes](hermes.md#model-suitability-agentic-tool-use) lists what the two overlays anchor on.

With both flags off, the add-on starts nothing and writes nothing into Hermes profiles.

## Backup and restore

```bash
scripts/vault.sh backup ~/vault-backup-2026-11-08
scripts/vault.sh restore ~/vault-backup-2026-11-08 [--force]
```

`backup` takes an online SQLite backup of Vaultwarden's `db.sqlite3` and copies `rsa_key*`,
`config.json`, `attachments/` and `sends/`, plus `secrets/`, `local/`, `broker/local-account.json`
and the instance files, into a new, empty folder. The backup holds master passwords and API keys; keep it as safe as the vault.
`restore` needs Vaultwarden stopped (`docker compose stop vaultwarden vaultwarden-tls`) and refuses
to overwrite existing data without `--force`. With `--force` it copies the backup over what is
there; files that exist only in the current data stay.

## Linux hosts

The broker and `bitwarden-setup` run as `HERMES_UID:HERMES_GID` (default `1000:1000`), as the
email add-on's services do. On Linux those must equal your `id -u` and `id -g`: the containers then
read the instance store's `instances/`, which `scripts/lib/addon_store.py` keeps at mode 700, and
every file under `BITWARDEN_HOST_DATA_DIR` is yours. With any other `HERMES_UID`, the broker
refuses `instances/` (`bad_file`, shown by `make doctor`) and loads no instance, and `make doctor`
flags the mismatch. Docker Desktop on macOS maps bind-mount ownership to your user, so nothing here
applies there.

Earlier versions ran both as uid 10001 and gave it `bw/`, `broker/`, `local/` and `secrets/<slug>/`.
Hand them back once, before `make up`:

```bash
sudo chown -R "$(id -u):$(id -g)" data/addons/bitwarden
```

Use your `BITWARDEN_HOST_DATA_DIR` in place of `data/addons/bitwarden` if you set one. Until then,
`vault.sh` stops at the first path it cannot read or write and prints that command, and the broker
cannot read its own `bw/` data.

None of this has been run on a Linux host.

## Turning it off

Set `BITWARDEN_ENABLED=0` and run `make up`. The broker stops, and the next bootstrap takes
`bwbroker` out of each profile's `plugins.enabled` and removes `vault.bwbroker`. `hermes` is not
recreated: `make up` asks the running gateway to reload its plugins (`scripts/vault.sh
reload-plugins`), and the Bitwarden tools are gone from new sessions. If the gateway does not
answer, `make restart` applies the change. It leaves
`vault.bitwarden.enabled: false`, which it wrote while the add-on was on, so Hermes' own `bw:`
backend stays off until you remove that line. Instance files and data stay. Turn local mode off with
`BITWARDEN_LOCAL_ENABLED=0`.

## Troubleshooting

`make doctor` has a Bitwarden section: the broker's health, each account's state (`unlocked`,
`locked`, `offline`, `error` with an error class), refused accounts, a `scope` `all` warning, account
files from earlier versions, and local mode's files.

| Symptom | Cause and fix |
|---|---|
| `browser_vault` `list` shows `"bitwarden": {"unavailable": true}`; fills say "Bitwarden is unavailable right now" | The broker is not answering. `make up`; `docker compose logs bitwarden-broker` |
| `"bitwarden": {"locked": [...]}` | A manual account is locked: `make vault-unlock SLUG=…` |
| doctor: `<slug>: error (auth)` | Bitwarden refused the API key or master password: `make vault-add SLUG=… MODE=… FORCE=1` |
| doctor: `<slug>: offline (rate_limited)` | Bitwarden answered 429. The broker waits 60 s (or the server's `Retry-After`) and keeps serving the last synced items |
| doctor: `the broker refused an account (<cause>)` | See the causes under [Instance files](#instance-files); the message names the file and the setting |
| doctor: `compose/bitwarden/accounts/ holds … account file(s)` | Files from an earlier version, which nothing reads. Connect those accounts again (`make vault-add` or Boundary), then delete the folder |
| doctor: `<slug>: not loaded by the broker yet` | The broker reads the instance store at start: `docker compose --profile core --profile bitwarden up -d --no-deps --force-recreate bitwarden-broker` |
| doctor: `bitwarden-broker is not in the running hermes container's network namespace` | `hermes` was recreated and the broker was not: `docker compose --profile core --profile bitwarden up -d --no-deps bitwarden-broker` (or `./scripts/vault.sh follow-hermes`) |
| A login is in the folder but not listed | It has no `http`/`https` URI, or only URIs with match type "Never"; or the agent released it and it is still denylisted. `make vault-sync` re-reads |
| doctor: `… is a folder (Docker made it for a missing file)` | A container started before `make vault-init` wrote that file. Remove the folder and run `make vault-init` again |
| `make vault-init` stops waiting for you | Register in the web vault with the invited address, then `make vault-init` again |
