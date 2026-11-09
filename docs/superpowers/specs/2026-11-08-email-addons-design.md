# Email add-ons: `email` (IMAP) and `email-local` (Mail.app)

Design, 2026-11-08. Builds on the add-on manifest spec (`docs/private/addon-manifest-spec.md`)
and the mail design in `docs/email.md`. Goal: Boundary can list and install IMAP email from the
add-on catalog on any machine, without building or signing macOS apps, while today's Mail.app
setup keeps working unchanged.

## Decisions

- Two independent add-ons. `email-local` is today's Mail.app reader and drafts with a manifest
  added. `email` is new: several IMAP accounts, read plus drafts, all in containers.
- No SMTP. Drafts are saved by IMAP `APPEND` and the user sends them from their own client, as
  `docs/email.md` already does for IMAP accounts. An autonomous sender stays a later, separate
  add-on.
- Drafts are part of the reading add-on, behind a gate: a flag for `email-local` (today's
  `MAIL_DRAFTS_ENABLED`), a setting for `email`. Drafts don't send, so this doesn't break the
  manifest spec's read/send split.
- Two MCP servers, `mail` and `email`, instead of one search across both. A user with both
  add-ons gets two searches. Merging them would need per-index id prefixes, which break the
  integer `after_id` and watermark the triage cron relies on.
- The work is two sub-projects with separate plans: the generic manifest path first, then the
  two email add-ons.

## The two add-ons

| | `email-local` | `email` |
|---|---|---|
| id | `io.github.kaxline.stack.email-local` | `io.github.kaxline.stack.email` |
| Directory | `addons/email-local/` | `addons/email/` |
| Reads from | Mail.app (`script`, `store` sources) and the host `imap` source | IMAP servers, one instance per account |
| Main flag / profile | `MAIL_MCP_ENABLED` / `mail` (unchanged) | `EMAIL_ENABLED` / `email` |
| Drafts gate | Flag `MAIL_DRAFTS_ENABLED` / profile `mail-drafts` (unchanged) | Gate `EMAIL_DRAFTS_ENABLED` / profile `email-drafts`, set by the `drafts` setting |
| MCP names | `mail`, `drafts` (unchanged) | `email`, `email-drafts` |
| Compose services | `mail-mcp`, `mail-drafts-mcp` (unchanged) | `email-indexer`, `email-mcp`, `email-drafts-mcp`, `email-outbox` |
| Host component | `host` block: the reader and drafts apps in `macos/mail-apps/` | None |
| Index | `MAIL_INDEX_DIR` (default `data/mail`) | Named volume `email_index` |
| `permissions.reads` | `["mail"]` | `["mail"]` |
| `permissions.cron` | `false` | `false` |
| `permissions.writes` | `["external:mail-drafts"]` | `["external:mail-drafts"]` |
| `permissions.egress` | `[]` | `["internet"]`, on `email-indexer` and `email-outbox` only |
| MCP profiles | `default`, `browser`; never `api-server` | Same |

### `email-local` compatibility

Nothing an existing install sets changes: flags, profiles, MCP names, index and drafts paths,
`setup.sh --mail` and `--mail-drafts`, the cron fence and `make mail-triage-cron`. The manifest
replaces the two `MAIL_*` rows in `SERVICES` in `scripts/sync-profiles.sh` and the hand-written
mail registration in the bootstraps, and must produce the same Hermes config.

Its `host` block names `macos/mail-apps/install.sh`, the Automation → Mail grant (plus Full Disk
Access when an account uses `source = "store"`) and `make mail-index-doctor` as the check.
`addon.sh list --json` reports it with `platform: "macos"`. Boundary may show it, but installing
it still needs the macOS apps, and only built-in and official-feed add-ons may carry `host`.

The host `imap` source stays. `docs/email.md` points new IMAP users at `email`. Removing the
host source is a later decision, made after Keith's real-account check of both.

