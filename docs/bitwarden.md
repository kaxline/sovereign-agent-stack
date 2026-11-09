# Bitwarden

Hermes signs in to websites with logins kept in Bitwarden, and `make secrets-sync` writes stack
secrets from Bitwarden into the files the stack already reads. A sidecar, `bitwarden-broker`, holds
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

Each Bitwarden account the broker signs in to is one file, `compose/bitwarden/accounts/<slug>.env`.
Several accounts can be configured at once.

| Mode | Server | Account made by | Command |
|---|---|---|---|
| `cloud` | bitwarden.com, US or EU | you | `make vault-add SLUG=personal MODE=cloud [REGION=eu]` |
| `selfhosted` | your Bitwarden or Vaultwarden at `URL` (HTTPS) | you | `make vault-add SLUG=home MODE=selfhosted URL=https://…` |
| `local` | Vaultwarden in this stack | `make vault-init` | `make vault-init` |

`make vault-add` asks for the account's personal API key (`client_id`, `client_secret`; web vault:
**Settings > Security > Keys > API key**) and, with stored unlock, the master password, in hidden
prompts on the host terminal. None of them is ever a command-line argument. It writes the account
file and `secrets/<slug>/` under `BITWARDEN_HOST_DATA_DIR`, sets `BITWARDEN_ENABLED=1`, and offers
to create the folder or collection and choose its items (see [Setup and seeding](#setup-and-seeding)).
With the broker running it restarts the broker, which reads account files only when it starts,
and reports what the broker makes of the credentials. When Bitwarden refuses the API key or master
password, it removes what it wrote (with `FORCE=1`, it puts the previous file and secrets back).
Without a running broker the check happens at the next `make up`; `make doctor` shows the result.

Other options: `SCOPE=` (default `folder:Agent`), `UNLOCK=stored|manual` (default `stored`),
`FORCE=1` to replace an existing account. `make vault-remove SLUG=…` deletes the account file,
its secrets and its `bw` data, and restarts the broker; the API key still exists in Bitwarden until
you revoke it there.

### Account files

```
BW_MODE=cloud                               # cloud | selfhosted | local
BW_SERVER=https://vault.bitwarden.com       # cloud: vault.bitwarden.com or vault.bitwarden.eu
BW_SCOPE=folder:Agent                       # folder:<name> | collection:<id> | all
BW_UNLOCK=stored                            # stored | manual
BW_CLIENTID=user.…                          # the account's personal API key id
BW_SECRETS_ITEM_SCOPE=folder:Stack secrets  # optional, see Stack secrets
```

`BW_SCOPE` defaults to `folder:Agent`. The other four keys are required. The broker accepts CRLF
line endings, spaces around keys and values, `#` comment lines and one pair of quotes around a
value. An unknown key, an unknown value (`BW_UNLOCK=auto`), a repeated key or a missing required
key refuses the whole file, and `make doctor` names the file and the key. A `#` after whitespace in
an unquoted value starts a comment, so `BW_SCOPE=folder:Team #2` reads as `folder:Team`; quote the
value (`BW_SCOPE="folder:Team #2"`). `make vault-add` quotes it for you.

Credentials never go in this file. The API key's secret and, with stored unlock, the master
password are `client_secret` and `master_password` in `secrets/<slug>/`, mode 600. Only the broker
(read-only) and `bitwarden-setup` mount that folder.

### Scope

`BW_SCOPE` decides which items the agent can reach through an account.

- `folder:<name>` works on a personal account with no organization. The broker lists and fills
  only items in that folder. Bitwarden does not enforce this: the broker holds a key that decrypts
  the whole vault.
- `collection:<id>` is for an organization. The account file then belongs to a separate member
  account (for example `agent@…`) that the organization grants only that collection, so Bitwarden
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
  and hands it to the broker over a Unix socket that only the broker and `bitwarden-secrets-sync`
  mount. The broker uses it for one `bw unlock` and drops it. The account stays unlocked until the
  broker restarts, `make vault-lock SLUG=…`, or `BITWARDEN_IDLE_LOCK_MINUTES` without a fill or
  unlock (default 0, never). While it is locked its items are not listed, and `browser_vault` `list`
  carries `"bitwarden": {"locked": ["<slug>"], "unlock": "make vault-unlock SLUG=<slug>"}`. The broker
  shares `hermes`' network namespace (`network_mode: service:hermes`). Compose does not restart it
  when `hermes` alone is recreated or restarted (`make hermes-reload`, `docker compose restart
  hermes`): Hermes then gets `broker_unavailable` until `docker compose restart bitwarden-broker` or
  `make up`, and a manual account then needs unlocking again.

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
3. Creates the organization "Assistant" with the collections "Agent" and "Stack secrets" and writes
   `compose/bitwarden/accounts/local.env` (`BW_SCOPE=collection:<Agent>`, `BW_UNLOCK=stored`,
   `BW_SECRETS_ITEM_SCOPE=collection:<Stack secrets>`).
4. Invites your address (`VAULT_OWNER_EMAIL`, or asked) as an Owner and prints the web vault's
   address. Register there with that address and a master password you choose; the stack never
   sees or stores it. Sign-ups are otherwise off.
5. Waits up to 10 minutes for you to register, then confirms you in the organization. If you have
   not registered by then it stops; `make vault-init` again picks up from this step.
6. Asks for your master password once, in `bitwarden-setup`, to change the agent account to the
   User role with Manage on "Agent" and view on "Stack secrets". From then on you own the
   organization and Bitwarden enforces the agent's scope.

Each finished step is recorded in `local/state`. `make vault-init` then sets `BITWARDEN_ENABLED=1`;
run `make up`. `make vault-open` opens the web vault at `http://localhost:8222`
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

Setting `BITWARDEN_LOCAL_ENABLED=0` and running `make up` stops Vaultwarden. `local.env` stays, the
broker reports the account as `offline`, and `make doctor` notes it.

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
`make vault-init`, `make vault-add` and `make vault-grant` use it. Its output shows item names,
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
says whether a matching item exists outside the agent's scope. `site` must be a hostname or a URL;
`reason` loses control characters and is cut to 200 characters, and you always see it labelled as
the agent's text. A repeat of a pending request returns the same request. At most 10 requests are
pending at a time, and requests are dropped after 7 days. `make vault-requests` lists them.

### Approving

- `make vault-grant` lists pending requests. For the one you pick, `bitwarden-setup` asks for your
  unlock, lists the matching items, and moves the one you choose. The broker then syncs and marks
  the request done once the item is in scope. `make vault-grant SITE=example.com` skips the list.
- In Bitwarden: move the item into the folder or collection in any Bitwarden app. The broker's
  next sync (every `BITWARDEN_SYNC_MINUTES`, or `make vault-sync`) picks it up and marks a pending
  request for that host done.

### Releasing

`bitwarden_release_access(handle)` takes an item out of the agent's scope on the agent's word alone,
since narrowing access is safe whoever asks. The broker adds the handle to `broker/denylist.json`
and stops listing and filling it at once. Then it returns the item:

- `folder:` scope: the item's folder becomes none.
- `collection:` scope: the item leaves the agent's collection and keeps any other collections it is
  in. When that was its only collection, it becomes an unassigned organization item, which only
  Owners and Admins see. List those, then file them in the web vault:
  `docker compose --profile bitwarden-tools run --rm bitwarden-setup list-unassigned --slug <slug> --email <you>`.

The result's `returned` is false when Bitwarden refused the edit. The item then stays in the agent's
folder or collection but stays denylisted; move it out in Bitwarden. A denylist entry is dropped at
the first sync that no longer finds the item in scope, so granting it again later lists it again.
Requests and denylist entries survive broker restarts.

## Stack secrets

`make secrets-sync` runs `bitwarden-secrets-sync`, which reads items from the broker over the Unix
socket and writes them into secret files the stack already reads. It serves only items in an
account's `BW_SECRETS_ITEM_SCOPE` (`folder:<name>` or `collection:<id>`); local mode sets it to
"Stack secrets". Each login item maps by its exact name, and its password field is the value:

| Item name | Written to |
|---|---|
| `twenty/<slug>/key-ro`, `twenty/<slug>/key-rw` | `TWENTY_KEY_RO` / `TWENTY_KEY_RW` in `compose/twenty/workspaces/<slug>.env` |
| `twenty/encryption-key` | the file `TWENTY_ENCRYPTION_KEY_FILE` names |
| `twenty/db-password` | the file `TWENTY_DB_PASSWORD_FILE` names |
| `postgres/<role>` | `DB_SERVICE_PASSWORD` in `compose/postgres/services/<role>.env`, and the same password inside `DATABASE_URL` and any `*_DATABASE_PASSWORD` line that held the old one |
| `caldav/<slug>` | `CALDAV_PASSWORD` in `compose/caldav-mcp/accounts/<slug>.env` |

The two Twenty files must sit directly in `TWENTY_SECRETS_DIR`, which compose mounts for the sync;
with it unset, those items are skipped with a note. No row covers `llm-proxy` keys.

A file is rewritten only when its value changed, through a temp file, at mode 600. A file that does
not exist is never created, a symbolic link is skipped, an unknown item name is reported and
skipped, and a name that maps to two files is refused. The output names files and items, never
values. When a file Hermes reads changed (Twenty workspaces, CalDAV accounts), the command runs
`make hermes-reload`. `make up` runs the sync after `docker compose up -d` when an account has
`BW_SECRETS_ITEM_SCOPE` and is unlocked; otherwise it prints that the sync was skipped and the files
keep their values.

The `KEY=value` files are read by compose's `env_file`, shell scripts and Python parsers, which
quote differently. A value bound for one of them is refused (skipped with a note naming the item)
when it holds `$`, a backtick, `\`, `'`, `"`, `#`, whitespace at either end, or a control character.
Generated keys and passwords are normally alphanumeric; change such a value, or keep that file by
hand. The two Twenty files take any value.

A new value in a file does not always change the service that checks it:

- `twenty/<slug>/key-ro`, `key-rw`: create the key in Twenty first, then store it. Hermes picks
  it up through the `make hermes-reload` the sync runs. `make crm-init ROTATE=1` writes keys of its
  own, so update the Bitwarden items after one, or the next sync puts the old keys back.
- `twenty/encryption-key`: don't change it on an existing install. Twenty encrypts what it
  stores with it, and a new key loses that data ([CRM](crm.md#settings)).
- `twenty/db-password`: Postgres reads its password file only when it first creates the
  database. Change the role's password first (`docker compose exec twenty-db psql -U twenty`, then
  `\password twenty`), run the sync, then `docker compose up -d --force-recreate twenty-server twenty-worker`.
- `postgres/<role>`: `db-provision` sets each `svc_<role>` password from its file on every
  `make up`; run it now with `docker compose run --rm db-provision`, then restart the integration
  that uses the role.

Values Hermes needs in profile `.env` files (CalDAV passwords for MCP headers) still end up there,
with the caveat in [SECURITY.md](../SECURITY.md).

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
  which is why no route returns a value; `hermes-worker` and other containers cannot reach it.
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
- `make vault-init`, `vault-add` and `vault-grant` sign in to your own account with email and master
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
| `TWENTY_SECRETS_DIR` | unset | The folder holding both Twenty secret files, for `make secrets-sync` |

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
`config.json`, `attachments/` and `sends/`, plus `secrets/`, `local/` and the account files, into a
new, empty folder. The backup holds master passwords and API keys; keep it as safe as the vault.
`restore` needs Vaultwarden stopped (`docker compose stop vaultwarden vaultwarden-tls`) and refuses
to overwrite existing data without `--force`. With `--force` it copies the backup over what is
there; files that exist only in the current data stay.

## Linux hosts

The broker and `bitwarden-setup` run as uid 10001. Docker Desktop on macOS maps bind-mount
ownership to your user, so nothing here applies there. On Linux:

- `make vault-add` and `make vault-init` create the data folders before any container starts. Run
  as root they give the new folders to uid 10001; otherwise they print the
  `sudo chown -R 10001:10001 …` command to run. `secrets/` and the accounts folder stay yours at
  mode 711.
- When `vault.sh` cannot read or write a file the containers own, it goes through
  `bitwarden-setup` and prints a note naming the path.
- `bitwarden-setup` writes `local.env` (vault-init) and rewrites `BW_SCOPE` (setup creating a new
  collection) inside the accounts folder, which uid 10001 cannot write while it is yours at 711.
  Before those steps, give it access, for example
  `sudo setfacl -m u:10001:rwx compose/bitwarden/accounts`, or run them as root. Otherwise they stop
  with a permission error.
- `make secrets-sync` runs as uid 10001 too. It needs write access to the target folders, and a file
  it replaces may end up owned by uid 10001 (it prints a note when it could not keep the owner).
  Check that the service reading the file can still read it.

None of this has been run on a Linux host.

## Turning it off

Set `BITWARDEN_ENABLED=0` and run `make up`. The broker stops, and the next bootstrap takes
`bwbroker` out of each profile's `plugins.enabled` and removes `vault.bwbroker`. It leaves
`vault.bitwarden.enabled: false`, which it wrote while the add-on was on, so Hermes' own `bw:`
backend stays off until you remove that line. Account files and data stay. Turn local mode off with
`BITWARDEN_LOCAL_ENABLED=0`.

## Troubleshooting

`make doctor` has a Bitwarden section: the broker's health, each account's state (`unlocked`,
`locked`, `offline`, `error` with an error class), refused account files, a `BW_SCOPE=all` warning,
and local mode's files.

| Symptom | Cause and fix |
|---|---|
| `browser_vault` `list` shows `"bitwarden": {"unavailable": true}`; fills say "Bitwarden is unavailable right now" | The broker is not answering. `make up`; `docker compose logs bitwarden-broker` |
| `"bitwarden": {"locked": [...]}` | A manual account is locked: `make vault-unlock SLUG=…` |
| doctor: `<slug>: error (auth)` | Bitwarden refused the API key or master password: `make vault-add SLUG=… MODE=… FORCE=1` |
| doctor: `<slug>: offline (rate_limited)` | Bitwarden answered 429. The broker waits 60 s (or the server's `Retry-After`) and keeps serving the last synced items |
| doctor: `the broker refused an account file` | A key or value in the file is unknown; the message names the file and key |
| doctor: `<slug>: not loaded by the broker yet` | The broker reads account files at start: `docker compose restart bitwarden-broker` |
| A login is in the folder but not listed | It has no `http`/`https` URI, or only URIs with match type "Never"; or the agent released it and it is still denylisted. `make vault-sync` re-reads |
| doctor: `… is a folder (Docker made it for a missing file)` | A container started before `make vault-init` wrote that file. Remove the folder and run `make vault-init` again |
| `make vault-init` stops waiting for you | Register in the web vault with the invited address, then `make vault-init` again |