## `email` services and data flow

```
                      internet (IMAP servers)
                        ▲               ▲
              email-indexer         email-outbox          hold passwords; egress network only;
               (writes index)      (APPEND drafts)        no network shared with Hermes
                        │               ▲
            email_index volume    drafts queue (data/addons/email/drafts/)
                        ▼ ro            │
                  email-mcp        email-drafts-mcp       no passwords; no internet;
                (mail-mcp image) (mail-drafts-mcp image)  compose-internal only
                        ▲               ▲
                        └──── Hermes ───┘
```

- **`email-indexer`** runs the indexer core (`scripts/lib/mail_index.py`) with the existing IMAP
  reader (`mail_imap.py`), so scope rules, `EXAMINE` and `BODY.PEEK[]`, index ids, threading and
  deleted-text overwriting are the same code as the host source. It runs a pass every
  `interval` seconds (setting, default 900) and is the index's only writer. It also serves the
  `mailboxes` command (below).
- **`email-mcp`** is the `mail-mcp` image with `email_index` mounted read-only, registered as
  `mcp_servers.email`. Tools, the untrusted-content markers and `/watermark` are unchanged.
- **`email-drafts-mcp`** is the `mail-drafts-mcp` image over the queue, registered as
  `mcp_servers.email-drafts`. Starts only when `drafts` is on.
- **`email-outbox`** runs `mail_imap_drafts.py` against the queue with the host helper's checks
  and its 20-drafts-an-hour limit. It reads only the reply headers `docs/email.md` lists. Starts
  only when `drafts` is on.

All four keep today's container hardening: read-only root filesystem, `cap_drop: ALL`,
`no-new-privileges`, `/tmp` as tmpfs, running as `HERMES_UID:HERMES_GID`.

**Networks.** `email-mcp` and `email-drafts-mcp` join only the compose-internal network Hermes
uses. `email-indexer` and `email-outbox` join only an egress network that no other service
joins. Each reaches the volume or folder it needs, not the other services.

**Index on a named volume.** One container writes the SQLite index in WAL mode and another
reads it. Over Docker Desktop's virtiofs bind mounts that is risky, and many-small-file
workloads are slow there. The index can be rebuilt from the servers, so it isn't backed up.
Removing the add-on with purge deletes the volume.

**Proton Bridge** is reached at `host.docker.internal`, with `tls_ca` set to the certificate
Bridge exports. `security = "plain"` stays allowed only for `host.docker.internal`, localhost
and test servers.

### Accounts

Each account is an instance with these settings:

| Setting | Type | Default | Notes |
|---|---|---|---|
| `id` | string | none | The instance slug; appears in results and `reply_ref` |
| `host` | string | none | |
| `port` | port | 993 | 143 with STARTTLS |
| `security` | choice `tls`, `starttls`, `plain` | `tls` | `plain` limited as above |
| `user` | string | none | |
| `address`, `addresses` | string, list | none | From addresses for drafts |
| `mailboxes` | list | none | Nothing is indexed until set |
| `tls_ca` | secret | none | PEM text, stored as a file |
| `drafts_mailbox` | string | none | When the server doesn't flag `\Drafts` |
| `password` | secret | none | Never echoed, never returned by `list` |

Add-on-level settings, matching the indexer's top-level keys: `since` (date, no default, so
nothing is indexed until it's set), `bodies` and `attachments` (bool, default `false`),
`max_body_chars` (int, default 100000), `interval` and `drafts`.

The installer writes `data/addons/email/config.toml` in `mail-index.toml` format from the
instances, without passwords. Changing an instance rewrites it, and the indexer picks it up on
its next pass.

### Passwords

Each password is a file, `data/addons/email/secrets/<id>.password` (0600, owned by
`HERMES_UID`). The `secrets/` directory is mounted read-only into `email-indexer` and
`email-outbox` only. `scripts/lib/mail_credentials.py` gains a third source, a directory named by
`MAIL_CREDENTIALS_DIR`, beside the Keychain file descriptor and `password_env`. The config has no
password field, as today.

This relaxes the current `docs/email.md` rule that the password never reaches a container. The
new rule for `email`: the password is never in a container Hermes can reach, in `.env`, in an
environment variable, in an argument or in `docker inspect` output. `docs/email.md` will say so,
and that `email-local` with the Keychain remains the stricter option.

## Manifest path additions (sub-project 1)

Today `scripts/lib/addon_manifest.py` validates manifests and prints `mcp-plan`, the bootstraps
register MCP entries, `sync-profiles.sh` reads the `builtin` block, and the cron fence reads
`permissions.cron`. There is no `scripts/addon.sh`, and settings, instances, secrets, `host` and
commands aren't implemented. `email` needs all of them except `host`, which `email-local` uses
only for listing.

### Gated entries

`mcp` may be an object or a list of objects. Built-in manifests may declare `gates`, a list of
extra `{flag, profile, seed}` rows for `sync-profiles.sh`, and any `mcp` or `services` entry may
carry `"when": {"gate": "<flag>"}` naming one of them:

```json
"gates": [{"flag": "MAIL_DRAFTS_ENABLED", "profile": "mail-drafts", "seed": false}]
"when": {"gate": "MAIL_DRAFTS_ENABLED"}
```

Gates are flags, not settings, because compose profiles are switched by flags. A `bool` setting
can carry `"flag": "<gate>"`, and `addon.sh configure` writes its value to that flag in `.env`;
that is how `email`'s `drafts` setting turns on `EMAIL_DRAFTS_ENABLED`. An entry whose gate is off
gets no MCP registration and its service isn't started. A gated entry follows its own flag, not
the add-on's main flag, so `MAIL_DRAFTS_ENABLED` works without `MAIL_MCP_ENABLED` as it does
today. A `when` naming an unknown gate fails validation. This also answers the manifest spec's
open question about Twenty's two flags.

### Settings and instances

- `settings` as in the manifest spec. Values go in `data/addons/<id>/settings.env`.
- `instances: {"name": "accounts", "settings": {…}}` declares a per-instance settings schema.
  Each instance is `data/addons/<id>/instances/<slug>.json`. The slug matches
  `[a-z][a-z0-9-]{0,30}`.
- `secret` values, top-level or per instance, go under `data/addons/<id>/secrets/` (0600) and
  never into `settings.env`, an instance file, `list` output or logs.
- A `secret` setting is allowed only in built-in or official-feed add-ons.
- Unknown `choice` values fail closed, as the manifest spec says.

For built-ins, a `services` entry can mount the add-on's directories with
`"mounts": {"secrets": "ro", "config": "ro"}`. The compose service in `docker-compose.yml` does
the mounting. The contract test checks the manifest and compose agree.

### `commands`

A new field naming read-only commands Boundary may call:

```json
"commands": {
  "mailboxes": {"service": "indexer", "instance": true, "argv": ["mailboxes", "--json"]}
}
```

A command runs in a one-shot container from the named service with that service's env and
mounts, and prints JSON on stdout. For an add-on with a non-empty `reads`, a command's output
may hold names and counts only, never content. `mailboxes` prints the account's folder paths,
message counts and special-use flags, the same as `make mail-index-mailboxes`.

### `scripts/addon.sh`

Boundary's only interface to add-ons. Documented in `docs/extending.md` as experimental while
the stack is in 0.3.x.

| Command | Does |
|---|---|
| `list --json` | One object per add-on: the manifest merged with compose facts, whether it's enabled, its instances without secrets, and which settings are still missing |
| `configure <id> [--instance <slug>]` | Reads settings as JSON on stdin, validates them, writes `settings.env`, the instance file and secret files. Never takes a value in argv |
| `configure <id> --instance <slug> --remove` | Removes the instance and its secret files |
| `run <id> <command> [--instance <slug>]` | Runs a manifest command and prints its JSON |
| `enable <id>` | Refuses while required settings are missing. Sets the flag, runs `make up` for the add-on's profile, waits for health, re-runs the bootstraps |
| `disable <id> [--purge]` | Clears the flag, drops the MCP entries, stops the services. Keeps `data/addons/<id>/` unless `--purge`, which also removes the add-on's named volumes |

Exit codes: 0 success, 2 invalid input (the message names the setting, never its value),
3 a required setting is missing, 4 a command failed in its container.

### Validator rules

Added to `addon_manifest.py validate` and `test-addon-manifests.py`:

- `secret` settings only in built-in or official-feed manifests.
- A service that mounts `secrets` may not be the service an `mcp` entry points at.
- A `path` setting in a `reads` add-on may not have a default (already in the spec, now
  enforced).
- `commands` may not name a service that an `mcp` entry points at, for a `reads` add-on.
- `when` must name a flag listed in `gates`.

## Boundary's install flow for `email`

1. `addon.sh list --json` returns the listing, the permissions and the account settings
   schema.
2. For each account, `addon.sh configure email --instance work` with the settings and the
   password as JSON on stdin.
3. `addon.sh run email mailboxes --instance work` returns folders with counts. The user picks
   mailboxes and a date window, and Boundary calls `configure` again with them.
4. Optionally `addon.sh configure email` with `{"drafts": true}`.
5. `addon.sh enable email`. The first index pass starts once the services are healthy, and
   `mail_status` on the `email` server reports progress.

Boundary needs no other change: the flags and services it pins today are untouched.

## Testing

Everything runs against synthetic mail and GreenMail (`greenmail/standalone:2.1.3`), never a
real account.

- **`email-local` parity.** With the manifest in place, the bootstraps must write the same
  `config.yaml` and `.mail-cron-toolsets.json` as `main`, with mail and drafts each on and off.
  `test-mail-*-registration.sh`, `make mail-imap-test` and `make mail-apps-test` pass unchanged.
- **Manifest path.** `test-addon-manifests.py` covers the new fields and each refusal above. A
  new `scripts/test-addon-cli.sh` runs `configure`, `list`, `run`, `enable` and `disable`
  against a fixture add-on, and checks that a secret value appears in no `list` output, argv,
  log line or `docker inspect` output.
- **`email` end to end (`make email-test`).** GreenMail loaded with the synthetic messages, two
  instances configured through `addon.sh`, drafts on. Checks: the index matches what the host
  IMAP source builds from the same server; nothing on the server changed; a draft and a reply
  land in Drafts. Isolation checks: `email-mcp` and `email-drafts-mcp` have no secrets mount and
  no route outside the internal network; `email-indexer` and `email-outbox` can't reach Hermes
  or the MCP containers. Skips when Docker isn't running.
- **Real account.** After both sub-projects merge, Keith runs `email` against a real account
  with commands provided for him, reporting counts and pass/fail only. This sits beside the
  host IMAP first run still owed from v0.3.6.

## Rollout

Sub-project 1 ships first as a 0.3.x patch release: the new manifest fields, `addon.sh`, the
`docs/extending.md` contract and a CHANGELOG entry. Sub-project 2 follows as the next patch:
both email manifests, the four `email` services, `MAIL_CREDENTIALS_DIR`, `docs/email.md`
changes and a CHANGELOG entry that marks `email` as not tested on a real account until Keith's
check passes.

## Not in this work

- SMTP or any sending.
- Merging `mail` and `email` into one search.
- An `email` variant of `make mail-triage-cron`. It would take the server name as a parameter.
- Gmail and Microsoft Graph adapters.
- Removing the host `imap` source from `email-local`.
- Porting Messages, Twenty or LightRAG to the new fields, though gated entries and instances
  are meant to serve them.
