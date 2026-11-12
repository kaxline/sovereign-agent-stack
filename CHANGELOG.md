# Changelog

Each release lists what a downstream install needs to know before moving its
pin: new services, required `.env` keys, new host requirements and upgrade
steps. Pin a tag, not a branch. See [docs/releasing.md](docs/releasing.md) for
how releases are cut and [docs/extending.md](docs/extending.md) for running
the stack under another app.

## v0.3.14 — 2026-11-11

Not run on a live stack yet: the Bitwarden broker reading the instance store
(on Linux in particular), Actual budgets under per-instance `bind`, and the
detector add-on with a real provider key. Every test for them uses stubs.

### Upgrade steps

1. **Without `make`** (Boundary's start path, or a plain `docker compose
   up`): run `./scripts/addon-dirs.sh` before compose, in place of v0.3.13's
   `./scripts/crm.sh ensure` and `./scripts/budget.sh ensure`. It creates
   `<data root>/addons/<id>/{config,instances,secrets}` (0700) for every
   add-on, enabled or not, so the Hermes bootstraps' bind mounts of
   `addons/twenty` and `addons/actual/instances` find real directories. `make
   ensure-local` and `setup.sh` run it. The two `ensure` commands still work.
2. `compose/twenty/workspaces/<slug>.env` files are no longer read. Delete any
   left over, since they hold API keys, and add remote workspaces again with
   `make crm-add`. Boundary: stop reading and writing that folder before
   moving the pin.
3. LightRAG is registered on the default profile too, read-only with the same
   five tools as on `api-server` and `browser`. After the next bootstrap run
   with `LIGHTRAG_MCP_ENABLED=1`, any other entry at LightRAG's URL on
   default (a hand-made `lightrag-mcp`, for example) is removed. With the flag
   off, entries at that URL are removed from default as well. To keep
   LightRAG out of chat, run `./scripts/addon.sh disable lightrag`.
4. Add-on manifests outside this repo that use a removed field (below) fail
   validation and are skipped with a warning until the field is taken out.
5. Bitwarden: the secret files the stack secrets sync wrote (Twenty keys,
   Postgres service passwords, CalDAV passwords) stay where they are; edit them
   by hand from now on. Delete `TWENTY_SECRETS_DIR` from `.env`. Boundary
   never called the sync, so it needs no change.
6. Bitwarden: account files (`compose/bitwarden/accounts/<slug>.env`) are no
   longer read, and nothing migrates them. Connect each account again with
   `make vault-add FORCE=1` (the store already lists it, so plain
   `make vault-add` refuses with "exists") or from Boundary (disconnect it
   first if Boundary still lists it), then delete the folder; `make doctor` warns while it holds
   `.env` files. Accounts connected before this release are refused with
   `missing_client_id` until connected again. Local mode: run `make
   vault-init` again; it writes the local account from `local/state` without
   signing in. Boundary: `status --json` can now report `bad_slug` and
   `missing_client_id` as the `error` of a `refused` line.
7. Bitwarden on Linux: the broker and `bitwarden-setup` now run as
   `HERMES_UID:HERMES_GID`, which must equal your `id -u` and `id -g`. Before
   `make up`, give the Bitwarden data folder back to your user once:

   ```bash
   sudo chown -R "$(id -u):$(id -g)" data/addons/bitwarden
   ```

   with your `BITWARDEN_HOST_DATA_DIR` in place of `data/addons/bitwarden` if
   you set one. macOS (Docker Desktop) needs nothing. The old
   `<project>_bitwarden_sync` volume is unused; `docker volume rm` removes it.
8. Actual Budget: the single budget is gone, with no migration.
   `ACTUAL_BUDGET_FILE`, `ACTUAL_ACCESS` and `compose/actual/budget.env` are no
   longer read. Delete them, and a `budget` entry left in a Hermes profile's
   `mcp_servers`, and connect the budget again with `make budget-connect`.
   Connected budgets keep working, but each becomes read-only on every profile
   until `./scripts/budget.sh access <slug> write` gives it a `bind`. Their old
   `access` and `profile` fields in `instances/<slug>.json` are ignored.
   **Boundary must change before moving the pin:** connect no longer accepts
   `profile` or `access` (send `bind`, or call `budget.sh access` afterwards),
   `budget.sh access` and connect report `mcp: ["budget-<slug>"]` instead of
   `budget-<slug>-ro` and `budget-<slug>`, and a row's write access is read
   from `bind`.
9. Image pins moved for security fixes (see Changes). If `.env` sets
   `N8N_IMAGE`, `VIKUNJA_IMAGE` or `MCP_SEARXNG_IMAGE`, update it or delete
   the line. n8n 2.47.4 and Vikunja 2.8.0 migrate their databases on first
   start, forward only, so back up first: copy n8n's directory as in
   v0.3.3's step 13 (`docker compose --profile automation cp
   n8n:/home/node/.n8n ./n8n-before-2.47.4`), and run `./scripts/db.sh
   backup` for Vikunja's schema in the shared database.
   Existing Vikunja MCP tokens keep working, because Vikunja renames their
   stored permission keys during the upgrade. With `calendar` enabled, run
   `docker compose --profile calendar build caldav-mcp` before `up`.
10. Hermes Agent v0.23.1 (from v0.22.7); WebUI stays at 0.52.544, still the
   latest stable release. Back up `data/hermes` with `hermes` stopped, then run
   `make hermes-upgrade AGENT=v0.23.1 WEBUI=0.52.544` and the verification
   block in [docs/hermes-webui.md](docs/hermes-webui.md#verification). The
   first start migrates each profile's `config.yaml` from `_config_version`
   52 to 53; the bootstraps write nothing else different. The overlays still
   apply to v0.22.7, so an app that pins `HERMES_AGENT_IMAGE_TAG=v0.22.7`
   keeps working on this release. The stack never calls `hermes mcp serve`
   (now the `mcp-serve` plugin), names none of the merged Office skills, and
   leaves `plugins.auto_apply` at its default (off).
   Every `hermes` CLI call in the v0.23.1 container prints `install out of
   sync (pnpm: not installed or outdated)`. It comes from the upstream image's
   package check; the gateways, dashboard and WebUI ran normally with it.

### New services

- `detector` and `detector-mcp`, the AI text detector add-on (Boundary CR
  0030 R1–R2, [docs/detector.md](docs/detector.md)). Off by default; turn it
  on with `./scripts/addon.sh enable detector`. `detector` sends checked text
  to Pangram or GPTZero under the user's key and publishes
  `127.0.0.1:${DETECTOR_PORT:-8790}` for `POST /check`, which needs the bearer
  token in `data/addons/detector/secrets/http_token` (written by
  `enable`). The token is not in `.env`, so Hermes profiles never see it.
  `detector-mcp` starts only with the add-on's agent setting
  (`DETECTOR_AGENT_ENABLED`) and offers `check_text` on the default and
  browser profiles, never on `api-server` or in cron. `hermes` joins the new
  internal `detector-agent` network. Neither provider adapter has run against
  the real API; GPTZero's field names come from third-party descriptions.

### New optional `.env` keys

- `DETECTOR_ENABLED`, `DETECTOR_AGENT_ENABLED` (set by `addon.sh`) and
  `DETECTOR_PORT` (default 8790).

### Changes

- Hermes v0.23.1 overlay fixes. Three anchors moved, and each patch now
  accepts both the v0.22.7 and the v0.23 shape. `patch-kanban-wake`:
  `_kb_poll_board` gained upstream's stray-delegate reclaim arguments.
  `patch-bitwarden-broker-fill`: `_fill_bound_page` takes `with_identifier`.
  On v0.23's `target='preview'` (Hermes Desktop's in-app browser) a
  `bwbroker:` fill refuses with `supervisor_required` rather than write into
  the managed browser's tab. `patch-intent-ack`: v0.23 already ignores tool
  results from earlier turns (#69778), so on v0.23 the overlay only raises
  the reply limit from 1,200 to 4,000 characters for a reply that opens with
  an ack. New `scripts/test-intent-ack.py`; until now that overlay had no
  test and failed only at container start, with a warning.
- Security pin bumps:
  - **n8n** 2.43.7 → 2.47.4, past the 2026-10-28 advisories fixed in 2.45.5
    and 2.46.1 (two of them high: workflow expression isolation, the
    GraphicsMagick config). Since 2.44, n8n refuses to create or update
    workflows that use deprecated nodes.
  - **Vikunja** 2.7.0 → 2.8.0, which carries the 41 security fixes from 2.7.1
    and 19 from 2.8.0. `todo.sh` now looks up bucket permission keys in
    `/api/v1/routes`, because 2.7.1 renamed them and 2.8.0 rejects the old
    names when minting tokens. `views_buckets_tasks` changed meaning: it was
    the POST that moves a task and is now the GET that lists bucket tasks.
    Checked against throwaway 2.7.0 and 2.8.0 servers, including tokens
    minted on 2.7.0 and then upgraded.
  - **Vaultwarden** 1.37.4 → 1.37.5 (digest-pinned), for two medium
    advisories (website icons, which the stack turns off, and emergency
    access). 1.37.5 refuses to start on invalid config values.
  - **mcp-searxng** 2.7.0 → 2.10.1. Images before 2.8.1 bundle a Node runtime
    with a high-severity Brotli decoder advisory. The later releases only add
    options.
  - **caldav-mcp** 0.1.1 → 0.2.1 (`gelse/caldav-mcp` at `d95eaec`). Fixes
    `caldav_update_event` failing with "Internal error" when it changes start
    or end. 0.2.0's breaking credential change applies only when `CALDAV_URL`
    is set on the server, and the stack sends credentials as headers.
- Manifests take an optional `store` block (Boundary CR 0027 R4). Its one
  field, `store.dataLeaves`, says what leaves this Mac and where it goes, and
  `validate` requires it when `permissions.egress` includes `internet`. Every
  built-in add-on with `internet` has one. `addon.sh list --json` now returns
  `description` and `store` on each entry (R5), `store` as `{}` when a
  manifest has none. R1–R3 (category, keywords, example questions) are not
  done.
- The `email` add-on has now had its first run on a real IMAP account
  (INBOX, a ten-day `since` window): `run email mailboxes` listed the
  mailboxes and the first pass indexed every message in scope with none
  unreadable or failed. The "not tested on a real account" note under
  v0.3.13 and "IMAP login and TLS" under v0.3.6 no longer apply. Drafts
  (`APPEND` to a real Drafts mailbox) and Proton through Bridge still
  haven't had a real run.
- Boundary's dashboard contract has now run on a live stack under
  `multiplex`: over `/api/ws` (9119), `session.create` and `prompt.submit`
  ran turns to `message.complete`, `turn.heartbeat` arrived every 30 s
  during a two-and-a-half-minute turn and stopped at `message.complete`,
  and `session.live_status` reported `working` mid-turn and `idle` after.
  The "Boundary under `multiplex`" note under v0.3.6 no longer applies,
  apart from the upstream `.env` read on the event loop it describes,
  which v0.22.7 still has. The dashboard's own Chat page runs the terminal
  UI over `/api/pty` and never receives turn events on `/api/ws`, so test
  this contract with a client of `/api/ws`, not by watching that page.
- `addon.sh enable`, `run` and hooks that run in a compose service now run
  `docker compose build --quiet` on their services first. Before, a service
  built from this repo (`email-indexer`, `email-mcp`, `email-drafts-mcp`,
  `email-outbox`, among others) kept running whatever image was built first,
  so an install that had built it before an update ran old code: `run email
  mailboxes` failed with ``accounts[0] needs a non-empty `mailboxes` list``
  on an account without mailboxes. An unchanged build is a cache hit. A
  failed build is `compose_failed` at step `start` (`enable`) or exit 4
  (`run`); the step names and their order are unchanged.
- Per-instance binding: an instance file may hold `bind`, a map from Hermes
  profile to `ro` or `rw` that replaces the manifest's `mcp.profiles` for that
  instance. `addon.sh connect` and `configure --instance` take it in the JSON
  on stdin, `check-instance` validates it, and `list --json` shows it. `rw` is
  refused on `api-server`, and an add-on that reads personal data may not name
  `api-server`.
- `mcp.instances` URLs may use `{access}` (`ro` or `rw`, the profile's level)
  and `{slug}`; `auth` and `access` are optional. An entry without `auth` is
  registered with its tool list, less `writeTools` at `ro`. instances-plan
  lines gain a thirteenth field, `tools`. With `permissions.cron` `"ro"`, each
  instance's `rw` URL is fenced out of cron.
- Actual Budget registers through `mcp.instances`: one `budget-<slug>` entry
  per budget and profile, at `actual-mcp-ro` or `actual-mcp-rw` by level.
  Budgets now reach `api-server` read-only, as Twenty's workspaces do. The
  read/write server serves a budget only when its `bind` gives some profile
  `rw`. A budget on the local server stays registered while
  `ACTUAL_LOCAL_ENABLED` is off; its calls fail until the server is back.
  `scripts/lib/actual_rows.py` and the bootstrap's Actual row code are gone.
- Fixed: on Linux, `addon.sh enable` checked `HERMES_UID` and `HERMES_GID`,
  and created the add-on's store directories, only for add-ons whose
  manifest declares `mounts`. It now does both whenever an add-on service
  runs as `HERMES_UID` in `docker-compose.yml`, which covers Bitwarden's
  broker and email-local's servers. `plan-enable` reports those services as
  `hermesUser`.
- Fixed: `addon.sh connect bitwarden --force` failed, because `vault.sh add`
  refused the `--replace` the hook gets. `vault.sh add --replace --json` now
  replaces the account, keeping the old instance file and secrets as backups
  until the new key signs in and putting them back if it doesn't.
- Removed the `mcp.service` `{ro, rw}` map and `mcp.env.access`, whose only
  user was Actual's single budget.
- `budget.sh`: removed `init` without `--server-only` (and `--rotate`), `add`,
  `check`, `remove` and `access read|write` without a slug, with `make
  budget-init`, `budget-add` and `budget-remove`. `make budget-access` is now
  `SLUG=<slug> ACCESS=read|write`. `status --json` lines drop `profile` and the
  `bad_access` state; `access` is derived from `bind`. `open` serves the
  bundled server only. The actual-mcp containers no longer mount
  `/app/.env`, and `/mcp`, `/sse` and `/messages` on them answer 404.

- Removed manifest fields, unused by any built-in add-on or replaced:
  `onToggle` (and `addon.sh`'s `recreate` step), `services.<name>.volumes`
  (use `data.volumes`), `services.<name>.oneshot`, `mcp.unmanaged`, and the
  `upgrade`, `backup` and `restore` hooks (`hooks.init` stays). A manifest
  that still has one is refused with a message naming the field.
  `addon_cli.py toggle-script` is gone.
- A service is now a one-shot when another compose service depends on it
  with `condition: service_completed_successfully` in `docker-compose.yml`;
  `addon.sh enable` and `remove` read that instead of the manifest.
  `addon.sh list --json` still reports `oneshot` per service. n8n's
  `n8n-import` is the only add-on one-shot.
- The validator accepts built-in manifests only: the packaged-add-on rules
  (image digests, `official` and `community` feeds and their refusals) are
  gone from the code and stay in the spec as design. `validate()` no longer
  takes a source argument.
- `addon.sh list --json`'s `hooks` no longer lists `backup` or `restore`.
  The `done` line's `restart` is always `[]`.
- The email-imap image (`email-indexer`, `email-outbox`) builds from
  `compose/email-imap/` instead of the repo root, and imports nothing from
  `scripts/lib`. The mail library moved from `scripts/lib/` to
  `compose/email-imap/lib/` (`mail_*.py`, `mail_*.js`, `jxa.py`,
  `email_addon.py`), and `mail-index.py` and `mail-drafts.py` to
  `compose/email-imap/`. `scripts/mail-index.py` and `scripts/mail-drafts.py`
  still work and run the moved files. Paths inside the image are unchanged.
  A compose override that sets `build.dockerfile:
  compose/email-imap/Dockerfile` for these services must drop it.

- New `scripts/addon-dirs.sh` creates every add-on's store directories; no
  add-on script has to run before core starts.
- Removed `crm.sh migrate`, the workspace-file import that `crm.sh ensure`,
  `init`, `add`, `remove`, `access`, `login-link` and `open` ran, and `make
  doctor`'s warnings about leftover workspace files.
  `compose/twenty/workspace.env.example` and `compose/twenty/workspaces/` are
  gone from the repo; the folder stays gitignored.
- Removed the Bitwarden stack secrets sync: `make secrets-sync`,
  `vault.sh secrets-sync`, the `bitwarden-secrets-sync` service and the
  broker's `GET /secrets` route on its Unix socket (the socket still takes
  manual unlocks). `make up` no longer runs a sync. Local mode no longer
  creates the "Stack secrets" collection; existing ones stay in Vaultwarden.
  `TWENTY_SECRETS_DIR` and
  `compose/twenty/no-secrets/` are gone. The Bitwarden manifest drops
  `hooks.backup` and `hooks.restore`; `vault.sh backup` and `restore` are
  unchanged.
- The Bitwarden broker reads its accounts from the add-on instance store,
  `<data root>/addons/bitwarden/instances/<slug>.json`, mounted read-only at
  `/store/instances` (`BITWARDEN_INSTANCES_DIR`), with each API key's
  `client_id` in `secrets/<slug>/client_id`. The slug rule is the store's in
  the broker, `bitwarden-setup` and `vault.sh`. `vault.sh add` writes the
  instance file through `scripts/lib/addon_store.py` before it restarts the
  broker and removes it on a failed sign-in; `remove` deletes it before the
  restart. Terminal add and remove no longer call `addon.sh configure`, and
  `make up` no longer records account files in the store. The local account
  is `broker/local-account.json` plus `secrets/local/client_id`, outside the
  store. Removed: the account-file parser, `BITWARDEN_ACCOUNTS_DIR`,
  `VAULT_ACCOUNTS_DIR`, `VAULT_ADDON_SH`, `VAULT_ADDON_STORE`, the
  `compose/bitwarden/accounts` mounts and `make doctor`'s
  `BW_SECRETS_ITEM_SCOPE` warning. `vault.sh backup` copies the instance
  files and the local account in place of the account files.
- `bitwarden-broker` and `bitwarden-setup` run as `HERMES_UID:HERMES_GID`
  instead of uid 10001, so on Linux they read the store's 0700 `instances/`
  as the host user does. The manual-unlock socket's folder,
  `/run/bitwarden-sync`, is a tmpfs owned by that user; the `bitwarden_sync`
  volume is gone. `vault.sh` no longer reaches files through
  `bitwarden-setup` or prints `chown 10001` hints: a path it cannot use stops
  it with the chown below.

## v0.3.13 — 2026-11-10

Every add-on manifest field added in this release is experimental: `gates`, `commands`, `mounts`, `instances` and `mcp.instances`,
`mcp.unmanaged`, the lifecycle fields (`services.<name>.oneshot`, `hooks`,
`instances.connect`, `instances.disconnect`, `instances.errors`,
`instances.optional`, `transient`, `onToggle`, `data`, `backup`), and the
`result` and `retryAfter` fields of hook lines. A later 0.3.x patch may
rename or reshape them.

### Upgrade steps

1. `make up`. It recreates `hermes` once on every install, whether or not
   you use the new `email` add-on: `hermes` joins the internal `email`
   network, and `BITWARDEN_ENABLED` left its environment (see "Turning
   Bitwarden on or off" under Changes). The `email` network stays empty while
   the add-on is off. Both Hermes bootstraps re-run once, because their
   environment gains the `EMAIL_*`, `LIGHTRAG_MCP_*TIMEOUT`,
   `TWENTY_MCP_*TIMEOUT`, `ACTUAL_BUDGET_FILE` and `ACTUAL_LOCAL_ENABLED` keys
   and new mounts. The same run:
   - creates `data/addons/actual/{instances,secrets}` and
     `data/addons/twenty/{instances,secrets}` (0700) through `make
     ensure-local`;
   - imports any `compose/twenty/workspaces/<slug>.env` into
     `data/addons/twenty/` and renames it `<slug>.env.migrated` (step 4);
   - records accounts added with `vault.sh add` before this release in the
     add-on store, so Boundary can disconnect them. An account whose slug does
     not start with a letter or is longer than 31 characters stays out of the
     store, and `make up` says so;
   - runs `./scripts/vault.sh reload-plugins`.
2. With Bitwarden enabled, rebuild the broker image, since `make up` and
   `addon.sh enable` don't rebuild it: `docker compose --profile core
   --profile bitwarden build bitwarden-broker`, then `./scripts/vault.sh
   follow-hermes`. An image built before 2026-11-09 has no `broker.cli`, and
   every `vault.sh` command that asks the broker for account state fails with
   it.
3. With Actual enabled, rebuild the locally built MCP image, since `make up`
   doesn't rebuild it: `docker compose up -d --build actual-mcp-rw
   actual-mcp-ro`. The old image has no session-token sign-in and no
   launcher, so connected budgets get no MCP server until it is rebuilt.
4. Twenty's workspaces move to the add-on store:
   1. `make up` (or `./scripts/crm.sh migrate`) imports the workspace files.
      Values lose surrounding whitespace, and of a repeated key the last line
      counts, as the old bootstrap read it. A file with an invalid name,
      whitespace inside a key or a URL with `user:password@` is skipped with a
      message and stays in place; fix it and run `./scripts/crm.sh migrate`
      again. `make doctor` lists the files still waiting, and `crm.sh init`
      refuses while one for its slug, or one on the local instance, is left.
      If `crm.sh ensure` warns that it can't set the store to 0700, `chown` it
      to your user as the warning says.
   2. `make hermes-reload` (a bootstrap run) rewrites the profile `.env`
      lines: `TWENTY_<SLUG>_KEY` becomes `CRM_<SLUG>_KEY`.
   3. With Bitwarden's `twenty/<slug>/key-ro` and `key-rw` items, run `make
      secrets-sync` after the migration; the items now update the files in
      `data/addons/twenty/secrets/`, not the old workspace files.
5. **Without `make`** (Boundary's start path, or a plain `docker compose
   up`):
   - Run `./scripts/crm.sh ensure` and `./scripts/budget.sh ensure` (or `make
     ensure-local`) before compose. Until then Hermes registers no `crm-*`
     entry, the Twenty workspace files are not migrated, and on Linux Docker
     creates the missing bind-mount directories owned by root.
   - After running the bootstraps, run `./scripts/vault.sh reload-plugins`.
   - After recreating `hermes` with `BITWARDEN_ENABLED=1`, run
     `./scripts/vault.sh follow-hermes`, as for any recreate.
6. A checkout used as the compose project directory needs
   `scripts/lib/actual_rows.py` (mounted by both bootstraps),
   `scripts/generate-n8n-credentials.js` (mounted by `n8n-import`) and
   `compose/email-imap/` (the build context of the email add-on's image).
7. Compose overlays: the bootstraps' `/bootstrap/twenty-workspaces` mount and
   `TWENTY_WORKSPACES_DIR` are gone, replaced by
   `/bootstrap/addon-data/twenty` and `ADDON_DATA_DIR`.
   `bitwarden-secrets-sync`'s `/targets/twenty-workspaces` became
   `/targets/twenty-keys`. An overlay that moved the old paths now adds a
   mount nothing reads; point it at the new container path. No path in the
   stable list in [docs/extending.md](docs/extending.md#add-or-change-mounts)
   changed.
8. A hand-added MCP key on the default profile (or on browser) at the GPT
   Researcher or Actual Budget URL is removed on the next bootstrap while that
   add-on is enabled. The stack's own keys stay: `gptr`, and `budget` while
   `ACTUAL_BUDGET_FILE` names a budget file (without one, `budget` is dropped
   too). Connected budgets' `budget-<slug>` entries are on their own URLs and
   are not affected. Point your key at a different URL, or use the stack's
   key. See "Other MCP keys at an add-on's URL are dropped" under Changes.
9. `LIGHTRAG_MCP_TIMEOUT`, `LIGHTRAG_MCP_CONNECT_TIMEOUT`,
   `TWENTY_MCP_TIMEOUT` and `TWENTY_MCP_CONNECT_TIMEOUT` set in `.env` now
   take effect. Compose did not pass them to the bootstraps before, so the
   defaults (120 and 30 seconds for LightRAG, 60 and 30 for Twenty) applied
   whatever `.env` said. Check any values you set.
10. **Boundary:** Bitwarden access requests are approved by moving the login
    into the agent's folder or collection in a Bitwarden app; `make
    vault-grant`, `vault.sh grant` and `bitwarden-setup grant` are gone. The
    release reply drops `returned`, which v0.3.12 shipped, and is now
    `{"released": true}`. See "`make vault-grant` and `vault.sh grant` are
    gone" under Changes.

### New services

All are off by default. `scripts/addon.sh enable email` turns them on; see
[docs/email.md](docs/email.md).

- `email-indexer` and `email-mcp` (profile `email`, flag `EMAIL_ENABLED`):
  index IMAP accounts read-only and serve the index over MCP as `email`.
- `email-outbox` and `email-drafts-mcp` (profile `email-drafts`, flag
  `EMAIL_DRAFTS_ENABLED`): queue drafts over MCP as `email-drafts` and save
  them to each account's Drafts mailbox over IMAP. Nothing is sent.

`email-indexer` and `email-outbox` build the image
`${IMAGE_PREFIX:-assistant}-email-imap:local` from
`compose/email-imap/Dockerfile` on first use; `email-mcp` and
`email-drafts-mcp` reuse the `mail-mcp` and `mail-drafts-mcp` images. New
networks: `email` (internal; `hermes`, `email-mcp`, `email-drafts-mcp`) and
`email-egress` (`email-indexer`, `email-outbox`). New volumes: `email_index`
and `email_drafts`.

The new compose profile `crm-mcp` selects no service; `TWENTY_MCP_ENABLED=1`
adds it to `COMPOSE_PROFILES`.

### New optional `.env` keys

Defaults keep the previous behaviour. `setup.sh` fills in none of them.

- `EMAIL_ENABLED=0`, `EMAIL_DRAFTS_ENABLED=0` (commented out in
  `.env.example`): start the `email` and `email-drafts` profiles.
  `scripts/addon.sh enable email` and the add-on's `drafts` setting set
  them; accounts, settings and passwords live under `data/addons/email/`.
- `TWENTY_MCP_TIMEOUT=60`, `TWENTY_MCP_CONNECT_TIMEOUT=30` (no line in
  `.env.example`): seconds Hermes waits for a call and for the first
  connection on every `crm-*` server.

### Changes

#### Connected Actual budgets
- **`addon.sh connect actual --instance <slug>`** connects a budget on the
  bundled or a remote Actual server (stdin keys `server`, `sync_id`,
  `session_token` or `password`, `encryption_password`, `profile`, `access`).
  The add-on must be enabled first: `connect` on a disabled add-on stores the
  budget, but Hermes registers no entry for it. `make budget-connect SLUG=...
  URL=... [ACCESS=write] [SYNC_ID=...]` asks for the passwords without echo,
  pipes the JSON to it and refuses while `ACTUAL_MCP_ENABLED` is off; `make
  budget-disconnect SLUG=...` removes the budget. Both print `[budget]` lines.
  Only the session token is stored, under `data/addons/actual/secrets/`; the
  password is used once. A slug starts with a letter, then up to 30 of
  `a-z`, `0-9` and `-`. Only the `default` Hermes profile is supported
  (`browser` mirrors it).
- **New `budget.sh` commands:** `budgets --json`, `status [--slug] --json`,
  `access <slug> read|write --json`, `init --server-only` (the manifest's init
  hook), `connect` and `disconnect` (its hooks) and `ensure`, which `make
  ensure-local` runs to create `data/addons/actual/{instances,secrets}` (0700).
  `actual-admin.mjs` signs in with `ACTUAL_SESSION_TOKEN` and exits 5
  (`no_access`) or 6 (`unreachable`). `make doctor` shows each connected
  budget's state.
- **Launcher in the `actual-mcp` containers:** one child per connected budget
  at `/<slug>/mcp`. It polls the store every 2 s, restarts a child when any of
  its files change and deletes its cache on every stop. `/mcp` is unchanged.
- **New mounts:** both MCP containers mount `data/addons/actual` read-only at
  `/budgets`, and the Hermes bootstraps mount `data/addons/actual/instances`
  read-only. Without `make`, run `./scripts/budget.sh ensure` before compose,
  or on Linux Docker creates the directories owned by root.
- **Hermes entries:** `budget-<slug>-ro` for every connected budget and
  `budget-<slug>` for `write` ones, on `default` and `browser`. The cron fence
  now matches add-on services by scheme, host and port, so `budget-<slug>`
  stays out of cron.
- **The single `budget` entry** registers only while `ACTUAL_BUDGET_FILE` names
  a budget file. `make budget-remove` keeps `ACTUAL_MCP_ENABLED=1` while any
  connected budget exists.
- **Manifest:** the `local` setting owns `ACTUAL_LOCAL_ENABLED`, and the
  manifest gains `instances` settings and hooks (with `optional: true`, so
  `addon.sh enable actual` works before any budget is connected), `data` paths
  and volumes, and the error names `bad_access`, `encryption_password` and
  `unknown_profile`. `addon.sh disable actual` now sets
  `ACTUAL_LOCAL_ENABLED=0` and stops `actual-server`; `addon.sh enable actual`
  turns it back on when `local` is stored. When the flag is on and `local`
  isn't stored (a v0.3.12 install that set `ACTUAL_LOCAL_ENABLED=1` by hand),
  `disable` stores `local` as true first, so the next `enable` restores it.
  This holds for every add-on whose bool setting owns a gate flag.
  `make budget-*` commands are unaffected.
- **Replacing a connection:** `make budget-connect ... FORCE=1` passes
  `--force` to `addon.sh connect`, which runs `budget.sh connect --slug <slug>
  --replace --json`. The hook makes the same checks as for a new connection and
  overwrites the secret files only if they pass.
- **First budget in `enable`'s result:** `budget.sh init --server-only --json`
  adds `"result": {"sync_id": ..., "name": ...}` to its `done` line when the
  server has exactly one budget afterwards (`sync_id` and `name` stay at the
  top level). With several or none there is no `result`.
- **Local budgets while the local server is off:** with `ACTUAL_LOCAL_ENABLED`
  off, the Hermes bootstraps skip rows whose server is `local` and drop their
  entries; remote rows are unaffected. Both bootstrap services now get
  `ACTUAL_LOCAL_ENABLED`, so toggling it re-runs the registration (this
  recreates the two one-shot bootstrap services once on the next `up`).
  `budget.sh status` reports such rows as `server_off`, and `make doctor`
  warns with the steps to turn the server back on.
- **The init hook** (`budget.sh init --server-only`) does nothing while
  `ACTUAL_LOCAL_ENABLED` is off, so `addon.sh enable actual` works on a
  remote-only install. Turning the bundled server on later takes `addon.sh
  enable actual --reinit` to set it up. On a v0.3.12 local install,
  `server: "local"` signs in with the password in `compose/actual/budget.env`
  and copies it to `data/addons/actual/secrets/server_password`.
- **Limits:** on Linux the host user must be uid 1000 (`HERMES_UID`, the
  default), because the MCP containers read the mode-600 secret files as uid
  1000; `budget.sh connect` refuses another uid with `not_found`, and `make
  doctor` flags it. A password change on an Actual server, or turning OpenID on, ends every
  connected budget's session; `budget.sh status` shows `token_expired` until
  each is reconnected. A write made in the seconds between a good sync and the
  background push can still be stranded if the session ends right then.
- Not run on a live stack yet.

#### Other changes

- **`sync-profiles.sh` is faster.** It reads `.env` once, and every add-on
  manifest in one `awk` run, instead of running `grep` and `sed` per flag and
  per key. That is about 15 processes per run instead of about 180, and 0.04 s
  instead of about 0.6 s on macOS. `make up` and every `addon.sh enable` and
  `disable` run it. Its output and `.env` changes are unchanged, with one
  exception. For a `.env` that does not end in a newline, `upsert_env` appends
  the next key onto the last line, as before. The old script then re-read the
  merged line and acted on the wrong value, and the new one uses the value the
  file started with.
- **`addon.sh connect --force` and hook results** (CR 0026 R7). `connect
  <short-id> --instance <slug> --force` replaces an existing instance: the
  `connect` hook gets `--slug <slug> --replace --json`, and only after it
  passes is the instance rewritten from the new values alone, so settings left
  out fall back to their defaults and stored secrets left out are deleted. A
  failed hook leaves the old instance untouched. The `done` line then has
  `"replaced":true`; `--force` on an unknown slug is a plain connect. An
  `init` or `connect` hook's `done` line may carry a `result` object, which
  `enable` or `connect` passes on as `result` in its own `done` line, keeping
  string, number and bool values only. See the addon.sh section of
  `docs/extending.md`.
- **Bitwarden accounts from Boundary.** `addon.sh connect bitwarden --instance
  S --json` and `disconnect` call `vault.sh add --json` and `remove --json`.
  `status --json`, `unlock --json`, `lock --json` and `sync --json` give Boundary
  the account states and the unlock and sync actions; see the Boundary section
  of `docs/bitwarden.md`. Connect restarts the broker, and disconnect restarts
  it when it is running; a restart locks the other `manual` accounts, and the
  done message names them. A disconnect retried after a failed restart ends
  with `done` and drops the store entry. New slugs start
  with a letter and have at most 31 characters. `make vault-add` and
  `make vault-remove` keep the add-on store's entries in step.
- **The broker signs in `manual` accounts at start.** They stay locked; a wrong
  API key shows as `login_refused` at once. `bw` stays signed in across broker
  restarts, so the start sign-in checks the key only the first time and does
  not catch a key revoked later. `GET /accounts` gains `logged_in` and
  `retry_after`.
- **`make vault-grant` and `vault.sh grant` are gone.** Approve a request by
  moving the login into the agent's folder or collection in a Bitwarden app;
  the next sync marks the pending request done. `bitwarden_release_access`
  denylists the login and no longer moves it out of the agent's folder or
  collection, so you move it in a Bitwarden app. The broker never lists or
  fills a denylisted login meanwhile, and the next sync that no longer finds it
  in scope drops the denylist entry. `vault.sh requests --json` (and
  `GET /requests`) returns `pending` and `recent` and no longer has an
  `unreturned` key. The release reply is `{"released": true}` without
  `returned`, and `bitwarden-setup` has no `grant` command, `--site` or
  `--item`. `python3 -m broker.cli requests` has no `--tsv`.
- **`addon.sh` relays `retryAfter`.** A hook's error line may carry a positive
  integer `retryAfter` (seconds); the add-on's error line then has the same
  field, for any add-on.
- **Mail.app email is a built-in add-on** (`addons/email-local/addon.json`),
  a manifest over the existing `mail-mcp` and `mail-drafts-mcp` services.
  Nothing an install sets changes: the flags `MAIL_MCP_ENABLED` and
  `MAIL_DRAFTS_ENABLED`, the profiles `mail` and `mail-drafts`, the MCP names
  `mail` and `drafts`, the `MAIL_*_URL` and timeout overrides, and the host
  apps and their install. The bootstrap registers both servers through the
  add-on path instead of its own functions, so `addon.sh list --json` now
  includes `email-local` with `platform: macos`.
- **New `email` add-on** (`addons/email/addon.json`): read-only email over
  IMAP accounts, configured through `scripts/addon.sh` with no host helper.
  Four services: `email-indexer` and `email-outbox` (profiles `email` and
  `email-drafts`) hold the account passwords as files under
  `data/addons/email/secrets/` and join only the new `email-egress` network;
  `email-mcp` and `email-drafts-mcp` hold no password and join only the new
  internal `email` network, which `hermes` also joins. Flags are
  `EMAIL_ENABLED` and the gate `EMAIL_DRAFTS_ENABLED` (set by the `drafts`
  setting). The index and the drafts queue live in the named volumes
  `email_index` and `email_drafts`, which `disable email --purge` deletes. MCP
  names are `email` and `email-drafts`, never offered to `api-server` or cron.
  `security = "plain"` is allowed only for `localhost`, `127.0.0.1`,
  `host.docker.internal` and `greenmail`; Proton Bridge goes through
  `host.docker.internal`. A multi-line PEM `tls_ca` can be stored, because the
  add-on has no connect hook. Not tested on a real account yet; see
  [docs/email.md](docs/email.md#first-real-run-email). An account without
  `mailboxes`, or with a setting the indexer refuses, is skipped with a
  log line naming its slug while the other accounts run.
- **One IMAP account failing no longer stops the others** (`email` and
  `email-local`'s IMAP source). A refused or missing password, or a server
  that can't be reached, is logged as before; the pass then indexes the other
  accounts and keeps the failed account's rows. `mail-index.py run` prints the
  counts with `"failed": {"<id>": 4}` (or 5) and exits 4 or 5 as before. Under
  `run --every`, the pass's JSON line now has the counts and `failed` beside
  the `"error"` key, where it used to be `{"error": "4"}` alone. The mail
  MCP's error before the first pass now reads "no index yet; the indexer
  hasn't completed a pass".
- **`mail_status` has `failed_accounts`** (`{"<id>": 4}` or 5) for the
  accounts the last pass couldn't read, cleared once a pass reads them. A pass
  where every account fails no longer creates the index or moves
  `last_indexed`.
- **`scripts/addon.sh` lets an exported `ASSISTANT_DATA_ROOT` win over
  `.env`**, as compose and `crm.sh` already did. Before, an exported value was
  ignored when `.env` set one.
- **The drafts watcher rejects an unreadable queue file** (status `rejected`,
  reason "unreadable request") and moves on to the next request, where it used
  to stop on it. This applies to `mail-drafts` on the host, so `email-local`
  too, as well as `email-outbox`.
- **`MAIL_MCP_ENABLED` and `MAIL_DRAFTS_ENABLED` accept `on` and any case**
  (`1`, `true`, `yes`, `on`), in the bootstrap, `make doctor` and
  `mail-triage-cron.sh`. `sync-profiles.sh` already started the containers
  for these values; before, Hermes then left the servers unregistered for
  `True` or `on`.
- **The cron fence also fences by name and configured URL for mail.** Besides
  any server on `mail-mcp:8000` or `mail-drafts-mcp:8000`, cron jobs now lose
  servers named `mail` or `drafts` and servers on the configured
  `MAIL_MCP_URL` or `MAIL_DRAFTS_MCP_URL`.
- **Twenty CRM is a built-in add-on** (`addons/twenty/addon.json`). Boundary can
  move Twenty onto `scripts/addon.sh`: instances are workspaces, with `url`,
  `access`, `local` and two secret keys, and the manifest's connect and
  disconnect hooks call `crm.sh connect` and `crm.sh disconnect`.
  `TWENTY_MCP_ENABLED=1` now also puts `crm-mcp` in `COMPOSE_PROFILES`; the
  profile selects no service. The manifest declares `permissions.cron: true`,
  which is what the stack did before 0.3.13 too: cron jobs keep the `crm-*`
  entries, and a job on the default profile uses a write workspace's
  read/write key.
- **Twenty can be enabled before any workspace exists.** The manifest's
  `instances` block sets `optional: true`, so `addon.sh enable twenty` no
  longer stops with `missing_settings` / `instances:workspace`. Its init hook
  still migrates leftover `compose/twenty/workspaces/*.env` files.
- **Twenty's hook error names** for Boundary: `crm.sh connect` ends with
  `local_mismatch`, `local_off`, `missing_key` or `bad_value` (listed in
  `instances.errors`), or with the shared names `bad_slug`, `login_refused`,
  `no_access`, `timeout` and `server_unreachable`. `crm.sh init --json`
  ends with `bad_slug`, `server_unreachable` or `command_failed`.
- **`crm.sh add` and `remove` output:** the stdout lines name the store
  file, `<data root>/addons/twenty/instances/<slug>.json` as an absolute
  path, instead of `compose/twenty/workspaces/<slug>.env`; renaming a
  leftover workspace file is reported on stderr. `add` now refuses a key with
  whitespace, a quote, a backslash or a control character, and a URL with
  `user:password@` before the host. Exit codes are unchanged.
- **Workspaces moved out of `compose/twenty/workspaces/`** into the add-on
  store `data/addons/twenty/` (`instances/<slug>.json`,
  `secrets/<slug>.key_ro` and `.key_rw`). `crm.sh migrate` imports the old
  files and renames them `.env.migrated`; `make ensure-local` and `crm.sh`
  `init`, `add`, `remove`, `access`, `login-link` and `open` run it.
  `crm.sh add`, `remove` and `access` write the store.
  `compose/twenty/workspace.env.example` is the legacy format and only
  documents what `migrate` reads.
- **Profile `.env` key names changed:** the Hermes bootstrap stores each
  workspace key as `CRM_<SLUG>_KEY`, not `TWENTY_<SLUG>_KEY`, and removes the
  old lines. `config.yaml` references the new name.
- **Bitwarden's Twenty key items write the store.** `twenty/<slug>/key-ro`
  and `key-rw` replace the whole file `secrets/<slug>.key_ro` / `.key_rw`
  under `data/addons/twenty/` instead of lines in a workspace file. A value
  with whitespace or a control character is refused, and a missing file is
  never created. The sync service mounts only that `secrets` folder.
- **`make doctor` reads the store** for Twenty: workspaces, keys present per
  access level (no values printed), and a warning for each workspace file not
  yet migrated, with the reason when it cannot be.
- **`TWENTY_MCP_TIMEOUT` and `TWENTY_MCP_CONNECT_TIMEOUT`** from `.env` now
  reach both Hermes bootstrap services and set the timeouts of every `crm-*`
  server.
- **`mcp.instances` in manifests** (experimental, built-in add-ons): one `mcp_servers`
  entry per stored instance, with the key picked by access. See
  [addons/README.md](addons/README.md).
- **LightRAG is a built-in add-on** (CR 0022 R2): `addons/lightrag/addon.json`
  replaces the hand-written registration in the api-server bootstrap. The
  entry key is still `lightrag`, with the same five read tools and pins, on
  the api-server and browser profiles only. The default profile is left
  alone (`mcp.unmanaged`, below). `scripts/addon.sh enable|disable lightrag`
  sets `LIGHTRAG_MCP_ENABLED` and the `rag` profile and starts or stops
  `lightrag` and `lightrag-mcp`. `disable` also stops Neo4j unless
  `NEO4J_ENABLED` is on or another active profile starts it; `rag_storage/`,
  `inputs/` and the `neo4j_data` volume are kept. `lightrag`
  now appears in `scripts/addon.sh list` and `list --json`.
  `LIGHTRAG_MCP_TIMEOUT` and `LIGHTRAG_MCP_CONNECT_TIMEOUT` in `.env` now
  reach the bootstrap. Compose did not pass them before, so they were
  ignored and the defaults of 120 and 30 seconds applied. An install that
  set them now gets those values. See
  [docs/knowledge-bases.md](docs/knowledge-bases.md).
- **n8n is a built-in add-on** (CR 0020 R10): `addons/n8n/addon.json`.
  `scripts/addon.sh enable|disable n8n` sets `AUTOMATION_ENABLED` as before;
  `enable` runs `n8n-import`, then starts `n8n` and `gpt-researcher`, and
  `disable` stops those three. `gpt-researcher` belongs to n8n.
  `disable n8n` leaves `gptr-mcp` running, since the GPT Researcher add-on
  owns it. With `GPTR_MCP_ENABLED=0` it stays up only until it is
  stopped or recreated, since `automation` was its last active profile;
  `scripts/addon.sh enable gptr` keeps it. `remove n8n --delete-data` (or `disable n8n --purge`) deletes the
  `n8n_storage` and `gptr_outputs` volumes, so the SQLite database with its
  workflows, credentials and execution history is gone. Back up
  `N8N_ENCRYPTION_KEY` with the volume. `n8n` appears in `addon.sh list`;
  the `AUTOMATION_ENABLED` row left `sync-profiles.sh`'s table for the
  manifest's `builtin` block. See [docs/n8n.md](docs/n8n.md).
- **`n8n-import` generates the demo credentials itself.** On the first start
  (no `database.sqlite` yet) it runs the generator in the n8n container from
  `N8N_ENCRYPTION_KEY`, `LIGHTRAG_API_KEY`, `NEO4J_USERNAME` and
  `NEO4J_PASSWORD`, and imports from `/tmp/n8n-credentials`. It no longer
  reads `n8n/demo-data/credentials/` on the host, so enabling n8n needs no
  host-side step. It exits 1 and names `N8N_ENCRYPTION_KEY` or
  `LIGHTRAG_API_KEY` if either is empty. Only `n8n-import` gets the
  LightRAG and Neo4j keys; the `n8n` service doesn't.
- **`postInstallMessage` in `addon.sh list --json`** (additive): an object gets
  the key `postInstallMessage` only when its manifest has a non-empty one
  (n8n does). Other objects are unchanged. The validator now refuses a
  `postInstallMessage` that is not a non-empty string.
- **`mcp.unmanaged`** (experimental, built-in manifests only): a list of
  Hermes profile names on which bootstrap neither registers, drops nor pins
  the add-on's MCP entry. For a fixed entry, the parked-retry setting still
  applies to an existing entry at the add-on's URL on every profile, default
  included. An `mcp.instances` entry has no fixed URL, so on an unmanaged
  profile it gets no parked retry either.
- **Other MCP keys at an add-on's URL are dropped**: after bootstrap registers
  an add-on's MCP server on a profile, it removes every other key at the same
  URL on that profile, whatever its tool filter. For LightRAG this removes an
  unfiltered `lightrag-mcp` copy cloned from default into api-server or
  browser. For GPT Researcher (default, browser) and Actual Budget (default,
  browser, api-server) it also applies to the default profile, which is the
  user's own config: a key you added by hand on default at the GPT Researcher
  or Actual URL is now removed in favor of the stack's key. With an add-on's
  flag off, bootstrap drops every key at its URL on those profiles the same
  way.
- **An invalid add-on manifest fails closed**: if a built-in manifest does
  not validate (a typo, non-canonical JSON), bootstrap still removes every
  MCP key at the URL the manifest names from the profiles it covers, instead
  of leaving them as they were. `mcp.unmanaged` profiles are still skipped. A
  manifest whose URL cannot be worked out is skipped with a warning, as
  before.
- **Add-on manifest fields** (experimental): a built-in manifest can declare
  `gates` (further flag-to-profile rows for `sync-profiles.sh`), `mcp` as a
  list with per-entry `title` and `when`, `instances`, `commands`,
  `services.<name>.when`, `mounts` and `volumes`, and settings that take the
  types `list` and `date`, `optional: true`, and `flag` (a gate flag) on a
  bool. Services that list `mounts` get the add-on's `config/`, `instances/`
  and `secrets/` directories under `/run/addon/`. See
  [addons/README.md](addons/README.md).
- **`scripts/addon.sh`** lists, configures, enables, disables and runs
  commands of built-in add-ons, with settings, instances and secrets stored
  under `data/addons/<short-id>/`. `list --json` includes each add-on's
  settings schema for building a settings form. See
  [docs/extending.md](docs/extending.md#add-ons-scriptsaddonsh). Field
  names may change between patch releases while the stack is in 0.3.x.
- **`scripts/addon.sh` lifecycle** (experimental). New subcommands
  `connect <id> --instance <slug>`, `disconnect` and `remove <id>
  --delete-data`, plus `disable --purge`, `enable --reinit` and `--json` on
  `enable`, `disable`, `connect`, `disconnect` and `remove`. With `--json`,
  stdout is one JSON object per line (`pull`, `setup`, then one `done` or
  `error`); `list --json [--sizes]` adds per-service image, memory in bytes
  and download size. A lock (`data/addons/.lock`) makes a second change fail
  with `busy`, and a failed `enable` restores the flags. `remove` keeps
  paths outside `./data` and `ASSISTANT_DATA_ROOT`, stack data and paths
  behind a symlink, and reports each as `kept`. `disable --purge` does the
  same work as `remove --delete-data` after disabling: it removes the
  containers, the `data.volumes` volumes and the `data.paths` directories,
  with the same `kept` rules. A `--purge` failure in step `delete` is
  reported as `compose_failed` (exit 1) when `docker compose rm` or `docker
  volume rm` fails, and as `command_failed` (exit 4) when deleting a path
  fails. Every
  `--json` run ends with exactly one `done` or `error` line, including an
  invalid data root (`command_failed`, exit 1) and an unexpected exit
  (`command_failed`, exit 4, after the rollback). A hook's relayed lines are
  limited to `setup` and `pull`. The `init` hook's `mcp` names join
  `enable`'s `done.mcp`, and a hook's `done` `message` (from `init`,
  `connect` or `disconnect`) is copied to the run's `done` line. A locked run deletes any
  `data/addons/.staging.*` file a killed `connect` left. `addon.sh` does not
  wait for MCP servers: Boundary checks the names in the `done` line's `mcp`
  in `mcp.servers.status`. See
  [docs/extending.md](docs/extending.md#add-ons-scriptsaddonsh).
- **Add-on lifecycle manifest fields** (experimental): `services.<name>.oneshot`,
  `hooks`, `instances.connect`, `instances.disconnect`, `instances.errors`,
  `instances.optional`, `transient` on a secret instance setting (the value
  goes to the connect hook and is never stored), `onToggle`, `data` and
  `backup`. `services.<name>.volumes` becomes `data.volumes`; both are
  accepted in this patch and the old form is refused from the next.
- **Add-on reload.** `addon.sh` reloads Hermes by running both bootstraps
  with `HERMES_BOOTSTRAP_FORCE=1` and then `scripts/vault.sh reload-plugins`.
  It no longer recreates `hermes`, so plugin changes such as Bitwarden's tools
  reach new sessions only.
- **Bitwarden's `backup` and `restore` hooks** moved from plain strings to
  the `script` form (`{"script": ["scripts/vault.sh", "backup"]}`).
- Boundary can move Actual Budget onto `addon.sh` once its manifest has
  `hooks.init`, `instances.connect` and `data`.
- **Turning Bitwarden on or off no longer recreates `hermes`.** The
  `bwbroker` plugin is installed into `/opt/hermes/plugins/bwbroker` on
  every start, and `BITWARDEN_ENABLED` is no longer in the `hermes`
  service's environment; it still reaches both bootstraps, which turn the
  plugin on or off per profile as before. When a bootstrap changes a
  profile's add-on login backends it leaves `.addon-plugins-changed` in that
  profile's home. The new `./scripts/vault.sh reload-plugins`, which
  `make up` runs, sends the running gateway Hermes' `reload-plugins`
  control verb for each marked profile and clears the marker once the
  gateway answers. New sessions get or lose the Bitwarden tools; open
  sessions keep theirs. A caller that runs the bootstraps without `make up`
  runs `./scripts/vault.sh reload-plugins` after them. When no gateway
  answers, the marker stays and `make restart` applies the change.
- **Tests.** `make hermes-image-test` runs all 35 tests with checks inside
  the pinned Hermes image; run it after a Hermes bump. With
  `HERMES_IMAGE_REQUIRED=1` a missing image fails a test instead of
  skipping it. `make actual-mcp-test` runs the vendored actual-mcp unit tests
  in a new `test` stage of its Dockerfile; the release image is unchanged.
  `scripts/addon.sh` no longer forks per `.env` line when reading flags,
  which took about 0.6 s per read on a `.env` copied from `.env.example`
  (`list`, `enable` and `disable` read them).
- **`make test`.** `scripts/run-tests.py` runs every test listed in
  `scripts/tests.manifest`, several at a time, with a log per test under
  `.test-runs/`. Tags in the manifest say what a test needs (Docker, macOS,
  the Hermes image), so tests this machine can't run are listed as skipped
  rather than passing silently; `--strict` makes a skip fail. CI runs the
  tests tagged `ci` through it in one step, and `make hermes-image-test`,
  `bitwarden-test` and the other multi-test targets use it too. A test that
  skips as a whole now exits 77 instead of 0. `test-messages-webhook-config.py`
  runs in the pinned Hermes image instead of the running `hermes` container,
  and `test-messages-mcp-container.py` builds a per-run image tag that it
  removes afterwards.

#### Fixes

- **Bitwarden: `vault.sh` found no broker.** `vault.sh` and `make doctor` ran
  `docker compose --profile bitwarden …`, which replaces `COMPOSE_PROFILES`
  and drops `core`. `bitwarden-broker` depends on `hermes`, which is in
  `core`, so compose rejected the project, and every command reported
  `broker_down` or exited 1 while the broker was running. Boundary's connect
  failed the same way. The scripts now pass the profiles `COMPOSE_PROFILES`
  enables plus `bitwarden`, and the commands they print add `--profile core`.
- **Compose tests ignore local settings.** The compose config tests load
  only `docker-compose.yml` and render with `ASSISTANT_DATA_ROOT=./data`.
  `test-bitwarden-compose-config.py` and `test-actual-compose.sh` failed on
  an install whose `docker-compose.override.yml` adds `hermes-worker` binds
  or whose `.env` moves the data root.
- **Actual Budget: ended sessions fail the call.** After the Actual server
  password changed or the MCP server's session ended (deleted, or the user
  disabled), `actual-mcp-rw` and `actual-mcp-ro` kept answering from their
  loaded copy: reads were stale, and writes were reported as saved but never
  reached the server. Now the sync before each call notices this, the call
  fails, and nothing is written locally. The next call signs in again, so a
  password install whose session merely expired recovers on its own; it keeps
  failing only when the password changed, with `Actual sign-in failed
  (<reason>). If it keeps failing, reconnect it with make budget-remove and
  make budget-init (local server) or make budget-add URL=<server> FORCE=1
  (remote server).` Removed access, a budget deleted on the server, and a
  budget reset in the Actual web app, re-keyed or otherwise changed on the
  server fail the same way, each with its own message. An unreachable server
  still answers from the loaded copy, as before. Verified against a throwaway
  `actual-server` 26.11.0 (deleted session and password change), not on a
  live stack. A write in the few seconds between a good sync and the
  background push can still miss the server if the session ends in that
  window; it stays in the local cache.

## v0.3.12 — 2026-11-09

### Upgrade steps

1. `docker compose up -d`. It recreates `hermes` once, with both add-ons
   off: `hermes` joins two new internal networks, `vault` and `budget`, and
   gets new overlay mounts. The network `vault` stays empty while Bitwarden's
   local mode is off. Both bootstrap services run in full once, because
   their environment gains `TWENTY_LOCAL_ENABLED`, `ACTUAL_MCP_ENABLED`,
   `ACTUAL_ACCESS` and `BITWARDEN_ENABLED`, and the bootstrap script and the
   add-on manifests changed. With both add-ons off no new service starts and
   the bootstraps write nothing for them.
2. A checkout used as the compose project directory needs `addons/` and
   `scripts/lib/addon_manifest.py`, which both bootstraps now mount.
3. **Boundary:** with `BITWARDEN_ENABLED=1`, a `hermes` recreate that Boundary
   drives itself must be followed by `docker compose --profile bitwarden up -d
   --no-deps bitwarden-broker` (or `./scripts/vault.sh follow-hermes`). See
   the Bitwarden entry under Changes. Boundary should also add its
   `vault.create_login` card before moving its pin (CR 0024 R1, below).

### New services

All are off by default.

- `actual-mcp-rw` and `actual-mcp-ro` (profile `budget`): the Actual Budget
  MCP servers, built from the vendored giorgiobrullo/actual-mcp. Started by
  `ACTUAL_MCP_ENABLED=1`.
- `actual-server` (profile `budget-local`,
  `actualbudget/actual-server:26.11.0`, pinned by digest): a local Actual
  server. Started by `ACTUAL_LOCAL_ENABLED=1`.
- `bitwarden-broker` (profile `bitwarden`): runs `bw`, holds every account's
  session key and fills pages over CDP. It shares `hermes`' network namespace
  and listens on `127.0.0.1:8099` there. The image builds from
  `compose/bitwarden-broker/` on first use. Started by `BITWARDEN_ENABLED=1`.
- `bitwarden-setup` and `bitwarden-secrets-sync` (profile `bitwarden-tools`):
  one-shots that the `make vault-*` and `make secrets-sync` targets start.
- `vaultwarden` (`vaultwarden/server:1.37.4`, pinned by digest) and
  `vaultwarden-tls` (profile `vault`): Bitwarden's local mode. Started by
  `BITWARDEN_LOCAL_ENABLED=1`.

New networks: `budget`, `vault` (internal; `hermes`, `vaultwarden-tls`,
`bitwarden-setup`), `vault-backend` (internal), `vault-host` and
`bitwarden-setup`. New volume: `bitwarden_sync`.

### New optional `.env` keys

Defaults keep the previous behaviour. `setup.sh --budget` fills in the Actual
keys; see [docs/budget.md](docs/budget.md) and
[docs/bitwarden.md](docs/bitwarden.md#settings).

- `ACTUAL_MCP_ENABLED=0`, `ACTUAL_LOCAL_ENABLED=0`: start the `budget` and
  `budget-local` profiles.
- `ACTUAL_ACCESS=` (empty): `read` limits every profile to `actual-mcp-ro`;
  `write` or empty keeps the manifest's levels; any other value registers no
  budget tools.
- `ACTUAL_PORT=5006`, `ACTUAL_DATA_DIR=` (empty means `./data/actual/server`),
  `ACTUAL_BUDGET_FILE=` (host path to the budget file; empty mounts a
  placeholder and every tool call fails), `ACTUAL_SERVER_IMAGE` (commented
  out). `ACTUAL_ADMIN_TIMEOUT_S` is read from the environment and has no
  line in `.env.example`.
- `BITWARDEN_ENABLED=0`: starts `bitwarden-broker` and registers its login
  backend.
- `BITWARDEN_LOCAL_ENABLED=0`: starts `vaultwarden` and `vaultwarden-tls`
  (wired in `sync-profiles.sh` by hand, as Twenty's is).
- `BITWARDEN_SYNC_MINUTES=10`, `BITWARDEN_IDLE_LOCK_MINUTES=0`: the broker's
  `bw sync` interval (must be above 0) and the idle lock for `manual`
  accounts.
- `BITWARDEN_HOST_DATA_DIR=./data/addons/bitwarden`: `bw/`, `broker/`,
  `secrets/`, `local/` and `vaultwarden/`. Not moved by `ASSISTANT_DATA_ROOT`.
- `VAULTWARDEN_PORT=8222` (bound to `127.0.0.1`),
  `VAULTWARDEN_TLS_IMAGE=nginx:1.30.6-alpine`.
- `VAULT_OWNER_EMAIL`: your address for `make vault-init`.
- `TWENTY_SECRETS_DIR`: the folder holding `TWENTY_ENCRYPTION_KEY_FILE` and
  `TWENTY_DB_PASSWORD_FILE`, so `make secrets-sync` can write them. Unset, it
  mounts the empty `compose/twenty/no-secrets/`.

`BW_CLI_VERSION` (2026.10.1) is a build argument in
`compose/bitwarden-broker/Dockerfile`, next to the per-architecture SHA-256
digests; it is not read from `.env`.

### Changes

- **Bitwarden add-on** (experimental): Hermes signs in to websites with
  logins kept in Bitwarden cloud (US or EU), a self-hosted Bitwarden or
  Vaultwarden, or a local Vaultwarden, and `make secrets-sync` writes stack
  secrets from Bitwarden into the existing secret files. See
  [docs/bitwarden.md](docs/bitwarden.md).
  - The broker and `hermes`. Compose restarts `bitwarden-broker` with
    `hermes` (`depends_on` with `restart: true`), but a recreate of `hermes`
    alone leaves it in the removed container's namespace, where `docker
    compose restart bitwarden-broker` fails. The recovery is `docker compose
    --profile bitwarden up -d --no-deps bitwarden-broker`; `make
    hermes-reload`, the new `make hermes-recreate`, `make restart` and the
    scripts that recreate `hermes` run it through `./scripts/vault.sh
    follow-hermes`, and `make doctor` flags a broker in the wrong namespace.
  - New commands: `make vault-init`, `vault-add`, `vault-unlock`, `vault-lock`,
    `vault-sync`, `vault-requests`, `vault-grant`, `vault-remove`,
    `vault-open` and `secrets-sync`, all through `scripts/vault.sh`, which also
    has `requests --json`, `backup DIR` and `restore DIR`. `make up` now runs
    `scripts/vault.sh up-hook` after `docker compose up -d`; it returns at
    once while `BITWARDEN_ENABLED` is off. `make doctor` gains a Bitwarden
    section when either flag is on or an account file exists.
  - Hermes. With the flag on, the new cont-init
    `14-install-bwbroker-plugin.sh` copies the `bwbroker` plugin into
    `/opt/hermes/plugins/bwbroker` (removed again when the flag is off), and
    both bootstraps add `bwbroker` to `plugins.enabled` and set
    `vault.bwbroker.enabled: true` on `default` and `browser`, never
    `api-server`. They write `vault.bitwarden.enabled: false` on every
    profile they manage, `api-server` included, so Hermes' own `bw:` backend
    stays off, and list the plugin's
    `bitwarden` toolset in `known_plugin_toolsets.cron` so cron jobs don't get
    it unless a job names it in its own `enabled_toolsets`. Turning the flag off removes the plugin entries and leaves
    `vault.bitwarden.enabled: false` in place. The plugin adds
    `bitwarden_request_access` and `bitwarden_release_access`; its backend
    lists nothing and refuses fills in cron turns, including `delegate_task`
    children of a cron job. `browser_vault` `save_login` still saves into
    Hermes' local vault on these profiles.
  - Two overlays, mounted and applied on every install and inert until the
    add-on fills something: `patch-digest-redaction.py` scrubs broker-filled
    values from model output by digest (raw, base64, hex, percent-encoded
    and reversed forms), and `patch-bitwarden-broker-fill.py` routes fills and
    `enter_code` of `bwbroker:` handles to the broker. `docker compose up -d`
    recreates `hermes` for the new mounts.
  - Manifests. `addon_manifest.py` accepts the `credentials` read class (with
    the `reads` rules: `cron` false, `sends` false, no `api-server`) and a
    `vaultBackend` block (`name`, `plugin`, `toolset`, `tools`, `profiles`;
    built-in and official sources only; `vault`, `op` and `bw` reserved), and
    has a `vault-plan` command next to `mcp-plan`. `cron_fence_toolsets` fences
    a vault add-on's plugin toolset from cron. `addons/bitwarden/addon.json` is
    the first manifest with a `vaultBackend`.
  - Deviation from the design spec: the plugin is a directory,
    `compose/hermes/plugins/bwbroker/` (`plugin.yaml` and `__init__.py`), with
    the backend module `compose/hermes/bitwarden_broker_backend.py` copied
    beside it, because Hermes loads plugins as directories and accepts a
    login backend only from a bundled plugin. `vaultBackend.plugin` names the
    directory.
  - `compose/bitwarden/accounts/` is gitignored; account files are mode 600.
  - Checked against a throwaway Vaultwarden 1.37.4 with `bw` 2026.10.1
    (`scripts/test-vw-register.py`). The organization steps on bitwarden.com
    and setup for accounts with two-step login, new-device verification or
    Argon2id are not checked or handled. Linux hosts are not tested.
  - A live check on macOS ran `vault.sh` init, grant, sync, secrets-sync,
    backup and restore against a scratch Vaultwarden 1.37.4 with real `bw`,
    and a `bwbroker:` list, fill and release in-process in the pinned Hermes
    image against headless Chromium, with no password in any tool result or
    log. Not covered: a model-driven sign-in turn, bitwarden.com, Linux.
  - The `browser-login` skill (1.6.0) tells the model to fill `bwbroker:`
    handles like any other, to relay a locked account's `make vault-unlock`,
    and to call `bitwarden_request_access` once when a login is in Bitwarden
    but not in its scope.
- **`browser-signup` skill (CR 0024 R3)**: covers creating an account. The
  model asks for the identifier and profile fields first, asks before
  ticking terms boxes, calls `create_login` on the password step, uses
  `enter_code` or asks the user for verification, stops at a CAPTCHA, and
  uses `capture_secret` for a key the site shows. `browser-login` (1.5.0)
  points to it. Three tool-eval cases check it, and the scorer gains
  `allow_tools` for `pass_none` cases and `max_calls` (`browser_exec=4`).
  With no `new-password` marks, `create_login` now picks the form that
  looks most like a sign-up instead of the first form with a password field.
- **`browser_vault` `capture_secret` and `vault.take` (CR 0024 R2)**: on a page
  that shows a new API key, the model calls `browser_vault` with
  `action: capture_secret`, a `name` and a `destination`. Hermes finds the
  value itself and asks the viewer through a new `vault.capture_secret`
  server request (`{origin, site, name, destination, last4, replaces,
  capture_id}`; answer `"approve"` or `""`). `env` saves it to the profile
  `.env`. `viewer` keeps it encrypted under `capture_id` until the client
  calls the new `vault.take {id}` RPC, which returns `{name, origin, value}`
  once. This replaces the CR's `secret.deliver` frame, which would have gone
  to every attached viewer. The value is redacted from later tool output.
  A `selector` that names an editable password input is refused
  (`password_field`), as the page scan already skipped one.
  See "Capturing keys from a page" in `docs/hermes.md`.
- **`browser_vault` `create_login` (CR 0024 R1)**: on a sign-up form, the
  model calls `browser_vault` with `action: create_login` and the
  `identifier` the user chose. Hermes asks the viewer through a new
  `vault.create_login` server request (`{origin, site, identifier,
  regenerate}`; answer `{"generate": true}`, `{"password": "..."}` or `""`),
  generates a password from the fields' `minlength`, `maxlength`,
  `passwordrules` and `pattern`, fills every new-password field of the form
  and stores a `login` item for the page's origin. The password never
  appears in the result. Calling it with the returned `handle` replaces the
  password. A client that does not handle the request leaves the model waiting
  180 seconds, then reports `save_declined`; Boundary should add its card
  before moving its pin. See "Creating logins on sign-up forms" in `docs/hermes.md`.
- **Vault guidance names the `browser_vault` actions**: the `browser-login`
  skill (1.4.0), the save-login next step and the password-in-chat nudge
  named `browser_vault_fill`, `browser_vault_save_login` and the other
  `browser_vault_*` tools, which Hermes v0.22 replaced with one
  `browser_vault` tool taking an `action`. They now name the actions. Eval
  traces and `last-run.json` record vault calls as `browser_vault.<action>`,
  and `browser-login-save-first` expects `browser_vault.save_login`.
- **browser_exec hints at `.value` assignments**: when code sets an input's
  `.value` inside `js()` or `cdp()`, the result gains a `hint` saying React-style
  forms don't see the change and to use `fill_input` instead, reloading first if
  the field was already set that way. Code that uses the native value setter
  gets no hint.
- **Actual Budget add-on** (experimental): Hermes reads and edits an Actual
  Budget file through `actual-mcp-rw` and `actual-mcp-ro`, against the local
  `actual-server` or a remote server. Setup is `./scripts/setup.sh --budget`,
  `make budget-init` (local) or `make budget-add URL=...` (remote); `make
  budget-open`, `make budget-remove` and `make budget-access
  ACCESS=read|write` do the rest, and `make doctor` has a Budget block. A
  `budget` skill tells Hermes to look up ids, treat amounts as cents and
  confirm deletes, bulk edits and category moves. The read-only instance
  serves `api-server`; default and `browser` get the read/write instance
  unless `ACTUAL_ACCESS=read`. A read/write entry is kept out of cron. See
  [docs/budget.md](docs/budget.md). **The add-on has not been checked on a
  live stack**; only the offline tests against stubs have run.
- **Manifest extensions** the Actual add-on needs (the format is still
  experimental):
  - `mcp.service` may be a map `{"ro": ..., "rw": ...}` that gives each
    access level its own service. A map `mcp.service` ignores `mcp.env.url`.
  - `mcp.writeTools` lists tools that `tools` and `pins` leave out for a
    profile on the read-only service.
  - `mcp.env.access` names a variable that caps access: `read` turns
    read/write into read-only, `write` or empty changes nothing, any other
    value gives the profile no entry.
  - `permissions.cron` accepts `"ro"`: cron keeps the read-only entry and
    loses the read/write one.
- **`addon_manifest.py mcp-plan` has an 11th field**, `all_urls`: every URL
  the add-on can register, space-separated and sorted, so the bootstrap can
  drop the ones a profile must not have. A caller that splits the line on `|`
  must accept the extra field.
- **GPT Researcher is registered from a manifest**: `addons/gptr/addon.json`
  now holds its flag, profile, tools, pins and profile list, and
  `sync-profiles.sh` and both bootstraps read it instead of their own copies.
  The written config is unchanged (checked against v0.22.7 with the flag on
  and off), and `GPTR_MCP_URL`, `GPTR_MCP_TIMEOUT` and
  `GPTR_MCP_CONNECT_TIMEOUT` still override it. The manifest format is
  experimental. See [addons/README.md](addons/README.md).
- **Add-ons stay out of cron unless their manifest allows it**: the cron
  tool list that keeps mail and messages away from cron jobs now also leaves
  out every add-on whose `permissions.cron` isn't `true`. GPT Researcher sets
  it, so nothing changes for it. A job that names an add-on in its own
  `enabled_toolsets` still gets it.
- **`crm.sh access` changes a workspace's access** (CR 0023 R1):
  `./scripts/crm.sh access --slug S read|write` (`make crm-access`) sets
  `TWENTY_ACCESS` in that workspace file. It refuses `read` when the file has
  no `TWENTY_KEY_RO` and `write` when it has no `TWENTY_KEY_RW`, and works for
  local and remote workspaces. `crm init --rotate` now rewrites only the lines
  it owns (`TWENTY_URL`, `TWENTY_WORKSPACE_URL`, `TWENTY_WORKSPACE_ID` and the
  two keys) and keeps every other line, so `TWENTY_ACCESS` survives a
  rotation. An app that rewrote the access line after each `crm init` can
  call `crm.sh access` instead and drop the rewrite.
- **Local workspaces aren't registered while the local instance is off**
  (CR 0023 R2): with `TWENTY_MCP_ENABLED=1` and `TWENTY_LOCAL_ENABLED=0`, both
  bootstraps skip workspace files whose `TWENTY_URL` is
  `http://twenty-server:3000` and drop their `crm-*` entries and keys. Remote
  workspaces stay. `make doctor` reports the skip, and warns when a stale
  entry is still registered. An app that renamed `main.env` to `main.env.off`
  to turn the local instance off can drop the rename.
- **`crm.sh login-link` and a signed-in `make crm-open`** (CR 0023 R4):
  `./scripts/crm.sh login-link [--slug S]` signs in as the local owner and
  prints `<TWENTY_WORKSPACE_URL>verify?loginToken=<token>` as the only line on
  stdout, with progress and errors on stderr. Twenty accepts the token for 15
  minutes (`LOGIN_TOKEN_EXPIRES_IN`). Remote workspaces are refused.
  `make crm-open` uses it for local workspaces and falls back to the sign-in
  page. An app that called `signIn` itself with the owner's password can run
  this instead.

## v0.3.11 — 2026-11-06

### New optional `.env` keys

- `HERMES_WEB_SEARCH_MODE`: `private` (default), `private-first`,
  `keyless-first` or `keyless`. Chooses whether Hermes' `searxng_web_search`
  and `web_url_read` are answered by SearXNG, by Hermes' keyless ring (Exa,
  Parallel, Firecrawl, Keenable), or by one with the other as fallback. Unset
  keeps today's behaviour. See
  [docs/searxng.md](docs/searxng.md#search-modes).

### Upgrade steps

1. `docker compose up -d`. It recreates `hermes` for the new overlay mounts
   and reruns both bootstrap services. With `automation` or `searxng-prod`
   on, it also recreates `n8n` and `valkey`, which get healthchecks.
2. On a host whose uid isn't 1000, `docker compose restart hermes` so the
   worker key is copied again with the right owner. An app that chowns
   `/var/lib/hermes-worker-key/id_ed25519` after each `up` (the CR 0021
   fallback) can drop that step once it pins this release.

### Host requirements

- **Removed: `python3` and `node`.** `setup.sh`, `crm.sh init` and `todo.sh
  init` now need only bash, the base system's tools and the container
  engine. Their Python runs in the pinned Hermes image as the calling user,
  with the repo mounted at its own host path; the n8n credentials step runs
  Node in the n8n image (`N8N_IMAGE`). The first run may pull the Hermes
  image before `make up` does. An app that checked for `python3` or the
  Command Line Tools before running setup can drop that check, and one that
  stubbed `node` can drop the stub. `STACK_PYTHON=host` and `N8N_NODE=host`
  use the host's tools instead. Other scripts, such as `make doctor`, still
  need host `python3`. See [docs/extending.md](docs/extending.md#host-tools).
  Checked by running `setup.sh --non-interactive --todo --crm`, `crm.sh init`
  and `todo.sh init` on a fresh throwaway stack with `python3`, `node` and
  `make` removed from `PATH`.

### Changes

- **Turning LightRAG off removes its MCP entry** (CR 0022 R1): with
  `LIGHTRAG_MCP_ENABLED=0`, the api-server and browser bootstraps now remove
  every MCP entry pointing at `LIGHTRAG_MCP_URL`. That covers `lightrag` and
  the unfiltered `lightrag-mcp` copy a profile cloned from default. Before,
  both stayed in the config and Hermes kept retrying a server that wasn't
  running. Other servers on the profile are kept. The flag is part of the
  bootstrap's input hash, so changing it and starting the bootstrap services
  is enough; `HERMES_BOOTSTRAP_FORCE` isn't needed. The default profile isn't
  touched, since the bootstrap never registers LightRAG there. A
  `lightrag-mcp` entry added there by hand stays until the user removes it.
  Checked on a throwaway stack with Hermes v0.22.7. Both bootstrap services
  first ran with the flag on, and an unfiltered `lightrag-mcp` entry was then
  added to each profile. A rerun with the flag off removed `lightrag` and
  `lightrag-mcp` from both profiles and kept `boundary` and `searxng`.
  Turning the flag back on without `HERMES_BOOTSTRAP_FORCE` registered
  LightRAG again.
- **The SSH worker key belongs to the remapped hermes user** (CR 0021 R1):
  `09-install-worker-key` read `HERMES_UID` under `#!/bin/sh`, where s6
  gives cont-init scripts no environment, so it always chowned the key and
  `/opt/data/.ssh` to 1000:1000. With `HERMES_UID=501`, hermes then could
  read neither the key nor `known_hosts`, and every terminal and file tool
  call over ssh failed. The script now takes the owner from `id -u hermes`,
  which stage2 and `005-remap-hermes-uid` have already set, and runs under
  `with-contenv`. `11-warmup-uv-cache` also moves to `with-contenv`; its
  `UV_CACHE_DIR` default matched compose, so nothing changed there. Checked
  in throwaway containers with `HERMES_UID=501 HERMES_GID=20`: before, the
  key was owned by `1000 1000` and ssh to a worker failed host-key
  verification; after, it is owned by `501 20` and ssh exits 0.
- **`crm.sh init` grants server admin between two passes**: the Twenty setup
  container has no docker CLI, so `twenty_admin.py --owner-only` creates or
  signs in to the owner, `crm.sh` runs the SQL grant, and a second pass signs
  in again and does the rest. Both passes are safe to repeat, so a run that
  stops partway resumes. `twenty_admin.py` still runs the grant itself when
  `TWENTY_GRANT_ADMIN_CMD` is set.
- **Healthchecks for `n8n` and `valkey`**: `n8n` answers `/healthz`, and
  `valkey` answers `PING`. Every long-running service in an optional
  profile now has one.
- **Neo4j has its own profile**: `NEO4J_ENABLED=1` (or `setup.sh --neo4j`)
  starts Neo4j without LightRAG, under the new `neo4j` profile. `rag` still
  starts Neo4j, so installs that run `rag` see no change. The container keeps
  the name `lightrag-neo4j` and its volumes. `NEO4J_PASSWORD` and
  `LIGHTRAG_API_KEY` are no longer required by `docker-compose.yml` on
  installs without `rag` or `neo4j`. Instead, `make ensure-local` (and so
  `make up`) stops with an error when either profile is on and a key it needs
  is empty, and `make doctor` checks them. A downstream overlay that listed
  `profiles: ["rag"]` on a service of its own that needs Neo4j can switch to
  `neo4j`.
- **A browser URL that doesn't exist says so**: `browser_exec` blocks a URL
  whose hostname has no DNS record, but reported it as "Blocked: URL targets a
  private or internal address". A turn that guessed a nonexistent government
  URL read that as the browser being restricted and went back to searxng. The
  new `patch-browser-unresolvable-host.py` overlay names the hostname and says
  the address is probably wrong. The URL is still blocked, and private
  targets keep the old text. `docker compose up -d` recreates `hermes` for
  the new mount.
- **A paused searxng server points Hermes at browser search**: after 3
  failed calls in a row, Hermes's MCP circuit breaker pauses a server for 60
  seconds and told the model only to "use a different approach". A turn kept
  calling `searxng_web_search` through the pause until
  `same_tool_failure_halt` stopped it. The new
  `patch-searxng-pause-browser.py` overlay adds to searxng's pause error how
  to search on Bing with `browser_exec`, and not to call searxng tools until
  the pause ends. No `.env` change; `docker compose up -d` recreates
  `hermes` for the new mount.
- **A refused page read points Hermes at the browser**: searxng's
  `web_url_read` is a plain fetch, so bot-protected sites answer it with
  403, 429 or a 503 challenge, and pages built with JavaScript come back
  empty. A research turn kept retrying it and searching for other copies of
  the same pages, and each refusal counted toward searxng's circuit breaker.
  The new `patch-blocked-fetch-hint.py` overlay recognizes those results and
  adds a note, outside the untrusted-data wrapper, to open the URL with
  `browser_exec` and not to try to get past a CAPTCHA. No `.env` change;
  `docker compose up -d` recreates `hermes` for the new mounts.
- **Compaction keeps what tool results said**: Hermes pruned old tool
  results to one-line stubs (`[web_url_read] (11,104 chars result)`) before
  handing the window to the summarizer, so a research turn came out of
  compaction with sizes but no facts and re-read the same pages. The new
  `patch-compaction-tool-facts.py` overlay feeds the summarizer the original
  bodies (newest first, up to 4,000 chars each and 60,000 in total) and adds
  a `## Findings` section that lists each fact with its source URL and each
  source that failed. The stored transcript is unchanged. No `.env` change;
  `docker compose up -d` recreates `hermes` for the new mounts.

CI: runs `test-crm-sh.sh` and the new `test-no-host-python.sh`. The new
`test-cont-init-env.py` (CR 0021 R2) fails when a script mounted into
`/etc/cont-init.d` reads a variable its service's `environment` sets and
doesn't start with `#!/command/with-contenv`; reverting R1 fails it.
`docs/releasing.md` adds a shellcheck 0.9.0 run, the version CI installs, after
v0.3.10's lint-only CI failure. Test-only.

## v0.3.10 — 2026-11-05

### Upgrade steps

1. Hermes Agent v0.22.7 and WebUI 0.52.544 (from v0.22.5 and 0.52.492). The
   overlays no longer apply to v0.22.5. Back up `data/hermes` with `hermes`
   stopped, then run `make hermes-upgrade AGENT=v0.22.7 WEBUI=0.52.544` and
   the verification block in
   [docs/hermes-webui.md](docs/hermes-webui.md#verification). An app that
   sets its own image tags must move both together.
2. `docker compose up -d`. The `08-warmup-agent-browser` mount is gone from
   `hermes`, so compose recreates it.
3. With the `search-gate` profile on, `docker compose up -d --build
   search-gate`. The gate's code is baked into its image, and `make up`
   doesn't rebuild it.
4. `./scripts/setup.sh` (or `make ensure-local`), then `docker compose up -d`.
   Nothing new starts: the Twenty flags are written as `0`. The first start
   runs the full bootstrap once, because both bootstraps get new
   `TWENTY_*` environment variables and a new read-only mount,
   `compose/twenty/workspaces`.
5. To run Twenty locally: `./scripts/setup.sh --crm`, `make up`,
   `make crm-init`, `make hermes-reload`. To connect a Twenty you already
   run: `make crm-add SLUG=name URL=https://…`, then `make hermes-reload`.

### New services

- `crm` profile, switched by `TWENTY_LOCAL_ENABLED`: `twenty-server`
  (`127.0.0.1:${TWENTY_PORT:-3000}`, MCP at `http://twenty-server:3000/mcp`),
  `twenty-worker`, `twenty-db` (Postgres 16) and `twenty-redis`, on a new
  internal `crm` network. Image `twentycrm/twenty:v2.55.0` (1.12 GB). About
  1.8 GiB of RAM idle. Every container has a memory reservation, a memory
  limit measured under load and a healthcheck.

### New optional `.env` keys

- `SEARCH_GATE_FALLBACK_URL` (default unset): a remote SearXNG on another IP.
  Each of its engines stands in for the local engine of the same name while
  that one is cooling down after a block or used up for the day, alongside
  the local engines that still work; one with no local twin is used only
  while no local engine is usable. `SEARCH_GATE_FALLBACK_ENGINES` (default:
  the local list) picks its engines and `SEARCH_GATE_FALLBACK_AUTH`
  (`user:password`) sends HTTP basic auth. Unset, the gate behaves as before. See
  [docs/searxng.md](docs/searxng.md#fallback-instance).

- `TWENTY_LOCAL_ENABLED`, `TWENTY_MCP_ENABLED`, `TWENTY_MULTIWORKSPACE`
  (default `0`), and `TWENTY_MULTIWORKSPACE_BOOL`, derived from the last one.
- `TWENTY_ENCRYPTION_KEY` and `TWENTY_DB_PASSWORD`, generated by `setup.sh`
  on every install and never replaced. `TWENTY_ENCRYPTION_KEY_FILE` and
  `TWENTY_DB_PASSWORD_FILE` read them from files instead, so they don't show
  in `docker inspect`.
- `TWENTY_PORT`, `TWENTY_IMAGE`, `TWENTY_DATA_DIR`, and `TWENTY_USERNAME` /
  `TWENTY_PASSWORD` (written by `make crm-init`). `TWENTY_PASSWORD_FILE` keeps
  the owner password in a file instead of `.env`.
- In workspace files, `TWENTY_ACCESS=read|write` (default `write`) picks the
  key the dashboard and WebUI profiles get. `api-server` is always read-only.

### Changes

- **Hermes stops turns that re-run calls for results they already have**: the
  new `patch-tool-loop-replay.py` overlay counts, per turn, each call that
  repeats an earlier call's tool and arguments and returns a result already
  seen. The second adds a note to the result; with
  `HERMES_TOOL_LOOP_HARD_STOP` on (the default), the third ends the turn with
  `stale_replay_halt`. Upstream's guards missed a 9-call `browser_exec`
  rotation that ran for about 30 minutes, because they only catch cycles of
  up to 4 calls whose results match on every lap. Short alternating loops now
  stop sooner too: an A,B,A,B loop with unchanged results halts on its
  seventh call instead of its tenth. No `.env` change.
- **Naver enabled**: `searxng/settings.yml` turns on SearXNG's `naver`
  engine, and the gate's default `SEARCH_GATE_ENGINES` becomes
  `bing:10,google cse:20,naver:20`. In tests from one residential IP it
  answered every English query, and gave on-topic pages for full-sentence
  queries where Bing didn't. Run `./scripts/setup.sh` to rewrite
  `settings.local.yml` from the template (`make ensure-local` only creates it
  when missing), then `docker compose up -d --force-recreate searxng`. A
  `.env` that sets `SEARCH_GATE_ENGINES` keeps its own list; add `naver:20`
  to it, then `docker compose up -d search-gate`. An app that mounts its own SearXNG settings without `naver`,
  or a fallback instance without it when `SEARCH_GATE_FALLBACK_ENGINES` is
  unset, should set `SEARCH_GATE_ENGINES` to the old value; otherwise the
  gate cools `naver` down after each failure.
- **n8n setup only runs with `automation`**: `docker-compose.yml` no
  longer requires `N8N_ENCRYPTION_KEY` and `N8N_USER_MANAGEMENT_JWT_SECRET`.
  Compose filled them in for every service, so a `.env` without them failed
  every `docker compose` command, even with n8n off. `setup.sh` no longer
  requires Node, and runs the n8n credentials generator only when
  `automation` is on. The new `scripts/sync-n8n-credentials.sh` does that
  step and runs from `make ensure-local` (and so `make up`). With
  `automation` on, it writes the credentials if none exist and stops with an
  error if either key or `LIGHTRAG_API_KEY` is empty. `make doctor` checks
  both keys and the credential files when `automation` is on. Installs that
  use n8n behave as before. An app that runs `docker compose` without `make
  up` gets no error for a missing key; n8n then starts without the key the
  demo credentials were encrypted with.
- **Per-model request defaults**: `llm-proxy` adds request fields per model id
  from `model-defaults/local.json` (gitignored) to chat, completions and
  responses requests that leave them unset. A tested Qwen3.6-35B-A3B example
  ships in `model-defaults/examples/`; `make model-defaults-add
  EXAMPLE=qwen3.6-35b-a3b` installs it. With no `local.json`, requests are
  unchanged. Compose now mounts `./model-defaults` into `llm-proxy` and sets
  `LLM_PROXY_MODEL_DEFAULTS_FILE`, so `docker compose up -d` recreates it; an
  app with its own compose file can set the variable empty to keep the old
  behavior. New `GET /admin/model-defaults`; `/admin/upstreams` is unchanged.
  `LLM_DEFAULT_MAX_TOKENS` is now applied per upstream attempt, after the
  model's fields, with the same result for requests that use no model
  defaults. The SIGHUP log line reads `SIGHUP reloaded:` instead of
  `SIGHUP reloaded key files:`. See [docs/model-defaults.md](docs/model-defaults.md).
- **Search query wording**: `HERMES_ENVIRONMENT_HINT` now tells the agent to
  write `searxng_web_search` queries as 2-6 keywords instead of sentences, to
  retry once with different keywords when results don't match, and not to
  resend a query the search gate answered as busy. Hermes changes no query
  itself, and long sentence-style queries got unrelated results from `bing`.
  The hint is part of the `hermes` environment, so `docker compose up -d`
  recreates `hermes`.
- **Search gate status**: `GET /status` has a top-level `fallback_active`
  and an `upstream` (`local` or `fallback`) on each engine. Fallback engines
  are named `fallback:<engine>` in `/status`, `/blocks`, the `gate.engine`
  field of results and the log.
- **Browser tools**: Hermes v0.22.7 removed `browser_navigate`,
  `browser_snapshot`, `browser_click`, `browser_type`, `browser_press`,
  `browser_scroll`, `browser_back`, `browser_console`, `browser_get_images`,
  `browser_vision` and the `agent-browser` CLI. `browser_exec` drives the
  Chromium build now shipped in the image under `/opt/hermes/tools`. The
  boot-time agent-browser install, its doctor check and the EACCES hint in
  `patch-browser-exec-hint` are removed; `doctor.sh` checks that the `hermes`
  user can run the image's Chromium instead. The `browser-login` skill
  (1.3.0) and the `browser-login-save-first` eval case use `browser_exec`
  only.
- **MCP trust-gate approvals**: v0.22.7 adds its own redacted argument block
  to the prompt and remembers `session` / `always` answers per server and
  tool. `patch-mcp-trust-details` now adds only the `mcp` field, which stays
  as documented in [docs/extending.md](docs/extending.md#mcp-approvals-for-untrusted-servers).
  Changes a client sees: `command` carries Hermes' argument block instead of
  the one-line summary, and `choices` is `once`, `session`, `always`, `deny`
  (still `once`, `deny` past the spend limit).
- **uv cache warmup**: v0.22.7 ships no `uvx` and runs browser-harness from
  its own venv, so `11-warmup-uv-cache` only prepares the shared
  `hermes_uv_cache` volume now and no longer logs a failed warmup.
  `UV_CACHE_DIR` and `HERMES_UV_CACHE_DIR` are unchanged.
- **Control socket**: v0.22.7 falls back to a temp-dir socket on its own when
  `$HERMES_HOME` refuses one, but through `tempfile.gettempdir()`, which is
  virtiofs here. `patch-control-socket-tmp` still binds under `/tmp`; only
  its client-path anchor moved.
- **Twenty CRM**: Each workspace, local or remote, is one file in
  `compose/twenty/workspaces/` and one MCP server in Hermes, `crm-<slug>`.
  `api-server` gets the workspace's read-only key or no entry at all; the
  dashboard and WebUI profiles get the read/write key. `make crm-init` creates
  the local owner account, workspace, two roles and two keys without prompts.
  On Twenty v2.55 the role-wide setting left junction objects such as
  `noteTarget` writable, so the read-only role also gets an explicit read-only
  permission on every object. `make crm-mode MULTI=1` switches the local
  instance to one workspace per subdomain (`<slug>.localhost`). `make crm-add`
  checks each remote key over MCP before saving it and asks nothing when the
  keys are in the environment; `make crm-remove` forgets a workspace. A new `crm`
  skill tells Hermes how to reach records, and `make doctor` checks each
  workspace's key over MCP. See [docs/crm.md](docs/crm.md).
- **New MCP servers without recreating Hermes** (CR 0020 R4, checked live
  on v0.22.7): after the bootstrap services write a new `crm-main` entry,
  the running Hermes connects it within about a second. A default-profile
  session that was open before Twenty existed called a `crm-main` tool on
  its next turn, with no restart and no `reload.mcp`. `reload.mcp` with
  `confirm: true` answered `mcp_servers_unavailable` ("did not connect:
  boundary") because another configured server, Boundary's, was down. The
  error names those servers even when the new one connected, so a caller
  should check `mcp.servers.status` for its own server instead of
  treating the error as failure. `make hermes-reload`, which recreates
  Hermes, still works.
- **MCP tool pins for servers with a hyphen in their name**: Hermes names
  tools `mcp__<server>__<tool>` with `-` turned into `_`, so pins for
  `caldav-<slug>` and `crm-<slug>` servers never matched, and those tools
  stayed behind tool search. They are now pinned under Hermes' names.
  Removing a CRM workspace also removes its pins.

CI: `test-sync-n8n-credentials.sh` and `test-twenty-compose.sh` pass
shellcheck (SC2016, SC2059, SC2329). Test-only.

## v0.3.9 — 2026-11-04

### Upgrade steps

1. `./scripts/setup.sh` (or `make ensure-local`), then `docker compose up -d`.
   The sync writes the new keys below without changing which profiles run. If
   a profile is in `COMPOSE_PROFILES` while its flag is `0`, it keeps running
   and the sync prints a warning naming the flag.
2. The first start runs the full bootstrap once, because both bootstraps get
   new `GPTR_MCP_*` environment variables.
3. An overlay that writes `COMPOSE_PROFILES` itself should set the matching
   `*_ENABLED` flags too. To give Hermes deep research, set
   `GPTR_MCP_ENABLED=1` before step 1.

### New services

- `research` profile: starts the existing `gptr-mcp` service alone, without
  n8n or the `gpt-researcher` REST service, and publishes no port. Switched by
  `GPTR_MCP_ENABLED`. `gptr-mcp` also stays in `automation`.

### New optional `.env` keys

- `AUTOMATION_ENABLED`, `OLLAMA_ENABLED`, `SEARXNG_PROD_ENABLED`,
  `SEARCH_GATE_ENABLED`: switches for the `automation`, `ollama`,
  `searxng-prod` and `search-gate` profiles. When missing, each is written as
  `1` if its profile is already on and `0` otherwise.
- `COMPOSE_PROFILES_MANAGED`: profiles the sync added from a flag. Written by
  the sync; an overlay should not set it.
- `GPTR_MCP_ENABLED` (default `0`): `1` starts `gptr-mcp` through the new
  `research` profile and registers it in Hermes. `GPTR_MCP_TIMEOUT` (default
  `900`) is how long Hermes waits for one call.

### Changes

`COMPOSE_PROFILES` now follows the `*_ENABLED` flags. `scripts/sync-profiles.sh`
runs from `make ensure-local` and at the end of `setup.sh`. A flag set to `1`
adds its profile; a flag set to `0` removes the profile if the sync added or
adopted it. Profiles without a flag (`core`, `n8n-postgres-legacy`, an
overlay's own) are left as they are. An overlay that writes `COMPOSE_PROFILES`
itself should set the matching flags too, or a flag at `0` leaves the profile
running with a warning on every sync. `make services` lists each flag and its
profile's state, and `make up` prints the profiles it starts.

GPT Researcher for Hermes. Hermes never registered `gptr-mcp` before, so chats
had no deep research tools. The new `research` profile starts `gptr-mcp` alone
(no n8n, no REST service, no published port), and `setup.sh --research` or
`GPTR_MCP_ENABLED=1` turns it on. The bootstraps register `mcp_servers.gptr`
on the default and browser profiles with five tools and drop it from
api-server. `gptr-mcp` also stays in `automation`, so `automation` installs
see no change unless they set the flag. Both bootstraps get four new
`GPTR_MCP_*` environment variables, so the first start after upgrading runs
the full bootstrap once. See
[docs/gpt-researcher.md](docs/gpt-researcher.md).

CI: `scripts/test-bootstrap-stamp.sh` uses an if-block for the skipped-run
spawn check, which CI's shellcheck flagged as SC2015 on v0.3.8. Test-only.

## v0.3.8 — 2026-11-03

### Upgrade steps

1. `docker compose up -d`. It recreates `hermes` and both profile bootstraps.
   The first start after upgrading runs the full bootstrap once (about 45 s);
   later starts skip it. Sessions with a cached agent keep the old tool-loop
   setting until the gateway restarts.
2. An overlay that runs `hermes-browser-bootstrap` on its own should set
   `HERMES_BOOTSTRAP_SHARED=1` on it, or the default profile and the other
   profiles are no longer configured.
3. Starting `hermes` alone with `docker compose up hermes` no longer starts
   its MCP sidecars. Start them by name, or use `docker compose up`.

### New optional `.env` keys

- `MCP_PARKED_RETRY_INTERVAL` (default `15`): seconds between probes of a
  parked stack MCP server. Empty keeps Hermes' 300 s.
- `HERMES_BOOTSTRAP_FORCE` (default `0`): `1` runs the full bootstrap even
  when nothing has changed. `make hermes-reload` sets it.

### Host requirements

- Docker Engine 25 or later, for `start_interval` on the `hermes` and
  `hermes-webui` healthchecks.

### Changes

Startup (Boundary CR 0019). On a five-profile stack with Hermes v0.22.5,
`hermes` used to start 31.6 s after `docker compose up`, behind both
bootstraps and the neo4j, lightrag, lightrag-mcp chain. It now starts at
8.2 s and turns healthy at 32.9 s, when the api-server gateway answers.

- **The profile bootstraps skip their work when nothing has changed** since
  the last complete run (R1). They compare their inputs and the config files
  they write with stamps in `data/hermes/.bootstrap-stamps/`; any difference
  runs the full bootstrap. See
  [docs/hermes.md](docs/hermes.md#when-the-bootstrap-runs). A skipped run
  takes 0.5 s.
- **A full bootstrap batches its `hermes config set` keys** in one Python
  process: about 2 CLI calls per launch instead of about 200. The two
  bootstraps took 174 s each; a full run now takes 20 s and 13 s.
- **`hermes-browser-bootstrap` configures the `browser` profile only.**
  `hermes-api-bootstrap`, which runs first, keeps the default profile and the
  sweep over every profile.
- **`hermes` turns healthy only once the api-server gateway answers** `/health`
  on 8643 as well as the dashboard on 9119. The gateway binds after startup
  MCP discovery, and the old check reported healthy about 27 s before chats
  worked.
- **A server down at startup no longer costs every profile Hermes' 3 initial
  connect attempts.** Once one profile has parked it after them, other
  profiles with the same name and target park it after one attempt for
  120 s. With Boundary's MCP server closed, startup discovery took 30 s and
  now takes 9 s. See
  [docs/extending.md](docs/extending.md#mcp-servers-your-app-hosts).
- **`hermes` no longer waits for its MCP sidecars** (R3): `lightrag-mcp`,
  `mcp-searxng`, `caldav-mcp`, `opencode-mcp`, `sql-mcp-ro`, `sql-mcp-rw` and
  `vikunja` are gone from its `depends_on`. A sidecar still booting parks,
  and the bootstrap now sets `parked_retry_interval` on every MCP entry that
  points at a sidecar, so its tools return within about 15 s of it coming
  up. See [docs/hermes.md](docs/hermes.md#what-hermes-waits-for-at-start).
- **A new cont-init step, `007-profile-ownership`, runs before the image's
  `01-hermes-setup`.** It removes sockets left at `gateway.sock` and
  `home/.config/browser-harness/runtime/*.sock`, where nothing binds them
  since those sockets moved to `/tmp`, and chowns the files under
  `profiles/` that are not hermes's. Either kind made `01-hermes-setup` run
  `chown -R` over all of `profiles/` on every boot, which took 55 s while the
  rest of the stack was starting.

Other changes:

- **Tool loops stop on every profile.** The bootstrap sets
  `tool_loop_guardrails.hard_stop_enabled=true`, so a turn ends after 5
  repeats of the same tool call or cycle of calls, including on `api_server`,
  where Hermes defaults to warn-only. Set `HERMES_TOOL_LOOP_HARD_STOP=false`
  to keep warn-only. Tool-loop warnings now follow the
  `<untrusted_tool_result>` wrapper rather than sitting inside it.
- **The `neo4j` healthcheck reads its credentials from `NEO4J_AUTH`** inside
  the container, so the password no longer appears in the probe command that
  `docker inspect` and `docker events` print. It is still in the container's
  environment.

## v0.3.7 — 2026-10-30

### Upgrade steps

1. `docker compose up -d --build llm-proxy`. The proxy code is baked into
   its image. With no fallbacks configured, routing, timeouts and errors are
   as before.

### New optional `.env` keys

- `LLM_PROXY_FALLBACKS_FILE` (default empty): container path of a JSON file
  of fallback upstreams for `chat` and `lightrag`. See
  [docs/extending.md](docs/extending.md#fallback-upstreams).
- `LLM_CONNECT_TIMEOUT` (default `10`): longest wait to connect to an
  upstream that has a fallback after it. The last upstream still waits up to
  `LLM_TIMEOUT`.

### Changes

- **Host scripts run under macOS `/bin/bash` 3.2 again.** `setup.sh` failed in
  `sync-buzz-profile.sh` from v0.3.6, and at the n8n credentials step because
  bash 3.2 reads nothing from `source <(...)`. `todo.sh` has not parsed under
  3.2 since v0.3.3, which stopped `make ensure-local`, `make up` and
  `setup.sh --todo`. `make bash32-test` and a macOS CI job now cover these. An
  install that set `HERMES_BUZZ_PROFILES=,` to work around the first failure
  can drop it. These runs use a stub `docker`, not a live stack.
- **`sync-buzz-profile.sh` does nothing on a stack that has not configured
  Buzz**: with `HERMES_BUZZ_ENABLED` off and `HERMES_BUZZ_PROFILES` and
  `BUZZ_RELAY_URL` empty, it no longer starts a Hermes container or writes
  `platforms.buzz.enabled: false` to the `browser` and `api-server` configs.
  Pins already written stay in place.
- **llm-proxy fallback upstreams**: `chat` and `lightrag` can each carry an
  ordered `fallbacks` list of `{url, api_key, model?}`, set through
  `POST /admin/upstreams` or `LLM_PROXY_FALLBACKS_FILE`. The proxy tries the
  next one when an upstream is unreachable or answers 429, 500, 502, 503 or
  504, never after a header timeout or once a response has started. `embed`
  refuses fallbacks with a 400. `GET /admin/upstreams` gains a `fallbacks`
  field on `chat` and `lightrag`, with `api_key_set` and no keys, and
  `POST /admin/reload` lists `chat.fallbacks` and `lightrag.fallbacks` when
  it re-read the file.
- **llm-proxy** responses carry `X-LLM-Proxy-Upstream: <route>;<index>`
  (0 = primary) unless the upstream sent that header itself. Request log
  lines gain the status and serving upstream
  (`POST /v1/chat/completions 200 upstream=chat;1`), and each attempt the
  proxy moved past is logged with its host and reason.

## v0.3.6 — 2026-10-28

### Not tested on a real install yet

Each of these passes its automated tests but has not had a run on a live
stack with real data. All three are opt-in; a stack that keeps the defaults
is not affected.

- **Boundary under `multiplex`.** WebUI chat, Signal delivery and the ssh
  terminal have run under the multiplexed gateway. Not yet run: a chat over
  the dashboard's `/api/ws` (9119), and `turn.heartbeat` /
  `session.live_status` during a long turn. Known upstream issue under
  `multiplex`: Hermes reads each profile's `.env` on the gateway's event loop
  for every `/p/<profile>/` request, and on Docker Desktop's file sharing a
  read sometimes takes one to several seconds. While one is pending, every
  profile's API waits and the WebUI can report the agent as not responding.
  `standalone` is not affected.
- **Messages.** The indexer, reader app, MCP and alerts have run only
  against a synthetic Messages store. Not yet run: the first real run in
  [docs/messages.md](docs/messages.md#first-real-run), including a new
  message reaching the webhook on 8646 under `multiplex`.
- **IMAP login and TLS.** Tested against GreenMail only. Not yet run: the
  first run on a real IMAP account in [docs/email.md](docs/email.md).

### Upgrade steps

1. `docker compose up -d`. `hermes` gets three new mounts and new
   environment variables, and both profile bootstraps get the topology
   variables, so compose recreates them. The topology stays `standalone`, so
   ports and processes are as before.
2. Optional, for iMessage/SMS: `./scripts/setup.sh --messages`, fill in
   `messages-index.toml`, build and install the reader app
   (`imessage/macos/build.sh`, `imessage/macos/install.sh --config …`) and
   grant it Full Disk Access, `make up`, `make hermes-reload`,
   `make messages-triage-cron`. See [docs/messages.md](docs/messages.md).
   Re-running setup without `--messages` adds `MESSAGES_MCP_ENABLED=0` and
   starts nothing. With messages off, the bootstraps remove any `messages`
   webhook route they added.

### New services

- `messages-mcp` (new **`messages`** profile, off by default): read-only MCP
  over the host-built Messages index plus a forwarder for the host watcher's
  new-message events, built locally from `imessage/` (`python:3.11-slim`,
  `mcp==1.30.0`). Mounts `MESSAGES_INDEX_DIR` read-only and
  `MESSAGES_EVENTS_DIR`. No host port, read-only root filesystem, all
  capabilities dropped, `no-new-privileges`, runs as `HERMES_UID:HERMES_GID`.
  Forwards events to the default profile's webhook listener on
  `MESSAGES_WEBHOOK_PORT` inside the compose network.

### New optional `.env` keys

- `HERMES_GATEWAY_TOPOLOGY` (default `standalone`): `multiplex` runs one
  Hermes gateway for every profile, as upstream intends. Switch with
  `./scripts/gateway-topology.sh multiplex|standalone`, which backs up
  `data/hermes` first. Unset, `HERMES_PIN_STANDALONE_GATEWAYS=0` still means
  `multiplex`.
- `MESSAGES_MCP_ENABLED` (default `0`): `1` registers `mcp_servers.messages`
  on the default and browser profiles and the `messages` webhook route on
  default. `setup.sh --messages` sets it.
- `MESSAGES_INDEX_DIR` / `MESSAGES_INDEX_FILE` (default `./data/messages` /
  `index.sqlite`): the host index, mounted read-only into `messages-mcp`.
- `MESSAGES_EVENTS_DIR` (default `./data/messages-events`): the host
  watcher's outbox, mounted into `messages-mcp`.
- `MESSAGES_WEBHOOK_SECRET` (default empty; `setup.sh --messages` generates
  it): shared by the forwarder and the webhook route. Empty turns new-message
  alerts off.
- `MESSAGES_WEBHOOK_PORT` (default `8646`): the default profile's webhook
  listener inside the `hermes` container (not published).
- `MESSAGES_MCP_URL` (default `http://messages-mcp:8000/mcp`),
  `MESSAGES_MCP_TIMEOUT` / `MESSAGES_MCP_CONNECT_TIMEOUT` (default `60` /
  `30`): endpoint and Hermes MCP timeouts for `messages`.

### Changes

- **IMAP login**: the IMAP source and IMAP drafts use AUTHENTICATE PLAIN
  when the server offers it, and LOGIN otherwise, never both on one
  connection. Passwords with non-ASCII characters now work on servers that
  offer AUTHENTICATE PLAIN. A server that disables LOGIN and doesn't offer
  AUTHENTICATE PLAIN, or a non-ASCII password over LOGIN, fails as a refused
  login (exit code 4, `imap_login_refused`) instead of crashing.
  `make mail-imap-test` now also covers TLS with `tls_ca`, STARTTLS, and the
  Keychain handoff through both mail apps, built against a throwaway keychain
  file (`MAIL_APPS_KEYCHAIN`, for tests).
- docs/email.md's first run on a real IMAP account now starts from a
  terminal with `password_env`, then checks the Keychain password through
  the installed reader app. Before, it had you run `make mail-index-doctor`
  after storing the password in the Keychain, which a terminal run can't
  read.
- **Messages MCP and alerts**: the bootstraps register `mcp_servers.messages`
  on the default and `browser` profiles, never `api-server`.
  `messages_triage_batch` is listed only on default. With
  `MESSAGES_WEBHOOK_SECRET` set, they enable the default profile's webhook
  platform (only if it was off, recorded in `.messages-webhook.json`) with a
  `messages` route that fires the `messages-triage` cron job. Routes you added
  are kept. The route works under both gateway topologies: the default
  profile's gateway opens the webhook listener in `standalone`, and the one
  multiplexed gateway does in `multiplex`. `make messages-triage-cron`
  creates that job with the messages tools only. The cron fence and
  `make doctor`'s cron check now cover `messages` as well as mail.
- **iMessage indexer**: deleted rows (unsent messages, chats that left the
  scope) are overwritten in the index file and dropped from the full-text
  index, and long runs commit every 500 changed rows so an interrupted first
  run resumes.
- **Multiplexed gateway, opt-in.** Under `multiplex` the API of each profile
  is at `http://localhost:8642/p/<profile>/v1`, with that profile's own key.
  Port 8643 (api-server) and the in-network 8644 (browser) keep working
  through `gateway-compat-proxy`, an s6 service in `hermes` that streams
  responses and passes WebSocket upgrades through, so Boundary, n8n and the
  WebUI need no change. The default profile gets its own `API_SERVER_KEY` in
  `data/hermes/.env`, and named profiles that never ran a gateway are parked
  once. Hermes rejects profile keys under 16 characters on `/p/<profile>/`;
  `make doctor` checks both. Details in
  [docs/hermes.md](docs/hermes.md#gateway-topology); the new base URL for apps
  is in [docs/extending.md](docs/extending.md#api-base-urls).
- `POST /v1/mcp/servers/{name}/reconnect` wakes parked connections in the
  answering process at once and adds `woken` (their count) to the 202 body.
- `HERMES_BUZZ_ENABLED=0` now turns Buzz off on the `HERMES_BUZZ_PROFILES`
  agent profiles too (keys kept). Before, their adapters kept running and
  retrying an absent relay, which under `multiplex` shares the process with
  every profile.
- WebUI turns can use `send_message` again (Signal delivery). Hermes v0.22
  dropped it from `hermes-cli` on `api_server`; the browser bootstrap now
  names it.
  Its description now tells the model that `target='signal'` works even
  though Signal is not a connected platform on the browser profile.
- The ssh terminal backend works again. Hermes puts temp files, and with
  them OpenSSH's ControlMaster socket, under `data/hermes/.../cache/scratch`,
  where Docker Desktop's file sharing cannot create the socket. `hermes`
  now sets `TMPDIR=/tmp`.
- `make gateway-preflight` runs Hermes' multiplex migration preflight against
  a temporary copy of the Hermes home and changes nothing.
- `make hermes-upgrade` refuses an agent tag that no longer supports
  `gateway.standalone` while the topology is `standalone`. On such
  an image ports 8643 and 8644 would stop answering.
- The WebUI's `send_message` reads `SIGNAL_*` from the calling profile's
  `.env` before the container environment. No change today, since both hold
  the same values; it keeps a multiplexed gateway from using another
  profile's Signal account.

## v0.3.5 — 2026-10-26

### Upgrade steps

1. `./scripts/setup.sh` to generate `HERMES_DASHBOARD_HANDOFF_SECRET`.
2. `docker compose up -d`. Six new mounts and one new environment variable
   on `hermes`, so compose recreates it. `mcp-searxng` is recreated too, for
   its new `SEARXNG_TIMEOUT_MS`.

### New services

- `search-gate` (profile `search-gate`, opt-in): a proxy in front of SearXNG
  that sends each query to one engine, paces each engine, moves a query to
  the next engine when one blocks, and caches results in the
  `search_gate_data` volume. A search that waits longer than
  `SEARCH_GATE_MAX_WAIT` gets no results and a `search-gate` entry in
  `unresponsive_engines`, and runs later for the cache. Off by default:
  clients keep calling `searxng` until `WEB_SEARCH_URL` points at the gate.
  Vendored from lead-gen, checked by `scripts/test-search-gate.py`. Details
  are in [docs/searxng.md](docs/searxng.md#search-gate).

### New optional `.env` keys

- `HERMES_DASHBOARD_HANDOFF_SECRET` (default empty): signs one-time sign-in
  links for the dashboard. `setup.sh` fills it in. Empty keeps
  `/auth/handoff` off (404).
- `WEB_SEARCH_URL` (default `http://searxng:8080`): the SearXNG-compatible
  base URL that `mcp-searxng`, `gpt-researcher` and `gptr-mcp` search
  through. Set it to `http://search-gate:8080` together with the
  `search-gate` profile.
- `SEARCH_GATE_ENGINES` (default `bing:10,google cse:20`),
  `SEARCH_GATE_MAX_WAIT` (default `15`), `SEARCH_GATE_TTL` (default `86400`)
  and `SEARCH_GATE_EMPTY_TTL` (default `3600`): the search gate's engines and
  pacing, how long a search waits, and cache lifetimes.
- `SEARXNG_TIMEOUT_MS` (default `20000`, image default `10000`): how long
  `mcp-searxng` waits for a search.

### Changes

- **Dashboard sign-in links**: `GET /auth/handoff?code=…` signs a browser in
  from a single-use code that expires within 60 seconds, HMAC-signed with
  `HERMES_DASHBOARD_HANDOFF_SECRET`, and redirects to the code's `next`.
  Password sign-in at `/login` is unchanged. `make dashboard-open NEXT=/kanban`
  mints a link from `.env` and opens it. Code format and redirects are in
  [docs/extending.md](docs/extending.md#sign-in-links).
- **Kanban task links**: `/kanban?task=<id>[&board=<slug>]` opens that task's
  drawer and scrolls its card into view.
- **Dashboard without its navigation**: `?embed=1` hides the dashboard's
  sidebar and headers for the rest of the tab, until `?embed=0`.
- All three are overlays on Hermes v0.22.5 (`patch-dashboard-board.py`,
  `patch-dashboard-handoff.py`, cont-init `13-patch-dashboard`), checked by
  `scripts/test-dashboard-handoff.py`.
- **Speaker names**: `speaker` (`{id, display_name}`) on `prompt.submit`
  names who answers a turn. Its assistant rows carry
  `display_metadata.speaker` in `session.history`, and later turns send those
  rows to the model with a `[speaker: …]` first line. Refused while a turn
  runs (`4085`). `gateway.capabilities` reports `turn_speaker`. An overlay on
  Hermes v0.22.5 (`patch-turn-speaker.py`), checked by
  `scripts/test-turn-speaker.py`. Details are in
  [docs/hermes.md](docs/hermes.md#speaker-names).
- `scripts/test-hosted-rooms.py` checks that upstream hosted rooms
  (`groups.*`) run each member in its own profile.
- **Compose before v5**: `docker compose` on Compose 2.x failed with
  `services.hermes conflicts with imported resource` since v0.1.0. The hermes
  side of each terminal backend moved from `compose/hermes/terminal-*.yml` to
  `docker-compose.hermes-terminal.yml`, which `hermes` extends; the fragments
  keep only `hermes-worker`. The resolved config is unchanged, so nothing is
  recreated.
- **OpenCode image**: `compose/opencode` builds from OpenCode 1.17.6 instead
  of `latest`. An install that built from a newer `latest` moves back to
  1.17.6 on its next build unless it passes `--build-arg OPENCODE_VERSION=…`.
- **`~/` paths**: `setup.sh --data-root ~/…`, `data-dir.sh`, `doctor.sh` and
  the Buzz scripts turned `~/foo` into `$HOME/~/foo`. They now expand it to
  `$HOME/foo`.

## v0.3.4 — 2026-10-25

### Upgrade steps

1. `docker compose up -d`. Three new mounts on `hermes` (`kanban_wake.py`,
   `patch-kanban-wake.py`, `005-remap-hermes-uid`), so compose recreates it.
   With this release the recreated container finishes setup on its first
   boot; no `docker compose restart hermes` is needed afterwards.
2. Hermes Agent v0.22.5 and WebUI 0.52.492 (from v0.22.2 and 0.52.458).
   The overlays no longer apply to v0.22.2. Back up `data/hermes` with
   `hermes` stopped, then run `make hermes-upgrade AGENT=v0.22.5 WEBUI=0.52.492`
   and the verification block in
   [docs/hermes-webui.md](docs/hermes-webui.md#verification). An app that
   sets its own image tags must move both together.
3. With the default ssh terminal backend: `docker compose build hermes-worker`,
   then `docker compose up -d --force-recreate hermes-worker hermes`. The
   rebuilt worker has new SSH host keys, and `hermes` adds them to its
   `known_hosts` only when it starts.

### Changes

- **Hermes v0.22.5 / WebUI 0.52.492**: WebUI 0.52.492 fixes pressing Enter on
  **Deny**, **Allow session** or **Always allow** in a tool approval, which ran
  **Allow once**. Overlay changes for v0.22.5:
  - `patch-history-budget`: the agent-build hook moved with `_make_agent` to
    `tui_gateway/agent_factory.py`. The `history_budget` wire contract and
    error `4028` are unchanged.
  - `patch-mcp-trust-details`: upstream moved the trust-gate prompt into
    `_ask_for_call` and added per-chat spend limits. Arguments still reach
    the approval entry under `mcp`; a spend-limit prompt now carries them
    too, and the summary in the prompt text follows the spend-limit notice.
  - `patch-send-message-tool`: the agent-callable `send_message` was skipped
    on v0.22.5 because its anchor moved. It registers again, and
    `scripts/test-send-message-tool.py` fails if any part of the patch skips.
  - `patch-control-socket-tmp`: v0.22.3 refuses a control socket whose
    directory is not private and accepts a pointer only to
    `hermes-gw-<hash>/control.sock`. The socket moved to
    `/tmp/hermes-gw-<hash>/control.sock`, and `gateway run --replace`
    discovery skips `$HERMES_HOME/gateway.sock` as the client already did.
    `scripts/test-hermes-topology.py` checks this and the
    `019-pin-profile-gateways` pins.
  - `sessions.auto_prune` now defaults to `false` upstream. The
    `019-pin-profile-gateways` pin still writes `false`, which also keeps
    upstream's low-disk prune prompt from appearing.
- **Hermes first boot**: a newly created `hermes` container stopped its
  setup halfway. The image's `01-hermes-setup` runs
  `usermod -u "$HERMES_UID" hermes`, which updates `/etc/passwd` and then fails
  (exit 12) chowning the home directory, the `data/hermes` bind on Docker
  Desktop. The script stops there, so the hermes group stayed 10000 and
  `API_SERVER_KEY` generation and the skills sync were skipped until a restart.
  Every pin move that recreates the container hit this. A new cont-init,
  `005-remap-hermes-uid`, sets the uid and gid before `01-hermes-setup` by
  editing `/etc/passwd` and running `groupmod`, so `usermod` is never called.
  Nothing under `data/hermes` is chowned; ownership on the host is unchanged.
- **Hermes gateway (overlay)**: a dashboard session claims the kanban events
  of tasks it filed before a context compression rotated its key. They used
  to wait forever, even across resumes. The `gave_up` notification, which is
  also the turn that wakes the filing session, says whether a retry can help,
  carries up to 500 characters of the error and names the tools that repair
  the task. See [docs/extending.md](docs/extending.md#telling-the-session-that-filed-a-task).
- **mcp-searxng** 1.15.0 → 2.7.0. Hermes and OpenCode connect, list the same
  four tools and run `searxng_web_search` and `web_url_read` unchanged. A
  client of your own that posts to `http://mcp-searxng:3000/mcp` must now send
  `Content-Type: application/json` (otherwise HTTP 415), and a sessionless
  request must also send `MCP-Protocol-Version`.
- **SearXNG** 2026.8.11-e8e710e42 → 2026.10.24-ef0ec03da. The existing
  `searxng/settings.local.yml` works as is.
- **Ollama** (`ollama` profile) 0.11.11 → 0.40.11. `ollama-pull`, `/api/embed`,
  `/api/chat` and `/v1/chat/completions` behave as before on freshly pulled
  models. The arm64 image is 4.3 GB.
- **hermes-worker** base image alpine:3.21 → 3.24. The worker now has
  Python 3.14 (was 3.12), git 2.54, OpenSSH 10.3 and ripgrep 15.1, so
  `execute_code` runs on Python 3.14. `bash -l`, GNU tar `--no-overwrite-dir`,
  env persistence between commands and key login from `hermes` work as before.

## v0.3.3 — 2026-10-20

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. It adds
   `VIKUNJA_MCP_ENABLED=0` and generates `VIKUNJA_SECRET`. Nothing new starts
   until you opt in with `--todo`.
2. `docker compose up -d`. The `hermes` system prompt (one sentence on where
   living todos live), both bootstrap services' environments, the new
   overlay mounts and `HERMES_TURN_HEARTBEAT_S` change, so compose recreates
   `hermes`.
3. Optional, for todo lists: `./scripts/setup.sh --todo`, `make up`,
   `make todo-init`, re-run `hermes-api-bootstrap` and
   `hermes-browser-bootstrap`, restart `hermes`. See [docs/todo.md](docs/todo.md).
4. Dashboard clients that send `profile` on `prompt.submit`: make sure it
   names the session's own profile, or drop it. A different profile used to
   be ignored silently and is now an error (see Changes).
5. Optional, for mail: `./scripts/setup.sh --mail`, fill in
   `mail-index.toml`, `make mail-index-doctor`, `make mail-index`, `make up`,
   re-run `hermes-api-bootstrap` and `hermes-browser-bootstrap`, restart
   `hermes`. See [docs/email.md](docs/email.md). Re-running setup without
   `--mail` adds `MAIL_MCP_ENABLED=0` and starts nothing.
6. Optional, for email drafts: `./scripts/setup.sh --mail-drafts`, `make up`,
   re-run both bootstrap services, restart `hermes`, and keep
   `make mail-drafts-watch` running on the host. Re-running setup without
   `--mail-drafts` adds `MAIL_DRAFTS_ENABLED=0` and starts nothing.
7. With mail or drafts enabled: rebuild the locally built mail images, since
   `make up` doesn't rebuild them: `docker compose up -d --build mail-mcp`
   (and `mail-drafts-mcp` with drafts). An old `mail-mcp` has no
   `/watermark`, and `mail-triage` then fails on every tick. Then re-run both
   bootstrap services and recreate `hermes`, then `make doctor`. Cron jobs without their own tool list stop
   getting the mail tools; doctor lists any job that still can read mail
   alongside other tools.
8. `docker compose up -d --build llm-proxy` for the new output and total-time
   limits (the proxy is a locally built image), then re-run both bootstrap
   services so every profile gets `model.max_tokens`. Profiles your app
   creates later need it written too (docs/extending.md).
9. With mail enabled, for the mail index's schema 2: first
   `docker compose up -d --build mail-mcp` (the new image reads schema 1 and
   2; the old one refuses 2), then `make mail-apps` and reinstall the reader
   (and drafts) app, then run `make mail-index` once. The run upgrades the
   index in place: ids are kept and nothing is fetched again, so
   `mail-triage` carries on where it was. Drafts requests queued before the
   upgrade still work.
10. Hermes Agent v0.22.2 and WebUI 0.52.458 (from v2026.9.14 and 0.52.113).
    `setup.sh` does not change image tags in an existing `.env`, and the
    overlays no longer apply to calendar-tagged agent images, so a `.env`
    left on `v2026.9.14` starts without them (`make doctor` fails on it).
    Back up `data/hermes` first (with `hermes` stopped). Then run `make hermes-upgrade AGENT=v0.22.2 WEBUI=0.52.458` (pulls both
    images, re-runs both bootstraps, recreates `hermes` and `hermes-webui`)
    and the verification block in [docs/hermes-webui.md](docs/hermes-webui.md#verification).
    An app that sets its own image tags must move both together.
11. Image pins moved (see Changes). `setup.sh` never rewrites an image key
    that is already in `.env`, so update `VIKUNJA_PROXY_IMAGE` or
    `SIGNAL_CLI_IMAGE` if you have set them, or delete the line to take the
    new default. Delete `CALDAV_MCP_IMAGE`: `caldav-mcp` is now built from
    source (see Changes), and an override that sets `platform: linux/amd64`
    on it can go. Then `docker compose up -d`.
12. With `rag` enabled, LightRAG moves from v1.4.9.3 to v1.5.7 (update
    `LIGHTRAG_IMAGE` if it is in `.env`). Back up `rag_storage` and the
    `neo4j_data` volume first. On a Linux host the new entrypoint runs
    `chown -R 1000:1000` on the mounted `rag_storage` and `inputs` before
    dropping to uid 1000, so those host directories change owner. To keep
    them owned by your uid, set `user:` on `lightrag` in an override; the
    entrypoint then skips the chown, and the directories must be writable by
    that uid. Rebuild `lightrag-mcp` too (`docker compose up -d --build
    lightrag-mcp`); without it `get_documents` gets a 405 from v1.5.7.
13. With `automation` enabled, n8n moves from 1.107.4 to 2.43.7 (update
    `N8N_IMAGE` if it is in `.env`). n8n migrates its database on first
    start and the migration only goes forward, so back it up before moving
    the pin. n8n keeps its workflows and credentials in SQLite in the
    `n8n_storage` volume, next to the `config` file that holds its
    encryption key (see item 14). Copy the whole directory:

    ```bash
    docker compose --profile automation stop n8n
    docker compose --profile automation cp n8n:/home/node/.n8n ./n8n-before-2.43.7
    ```

    Then `docker compose up -d`. Workflows you imported yourself that read
    `$env` stop working (see Behaviour changes).
14. With `automation`, n8n's store does not move. n8n has always kept its
    workflows, credentials and owner account in SQLite on `n8n_storage`,
    and still does. Before `docker compose up -d`, an overlay that binds n8n's
    `/home/node/.n8n` elsewhere must bind `n8n-import`'s to the same
    place, or `n8n-import` checks and seeds a store n8n never reads. Drop
    any override of the `postgres` service once your installs are on this
    release; it is deprecated and will be removed. After `up`, remove the
    old container with
    `docker compose --profile n8n-postgres-legacy rm -sf postgres`. Its
    `postgres_storage` volume holds only the demo data `n8n-import`
    seeded; delete it when you no longer want it. Existing installs never
    got the starter workflows in n8n; to add them, see
    [docs/n8n.md](docs/n8n.md#upgrading-an-existing-install).
15. `docker compose up -d` recreates `hermes` for the two kanban overlays
    (five mounts and `HERMES_KANBAN_GUARDS`). A client that files kanban
    tasks with `workspace_kind="worktree"` or `"dir"` should be ready for
    `kanban_create` and `POST /tasks` to refuse them (see Behaviour changes).

### New services

- `vikunja` (new **`todo`** profile, off by default): [Vikunja](https://vikunja.io)
  2.7.0 (AGPL-3.0) with its built-in MCP server. No host port. It joins
  `default` and the internal `db` network and keeps its data in
  schema `vikunja` of the shared Postgres. `todo` is added to
  `x-db-profiles`, so it starts `postgres-db`, `db-provision`, and the SQL MCP
  sidecars too. The SQL MCP is still registered only with `SQL_MCP_ENABLED=1`.
- `vikunja-proxy` (`todo` profile): `nginx:1.29.1-alpine` on `127.0.0.1:3456`
  in front of `vikunja`. Passes everything through and serves `/login-local`,
  which signs the browser in as the stack-managed account. About 4 MiB of RAM,
  capped at half a CPU.
- `mail-mcp` (new **`mail`** profile, off by default): read-only MCP over
  the host-built mail index, built locally from `compose/mail-mcp`
  (`python:3.11-slim`, `mcp==1.30.0`). No host port. Mounts only
  `MAIL_INDEX_DIR`, read-only, with a read-only root filesystem, all
  capabilities dropped and `no-new-privileges`. Runs as `HERMES_UID:HERMES_GID`.
- `mail-drafts-mcp` (new **`mail-drafts`** profile, off by default): MCP with
  `mail_draft_create` and `mail_draft_status`, built locally from
  `compose/mail-drafts-mcp` (`python:3.11-slim`, `mcp==1.30.0`). It only
  queues requests in `MAIL_DRAFTS_DIR`; it doesn't mount the mail index and
  can't send. No host port, read-only root filesystem, all capabilities
  dropped, `no-new-privileges`, runs as `HERMES_UID:HERMES_GID`.

### Required `.env` keys

Only with `todo` enabled.

- `VIKUNJA_SECRET`: `setup.sh` generates it for every install. If it is empty,
  Vikunja picks a new one each start and everyone is logged out.
- `VIKUNJA_MCP_TOKEN_RW` and `VIKUNJA_MCP_TOKEN_RO`: written by
  `make todo-init`. Without them the bootstrap logs a warning and registers
  nothing.
- `VIKUNJA_USERNAME` and `VIKUNJA_PASSWORD`: written by `make todo-init`.
  Without them `/login-local` shows an error and asks you to run it.

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `VIKUNJA_MCP_ENABLED` | `0` | `1` registers `mcp_servers.todos` on Hermes. `setup.sh --todo` sets it |
| `VIKUNJA_PORT` | `3456` | Loopback host port for the web UI |
| `VIKUNJA_PUBLIC_URL` | `http://localhost:${VIKUNJA_PORT}/` | Base URL in Vikunja's links and MCP page |
| `VIKUNJA_ENABLE_REGISTRATION` | `false` | Allow sign-up from the web UI |
| `VIKUNJA_SESSION_TTL` | `315360000` | Sign-in lifetime in seconds (10 years) |
| `VIKUNJA_PROXY_IMAGE` | `nginx:1.29.1-alpine` | `vikunja-proxy` image |
| `VIKUNJA_FILES_DIR` | `./data/vikunja/files` | Attachments bind mount |
| `VIKUNJA_IMAGE` | `vikunja/vikunja:2.7.0` | Vikunja image (MCP needs 2.7.0+) |
| `VIKUNJA_MCP_URL` | `http://vikunja:3456/api/v2/mcp` | Endpoint the bootstrap registers |
| `VIKUNJA_MCP_TIMEOUT` / `VIKUNJA_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `vikunja` |
| `MAIL_MCP_ENABLED` | `0` | `1` registers `mcp_servers.mail` on the default and browser profiles. `setup.sh --mail` sets it |
| `MAIL_INDEX_DIR` | `./data/mail` | Host folder holding the index; mounted read-only into `mail-mcp` |
| `MAIL_INDEX_FILE` | `index.sqlite` | Index file name inside `MAIL_INDEX_DIR` |
| `MAIL_MCP_URL` | `http://mail-mcp:8000/mcp` | Endpoint the bootstrap registers |
| `MAIL_MCP_TIMEOUT` / `MAIL_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `mail` |
| `MAIL_DRAFTS_ENABLED` | `0` | `1` registers `mcp_servers.drafts` on the default and browser profiles. `setup.sh --mail-drafts` sets it |
| `MAIL_DRAFTS_DIR` | `./data/mail-drafts` | Host folder for queued requests and their status; mounted into `mail-drafts-mcp` |
| `MAIL_DRAFTS_MCP_URL` | `http://mail-drafts-mcp:8000/mcp` | Endpoint the bootstrap registers |
| `MAIL_DRAFTS_MCP_TIMEOUT` / `MAIL_DRAFTS_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `drafts` |
| `LLM_DEFAULT_MAX_TOKENS` | `16384` | llm-proxy adds `max_tokens` to chat/completions requests that set no output limit. `0` = off |
| `LLM_STREAM_MAX_SECONDS` | `3600` | Longest one llm-proxy request may take, start to finish. `0` = no limit |
| `HERMES_MAX_TOKENS` | `16384` | `model.max_tokens` bootstrap writes to proxy profiles that have none. `0` = write nothing |
| `HERMES_PIN_STANDALONE_GATEWAYS` | `1` | `0` stops writing `gateway.standalone: true` into named profiles at `hermes` start (see Behaviour changes) |
| `HERMES_PIN_SESSIONS_KEEP` | `1` | `0` stops writing `sessions.auto_prune: false` into profiles at `hermes` start |
| `HERMES_KANBAN_GUARDS` | `1` | `0` turns off the kanban guards overlay: no workspace check at filing, the two-strike limit for every workspace error, no creator comment |

### Behaviour changes

These change defaults, to stop a model that never stops generating (a local
model ran one reply for 13 hours with no `max_tokens`; every timeout watched
only for silence). Set the keys above to `0` to get the old behaviour back.

- llm-proxy adds `max_tokens: 16384` to generation requests on the chat and
  LightRAG routes that set none of `max_tokens`, `max_completion_tokens` or
  `max_output_tokens`. A request with its own limit is unchanged.
- llm-proxy drops any request still running after 3600s, the same way as an
  idle stream (dead connection, upstream closed).
- Bootstrap sets `model.max_tokens=16384` on every proxy profile that has no
  value. Existing values, including `browser`'s 1024, are kept.
- **n8n 2.x blocks `$env` in workflow expressions** by default
  (`N8N_BLOCK_ENV_ACCESS_IN_NODE=true`), and this stack leaves it on. A
  `GPTR_URL` set in `.env` no longer reaches n8n; edit the URL in the
  workflow instead.

Hermes v0.22 changes two defaults that this stack sets back:

- **One gateway per profile.** v0.22 runs a single gateway that serves every
  profile and moves each profile's API server to `/p/<profile>/` on the
  default listener. The new cont-init `019-pin-profile-gateways` writes
  `gateway.standalone: true` into each named profile's `config.yaml` where the
  key is unset, before the image's reconciler runs. `api-server` stays on 8643
  and `browser` on 8644, a profile that ran a gateway still runs its own, and
  a profile that never ran one stays idle. Upstream marks `gateway.standalone`
  as temporary; see [docs/hermes.md](docs/hermes.md#gateway-topology).
- **Sessions are kept.** v0.22 deletes ended sessions inactive for 90 days on
  startup (`sessions.auto_prune: true`). The same script writes
  `sessions.auto_prune: false` into every profile where the key is unset. The
  `api-server` and `browser` profiles already had it; the default profile and
  agent profiles such as Buzz employees did not, and pruned under v2026.9.14
  too. A value set by hand is kept.

n8n's store:

- **n8n's Postgres is no longer started.** The `postgres` service (container
  `n8n-postgres`) moves from the `automation` profile to
  `n8n-postgres-legacy`. n8n never read it: the `n8n` service's environment
  list replaced the `x-n8n` anchor's, so the `DB_*` settings only reached
  `n8n-import`. `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` and
  `POSTGRES_IMAGE` are only used by that legacy service.
- **`n8n-import` seeds n8n's SQLite store**, and only when
  `database.sqlite` does not exist yet. It used to seed Postgres whenever
  Postgres had no workflows, and n8n never saw the result. A fresh install
  now gets the six starter workflows and both demo credentials in n8n.

n8n's environment:

- **n8n no longer sends diagnostics or shows the personalization survey.**
  The `n8n` service now merges the `x-n8n` anchor's environment instead of
  replacing it, so it gets `N8N_DIAGNOSTICS_ENABLED=false` and
  `N8N_PERSONALIZATION_ENABLED=false`, which only `n8n-import` had before.
  n8n also gets `LIGHTRAG_URL`, `NEO4J_HTTP_URL` and `SEARXNG_URL` for
  workflows to read. `docker compose up -d` recreates `n8n`. To turn
  diagnostics back on, set `N8N_DIAGNOSTICS_ENABLED: "true"` on `n8n` in a
  compose override file.

Kanban workspaces (overlay, `patch-kanban-guards.py`; `HERMES_KANBAN_GUARDS=0`
restores upstream behaviour):

- **Filing refuses a workspace the dispatcher can't build.** `create_task`
  now runs the spawn-time workspace checks once the project is resolved, so
  `kanban_create` returns a tool error and the dashboard's `POST /tasks` a
  400, and no task is filed. It refuses `worktree` with no path, no project
  and no board `default_workdir`; a path that isn't absolute or isn't in a
  git repo; and a repo, target or git dir this container can't write to. It
  refuses `dir` with no path, or one under a directory it can't write to.
  With the SSH terminal backend `/opt/projects` is read-only in `hermes`, so
  `worktree` tasks on those repos, including tasks linked to a project
  there, are now refused when filed instead of giving up later. `scratch` is unchanged.
- **A workspace error that can't heal gives up on the first claim.** A
  missing path, a path that isn't absolute or isn't in a git repo, or a
  repo the container can't write to blocks the task straight away. The block is sticky, so only an unblock releases
  it. The `gave_up` payload has `"retryable": false`, as does the give-up
  for a revoked credential or missing model (`terminal_provider`). Other
  give-ups have no `retryable` key and still use the failure limit.
- **A give-up comments on the creator task.** When a task filed by another
  task's worker gives up, the creator task gets a comment from
  `kanban-dispatcher` naming the task and its error.

### Changes

- **MCP trust approvals show the call** (`patch-mcp-trust-details.py`). When
  a write-capable tool on a `trust: untrusted` server asks for approval, the
  approval entry carries `mcp: {server, tool, arguments, tool_call_id}`, and
  the prompt text adds a one-line argument summary. Arguments are redacted
  and capped before they are stored, since `approval.pending` replays the
  entry as stored. Such approvals offer only `once` and `deny`: the gate
  accepted `session` and `always` as a single approval and remembered
  neither. Two different calls to one tool no longer share a prompt. With
  identical text, the second used to wait on the first and adopt its deny.
  See [docs/extending.md](docs/extending.md#mcp-approvals-for-untrusted-servers).
- **`profiles.create` takes `mirror_exclude`** (`patch-mirror-exclude.py`),
  glob patterns for keys the mirrored `.env` leaves out, so an agent created
  without calendar access holds no CalDAV password. Without it, nothing
  changes. See [docs/extending.md](docs/extending.md#keys-a-new-profile-leaves-out).
- **Gateway-registered calendars** are a supported path. The bootstrap
  removes only `caldav-*` entries, so calendars an app registers through
  the gateway under another prefix keep their entry and `.env` key on every
  bootstrap run. Documented in
  [docs/calendar.md](docs/calendar.md#calendars-registered-by-other-clients).
  `make calendar-test` runs the CalDAV bootstrap checks and both overlay
  tests.
- **n8n**: `n8n` and `n8n-import` set `DB_TYPE=sqlite` and both mount
  `n8n_storage` at `/home/node/.n8n`. `n8n-import` no longer depends on
  `postgres`, and checks for the database file instead of running
  `n8n list:workflow`, so a `docker compose up` that restarts it next to a
  running n8n never opens the live database.
- **n8n**: `scripts/generate-n8n-credentials.js` writes credentials n8n can
  decrypt. It base64-encoded the OpenSSL envelope in pieces, and n8n
  rejected both demo credentials with "Decrypted credentials data is not
  valid JSON". Re-run `./scripts/setup.sh` (or the script) to regenerate
  them.
- **Hermes v0.22.2 / WebUI 0.52.458**: Hermes Agent tags moved from calendar
  versions (`v2026.9.14`) to semver (`v0.21.6`, `v0.22.0`, `v0.22.2`). A semver
  tag is newer than every calendar tag. The overlays were re-anchored to
  v0.22.2:
  - Re-anchored: `patch-fabricated-result`, `patch-history-budget`,
    `patch-mcp-wake`, `patch-send-message-tool`, `patch-tool-search-visible`,
    `patch-tool-search-always-include`, and the WebUI
    `patch-webui-projects-library` (now wraps `load_workspaces` instead of
    rewriting it). `history_budget` now declares its contract fields at
    install time like the client-context overlay; its wire contract and
    error code `4028` are unchanged. The `POST /v1/mcp/servers/{name}/reconnect`
    contract is unchanged; `mcp_wake.py` reads YAML through `hermes_yaml`,
    because the v0.22 image has no PyYAML and the endpoint would otherwise
    fail on every call. `tool_search` no longer reports curated-deferred core
    tools (`cronjob_manage`, `todo_list`, `session_search`) as already active.
  - Dropped, covered upstream: `patch-reasoning-only-tool-markup` (v0.22
    promotes reasoning to the answer only on trusted routes; this stack's
    llm-proxy route re-prompts, checked by
    `scripts/test-reasoning-promotion-gate.py`),
    `patch-cron-default-deliver-signal` (v0.22 delivers a WebUI cron job with
    no origin to the Signal home channel), `patch-agent-browser-npx` (v0.22
    has no npx path; `08-warmup-agent-browser` still installs the CLI),
    `patch-webui-persisted-count` (fixed in WebUI #7672).
  - Dropped, dead: `patch-openrouter-empty-stream`. Its retry and message
    edits had not applied since v2026.9.
  - New overlay `patch-control-socket-tmp` (with `HERMES_CONTROL_SOCKET_IN_TMP=1`
    on `hermes`) binds the v0.22 gateway control socket on `/tmp`. On
    `data/hermes` (virtiofs) the socket cannot be connected to, and every
    per-profile gateway restarted in a loop.
  - The bootstrap scripts, `sync-buzz-profile.sh` and
    `scripts/lib/hermes-yaml.sh` fall back to `hermes_yaml` when PyYAML is
    missing, as it is in v0.22.
  - `make doctor` fails when `HERMES_AGENT_IMAGE_TAG` is a calendar tag, and no
    longer looks for the cron-default-deliver marker.
- **Image pins**: security and client-age bumps. `vikunja-proxy` moves from
  `nginx:1.29.1-alpine` (an end-of-life mainline release) to
  `nginx:1.30.6-alpine`. `signal-cli` moves from `signal-cli-jre:v0-14-7-2`
  to `v0-14-9-2`, ahead of Signal retiring older clients; the linked account
  store is read as before.
- **caldav-mcp** 0.1.1, built locally. Compose builds upstream's v0.1.1
  commit with upstream's Dockerfile and tags it
  `${IMAGE_PREFIX:-assistant}-caldav-mcp:0.1.1`, so the `calendar` profile
  runs natively on Apple Silicon. Upstream publishes linux/amd64 only. The
  old default, `ghcr.io/gelse/caldav-mcp:v0.1.0`, is not a published tag
  (upstream tags have no `v`), so that pin never pulled. `CALDAV_MCP_IMAGE`
  is no longer read. The first `up` with `calendar` fetches the source from
  GitHub.
- **LightRAG** v1.4.9.3 → v1.5.7, for the advisories fixed in 1.5.3–1.5.7
  (guest-token auth bypass, Cypher injection, upload path traversal). The
  server runs as uid 1000 instead of root. Without an API key, `/health`
  now returns only status fields (`status`, `core_version`,
  `pipeline_busy` and the like); `configuration`, queue and worker details
  need `X-API-Key`. The compose healthcheck and the `check_lightrag_health`
  MCP tool still pass. Entity and relation deletes moved from
  `/documents/delete_entity` and `/documents/delete_relation` to
  `/graph/entity/delete` and `/graph/relation/delete`.
- **lightrag-mcp**: `delete_by_entities`, `delete_by_doc_ids`,
  `create_entities`, `edit_entities`, `create_relations`, `edit_relations`
  and `merge_entities` call routes LightRAG does not serve (`/entities/…`,
  `/relations/…`, `/merge`, `DELETE /documents/{id}`), on v1.4.9.3 and
  v1.5.7 alike. LightRAG answers 404 and lightrag_mcp 0.1.1 reports
  `"status": "success"` with a `null` result. `insert_document` gets a 400
  from `POST /documents/text` on v1.5.7 and also reports `"status":
  "success"`, with `"None"` as the result. Queries, uploads and health work.
- **lightrag-mcp**: LightRAG v1.5 no longer serves `GET /documents` (405),
  so the image patches `get_documents` to page through
  `POST /documents/paginated`. The tool name and its output,
  `{"statuses": {"<status>": [doc, ...]}}`, are unchanged. Documents carry
  v1.5's fields (`track_id`, `error_msg`, with `error` copied from it), and
  statuses include v1.5's `parsing` and `analyzing`. Rebuild with
  `docker compose up -d --build lightrag-mcp`.
- **n8n** 1.107.4 → 2.43.7, past the advisories filed against 1.107.4.
  The bundled `WfGptrResearch01` workflow calls
  `http://gpt-researcher:8000/report/` directly instead of
  `{{ $env.GPTR_URL }}`, and `GPTR_URL` is no longer passed to `n8n` or
  listed in `.env.example`. n8n 2.x calls the workflow toggle **Publish**
  instead of **Activate**; the docs say publish.
- **Hermes bootstrap**: registers `mcp_servers.todos` (named for what users
  call it; an earlier `mcp_servers.vikunja` entry is removed) with an
  `Authorization: Bearer ${VIKUNJA_MCP_TOKEN}` header. The read-only token goes
  to `api-server`, the read/write token to default and `browser`, each into
  that profile's `.env`. With the profile off or the token missing, it removes
  the entry and the token.
- **`living-todos` skill**: uses the Vikunja tools when they are listed, and
  the markdown file otherwise. The Hermes system prompt says the same.
- **`make todo-init`** (`scripts/todo.sh`): asks nothing. It creates the
  Vikunja account (or adopts the only existing one) with a generated password
  kept in `.env`, mints both tokens, and recreates `vikunja-proxy`.
  Re-running it rotates the tokens. An adopted account's old password stops
  working. Both tokens add kanban on top of Vikunja's *Typed* preset: both
  read project views and buckets, and the read/write token also creates,
  renames and deletes buckets and moves tasks between them. Tokens minted
  before this change have no bucket access; re-run `make todo-init`, both
  bootstrap services, and restart `hermes`.
- **`make hermes-reload`**: runs both Hermes profile bootstraps and recreates
  `hermes`, the three steps most config changes need. `make hermes-upgrade`
  uses it, and the docs, `make doctor` hints, and `make todo-init` point to it.
- **`make todo-open`**: opens `http://localhost:3456/login-local`, signed in.
- **`make ensure-local` / `make up`**: with `todo` in `COMPOSE_PROFILES`,
  writes `compose/postgres/services/vikunja.env` if it is missing.
- **`make doctor`**: a todo section (profile, secret, database login, tokens,
  per-profile registration, `api-server` not holding the read/write token,
  container and HTTP health) and a port check for 3456.
- **Hermes kanban (overlay)**: `PATCH /tasks/{id}` takes `workspace_kind`,
  `workspace_path`, `clear_workspace_path` and `project_id` while the task
  isn't running (409 while it runs, 400 for a workspace that can't be built,
  and then none of the PATCH is applied). The task keeps its id, runs,
  comments and links, and an `edited` event records the old and new values.
  New orchestrator-only tools `kanban_update` (the same change) and
  `kanban_archive` (refuses a running task) sit beside `kanban_unblock` in
  the `kanban` toolset. Clients that don't send the fields see no change. See
  [docs/extending.md](docs/extending.md#changing-a-tasks-workspace).
- **Hermes gateway (overlay)**: while a turn runs, the gateway sends a
  `turn.heartbeat` event every 30 s (`HERMES_TURN_HEARTBEAT_S`, `0` for off)
  with `status`, `turn_started_at` and `idle_s`, and never after
  `message.complete`. New `session.live_status` returns one session's
  `status`, `running`, `turn_started_at` and `idle_s` by `session_id` or
  `session_key`, without a database read; a session that is not live answers
  `idle`. `gateway.capabilities` advertises both. Clients that ignore them see
  no change. See [docs/hermes.md](docs/hermes.md#turn-liveness).
- **Hermes gateway (overlay)**: `session.create`, `session.resume` and
  `prompt.submit` take an optional `context` string. Session context is added
  to the system prompt. Turn context is added to that turn's model input. The
  user's message and `session.history` stay as typed. Errors are `4080`
  (invalid), `4082` (a turn is running) and `4083` (isolated compute-host
  turns). `gateway.capabilities` advertises `client_context` and
  `prompt_profile_check`. Clients that leave the field out see no change. See
  [docs/hermes.md](docs/hermes.md#client-context).
- **Hermes gateway (overlay)**: `prompt.submit` with a `profile` that differs
  from the session's profile now fails with `4081` and
  `data.session_profile`. It used to be accepted and ignored: the turn ran on
  the session's profile. A matching or omitted `profile` behaves as before.
- **Mail indexer** (host-side, opt-in): `scripts/mail-index.py` indexes the
  accounts, mailboxes and date window named in a `mail-index.toml` config
  (copy `mail-index.toml.example`) into a local SQLite file with full-text
  search. By default it reads through Mail.app scripting, which needs the
  Automation permission for Mail and not Full Disk Access. With
  `source = "store"` it reads a snapshot of Mail's files instead, which is
  faster but needs Full Disk Access and a `store` path (there's no default).
  It never writes to Mail and prints counts and timings only. Bodies and
  attachment names are separate opt-ins. Exit code 4 means macOS refused the
  permission; 5 means a call to Mail failed or timed out. `make mail-index`,
  `make mail-index-doctor`, `make mail-index-mailboxes`,
  `make mail-index-test`, `make mail-script-test` (needs node). See
  [docs/email.md](docs/email.md).
- **Mail MCP** (`mail` profile): `mail_status`, `mail_search`, `mail_get` and
  `mail_thread` over the index. Every result carries a notice that message
  text is untrusted, and bodies come between markers that a message can't
  forge. Registered on the default and browser profiles only. With the
  profile off, or on `api-server`, the bootstrap removes any entry pointing
  at it. `make doctor` gets a mail section (profile, flag, index present,
  folder mode, per-profile registration, `api-server` not registered).
  `make mail-mcp-test`.
- **Mail drafts** (`mail-drafts` profile): Hermes can queue email drafts, and
  `scripts/mail-drafts.py` on the host saves them to Mail's Drafts through
  scripting. Nothing is ever sent; you send drafts from Mail. Replies thread
  through Mail's own reply. Requests are checked twice (in the container and
  on the host): plain addresses only, at most 20 recipients, one-line
  subjects, no attachments, a sender only from your own Mail accounts, and
  at most 20 drafts an hour. Registered on the default and browser profiles
  only. `make mail-drafts`, `make mail-drafts-watch`,
  `make mail-drafts-doctor`, `make mail-drafts-test`; `make doctor` gets a
  drafts section.
- **Mail helper apps** (macOS, opt-in): `make mail-apps` builds
  *Assistant Mail Reader* and *Assistant Mail Drafts*, each a small signed
  app carrying only its own helper's scripts, so each holds its own
  Automation permission (and Full Disk Access only for the reader, only with
  `source = "store"`) instead of your terminal or `python3`.
  `macos/mail-apps/install.sh` copies one to `~/Applications` and loads a
  LaunchAgent: the reader every 15 minutes, the drafts helper continuously.
  Needs `swiftc` and Python 3.11+. `make mail-apps-test`. Build output goes
  to the gitignored `build/`.
- **Mail MCP**: `mail_search` takes `after_id`, `mail_status` reports
  `newest_id`, and `GET /watermark` returns `newest_id=<n>`, which changes
  only when newer mail is indexed and holds no message content.
- **Mail triage cron** (opt-in): `make mail-triage-cron` creates a
  `mail-triage` Hermes cron job on the default profile with the mail tools
  only, which runs only when newer mail is indexed (`--monitor-url` on the
  watermark) and reports to Signal when `SIGNAL_HOME_CHANNEL` is set.
  `make mail-triage-cron-show`, `make mail-triage-cron-remove`,
  `make mail-cron-test`. See [docs/email.md](docs/email.md#proactive-review-cron).
- **Hermes bootstrap**: while mail or drafts is registered on a profile, the
  bootstraps set `platform_toolsets.cron` to `hermes-cron` plus every other
  MCP server, so cron jobs without their own tool list no longer get the mail
  tools. They keep everything else. A `platform_toolsets.cron` you wrote is
  kept, minus mail and drafts. The list is removed again when mail and drafts
  are both off. MCP servers added by hand reach cron jobs after the next
  bootstrap run. `make doctor` flags any cron job that can read mail
  alongside other tools.
- **Mail indexer**: deleted text is overwritten. The index turns on SQLite's
  `secure_delete` and FTS5's `secure-delete` (SQLite 3.42+), so a message
  that leaves scope, or a body blanked by turning `bodies` off, is gone from
  the file at once. It's compacted once for older indexes, after removals on
  older SQLite, and when over a quarter of it is free space. `run` reports
  `compacted`.
- **Mail indexer**: in `mailboxes` patterns, `*` now matches within one
  level and `**` matches any depth. `*` used to match nested mailboxes too,
  so a config using `*` or `Name/*` may index fewer mailboxes; use `**` to
  keep them. `run` and `doctor` report `mailboxes_missing` for exact names
  that weren't found.
- **Mail indexer (script source)**: mailboxes are looked up by path instead
  of walking the account's whole mailbox tree for every lookup, and accounts
  whose `mailboxes` are all exact names aren't listed at all. On accounts
  with many mailboxes this removes tens of seconds per run and per batch of
  50 messages. The drafts helper's reply lookup gets the same change.
- **Mail indexer**: saves progress every 50 messages, so a long first run
  that's cut short resumes on the next run instead of starting over.
- **Mail providers**: sources are now chosen per account
  (`[[accounts]] source = …`), and one index can mix them. See
  [docs/email.md](docs/email.md#providers).
  - Index schema 2 numbers messages itself, counting up and never reusing
    an id, and keeps each source's own id as `source_id`. `newest_id` and
    `after_id` now mean "indexed after" across every account. Results gain a
    `reply_ref`, which `mail_draft_create` takes as `reply_to`; the older
    `{id, account, mailbox}` form still works.
  - New `imap` source (opt-in): reads an IMAP account with `EXAMINE` and
    `BODY.PEEK[]`, so nothing on the server changes. The password lives in
    the Keychain (`macos/mail-apps/install.sh password <id>`) and the mail
    apps' launcher hands it to the helper on stdin; terminal runs use
    `password_env`. Proton works through Proton Mail Bridge.
  - Drafts for IMAP accounts are appended to the account's Drafts folder
    (`mail-drafts.py --config`, `--no-mail-app` without Mail.app). Still no
    way to send.
  - Message bodies are stored with LF line endings whatever the source.
  - `make mail-imap-test` checks the IMAP source and drafts against a
    throwaway GreenMail server (`greenmail/standalone:2.1.3`, pulled from
    Docker Hub on first run) loaded with the synthetic mail. It's skipped
    when Docker isn't running.
- **Docs**: [docs/email.md](docs/email.md) records the design for giving Hermes
  read access to Apple Mail, why scripting Mail.app is the default over Full
  Disk Access, and a survey of existing Apple Mail MCP servers.
  `scripts/make-synthetic-mail.py` writes a fake Mail store for developing
  the reader, and `make synthetic-mail-test` checks it.
- **iMessage indexer** (host-side, not wired into Hermes yet): `imessage/`
  holds a self-contained, stdlib-only reader for Messages.app history.
  `make messages-index` reads `chat.db` read-only into a scoped SQLite + FTS
  index; `make messages-index-doctor` and `make messages-index-chats` help
  write `messages-index.toml` (gitignored; template in
  `imessage/messages-index.toml.example`). It needs Full Disk Access and has
  been tested only against synthetic data. `make messages-index-test` runs
  its tests. See [imessage/README.md](imessage/README.md).
- **iMessage watcher and app** (host-side, not wired into Hermes yet):
  `make messages-watch` keeps the index current and, with a `[notify]`
  table, raises metadata-only new-message events to an outbox folder or a
  signed HTTP endpoint (Hermes generic-webhook HMAC V2). `make messages-app`
  builds *Assistant Messages Reader*, a sealed macOS app that holds Full Disk
  Access instead of your terminal, and `imessage/macos/install.sh` loads a
  LaunchAgent that runs the watcher. New host requirement only if you use
  it: grant the app Full Disk Access. Tests: `make messages-watch-test`,
  `make messages-app-test`.
- **Tests**: `imessage/make-synthetic-messages.py` writes a fake Messages.app
  store (`chat.db`, attachments, Contacts) for developing the iMessage reader,
  and `make synthetic-messages-test` checks it. See
  [imessage/README.md](imessage/README.md#synthetic-store).
- **Docs**: [docs/messages.md](docs/messages.md) records the design for giving
  Hermes read access to iMessage history and new-message events, and the
  open-source readers evaluated for it. Nothing reads messages yet.
- **Docs**: [docs/hermes.md](docs/hermes.md#platform-hints) documents
  `agent.platform_hints.<source>` for clients whose `source` has no built-in
  hint, and corrects the claim that `profile` works per turn.

## v0.3.2 — 2026-10-11

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. It adds
   `SQL_MCP_ENABLED=0` and generates the three `DB_*_PASSWORD` keys. Nothing
   new starts until you opt in with `--db`.
2. `docker compose up -d`. The `hermes` system prompt, mounts, and networks
   (it joins a new internal `db` network beside `default`, even with `db`
   off) and the `opencode-mcp` environment change, so compose recreates both.
   Compose creates the `hermes_uv_cache` volume and the `db` network. An
   override that sets `networks:` on `hermes` must keep `default` and `db`.
3. Optional: delete the old per-profile uv caches, which nothing reads any
   more (several hundred MB each):
   `rm -rf data/hermes/home/.cache/uv data/hermes/profiles/*/home/.cache/uv`.
4. Optional, to use the shared database: `./scripts/setup.sh --db`, `make up`,
   re-run `hermes-api-bootstrap` and `hermes-browser-bootstrap`, restart
   `hermes`. See [docs/database.md](docs/database.md).

### New services

All in the new **`db`** profile, off by default. They are separate from n8n's
`postgres` (`automation`), which is unchanged.

- `postgres-db`: shared Postgres 18 with one database and a schema per owner.
  No host port.
- `db-provision`: one-shot role, schema, and grant reconciliation on every
  `up`.
- `sql-mcp-ro` / `sql-mcp-rw`: [postgres-mcp](https://github.com/crystaldba/postgres-mcp)
  0.3.0 as `agent_ro` (restricted mode) and `agent_rw`. Hermes registers ro on
  `api-server` and rw on the default and `browser` profiles.

### Required `.env` keys

Only with `db` enabled. `setup.sh` fills all three for every install.

- `DB_ADMIN_PASSWORD`, `DB_AGENT_RO_PASSWORD`, `DB_AGENT_RW_PASSWORD`. If
  they are empty, `postgres-db` refuses to initialise and `db-provision`
  exits non-zero. Compose does not fail on them, because it parses the
  service with the profile off.

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `CODING_ROOTS_APP_MANAGED` | empty | `1`: the `needs_access` hint says the app will ask for access instead of naming `make coding-root-add` |
| `CONTEXT_ROOTS_APP_MANAGED` | empty | `1`: the Hermes prompt says the app will ask for a missing context root instead of naming `make context-root-add` |
| `HERMES_UV_CACHE_DIR` | `hermes_uv_cache` | Host path to use for the shared uv cache instead of the named volume |
| `CODING_PATH_ALIASES` | empty | `;`-separated `hermes-path=host-path` pairs for project folders that are also host folders |
| `SQL_MCP_ENABLED` | `0` | `1` registers the shared-database SQL MCP on Hermes. `setup.sh --db` sets it |
| `DB_NAME` | `assistant` | Shared database name (`db` profile) |
| `DB_DATA_DIR` | `./data/db` | Shared Postgres bind mount |
| `DB_BACKUP_DIR` | `./data/db-backups` | `make db-backup` output |
| `DB_IMAGE` | `postgres:18-alpine` | Shared Postgres image |
| `SQL_MCP_IMAGE` | `crystaldba/postgres-mcp:0.3.0` | SQL MCP sidecar image |
| `SQL_MCP_RO_URL` / `SQL_MCP_RW_URL` | `http://sql-mcp-{ro,rw}:8000/sse` | Endpoints the bootstrap registers |
| `SQL_MCP_TIMEOUT` / `SQL_MCP_CONNECT_TIMEOUT` | `60` / `30` | Hermes MCP timeouts for `sql` |

### Changes

- **opencode-mcp**: `coding_start_task` translates a path listed in
  `CODING_PATH_ALIASES` (or a path under it) to its host path before
  checking the roots, and `coding_list_roots` returns the pairs as `aliases`.
  An alias does not grant access. See
  [docs/opencode.md](docs/opencode.md#project-folder-aliases).
- **opencode-mcp**: the default `needs_access` hint names `hermes-worker`
  (or `hermes` on the local backend) as the container to recreate, instead
  of `hermes`.
- **Hermes**: the system prompt no longer repeats the `make coding-root-add`
  steps. It tells the model to follow the `needs_access` hint, so the hint is
  the one source for that text. It lists the alias pairs when
  `CODING_PATH_ALIASES` is set.
- **Hermes**: the `delegate-coding` skill (1.3.0) follows the `needs_access`
  hint. When the hint says the app will ask, the model names the folder in
  one sentence and ends the turn. It uses the alias pairs for "this project"
  instead of matching folder names against the roots.
- **Hermes**: `POST /v1/mcp/servers/{name}/reconnect` on the API server
  (8643, API key) wakes a parked MCP server in every profile within about a
  second. It returns `202`, or `404` when no profile configures the name. It
  never restarts a connected server. See
  [docs/extending.md](docs/extending.md#mcp-servers-your-app-hosts).
- **Hermes**: `mcp_servers.<name>.parked_retry_interval` (seconds, floor 10)
  sets how often a parked server is tried again. Unset keeps Hermes' 300 s.
  An app that hosts its own MCP server should set it, and
  `keepalive_interval`, to about 15.
- **Hermes**: every profile shares one uv cache on the new `hermes_uv_cache`
  volume (`UV_CACHE_DIR=/opt/uv-cache`), and a start-up script warms it with
  browser-use in the background. `browser_exec` ran the browser-use CLI with
  `uvx` from a cache under each profile's home on `data/hermes`, which took
  about a minute per call on Docker Desktop. It now takes about a second.
- **model-use**: writes every Hermes profile in one container run instead
  of 15 `hermes config set` calls, so a switch takes a couple of seconds.
  It now updates every profile under `data/hermes/profiles/`, not only the
  default, `browser` and `api-server`, so profiles from `make agent-create`
  follow the stack model. A profile whose `model.base_url` is not
  `llm-proxy` keeps its model.
- **model-sync**: `make model-sync` (`scripts/model-use.sh --sync`) copies
  `LLM_MODEL` from `.env` to Hermes and OpenCode, for when `.env` was edited
  by hand.
- **Database**: grants enforce the boundaries. `agent_ro` reads every schema.
  `agent_rw` also creates and writes in schema `agent`. Each integration's
  `svc_<slug>` role (`make db-service-add NAME=<slug>`) owns and migrates only
  its own schema. `public.data_catalog` lists every schema, table, and column
  with its `COMMENT ON` text.
- **Hermes**: new `database` skill. `mcp_servers.sql` exposes five tools
  (`list_schemas`, `list_objects`, `get_object_details`, `execute_sql`,
  `explain_query`). With `SQL_MCP_ENABLED=0` the bootstrap removes earlier
  `sql` registrations.
- **Make**: `db-backup` (`pg_dump -Fc`, mode 600), `db-service-add`,
  `db-psql`. `make doctor` gains a shared-database section, including a probe
  that `agent_ro` cannot write.
- **Compose**: an `x-db-profiles` anchor lists the profiles that start the
  database. An integration appends its own profile there.

## v0.3.1 — 2026-10-07

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`. No
   `.env` changes.
2. `docker compose up -d`. The `hermes` service gains three read-only
   `/bootstrap` mounts, so compose recreates it and cont-init applies the new
   overlays.

### Changes

- **Hermes**: a reply that is only reasoning and contains a malformed tool
  call (often just `</parameter></function></tool_call>`) is re-prompted
  instead of shown as the answer. Hermes v2026.9 started showing such
  reasoning as the final reply, and v2026.8 re-prompted. A model that keeps
  doing it is re-prompted at most twice per turn, then Hermes behaves as
  before. Reasoning-only replies without tool-call tags are unchanged. See
  `compose/hermes/patch-reasoning-only-tool-markup.py`.
- **Hermes**: a `browser_exec` result gains a `hint` field when the code
  imported the browser helpers from a module that does not exist, or ran and
  printed nothing. Results that print output are unchanged.
- **Hermes**: the `browser-login` skill (1.2.0) covers the browser-use
  backend, where `browser_exec` and the vault tools are the only browser
  tools, with working sign-in code. It lists the form's inputs before typing,
  and says what to do when a login was saved from a page without the form.
- **Hermes**: when `browser_vault_save_login` stores a login but the page has
  no form to fill, its `next` now says to open the sign-in form and call
  `browser_vault_fill` with the returned handle.
- **Hermes**: a reply that asks the user for a password in chat is re-prompted
  toward the vault tools instead of shown (at most twice per turn, shared
  with the fabricated-result check).
- **Gateway contract**: `session.create` and `session.resume` declare
  `history_budget`, and `message.complete` / `session.context_breakdown`
  declare `history_budget`, `history_tokens`, `history_first_row_id` and
  `history_summarised`. Hermes v2026.9 refused `history_budget` with `4000`
  and logged a contract warning for the reply fields.

### Behaviour changes

- **`history_budget` takes effect.** On v2026.9.14 the gateway refused it, so
  a client that retries without it (Boundary does) ran every session with no
  budget. Sessions that ask for a budget now get a windowed prompt and the
  `history_*` horizon fields.
- **`browser_exec` prints a trailing expression.** Code that ends in a bare
  expression (`js(...)`, `page_info()`) returns its value when it is not
  `None`, as a Python prompt does. Code ending in `print(...)` or a statement
  is unchanged.

## v0.3.0 — 2026-10-06

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`.
2. `docker compose up -d --build llm-proxy`. The proxy code is baked into the
   locally built `${IMAGE_PREFIX}-llm-proxy:local` image, so a plain `up -d`
   keeps the old proxy.
3. `docker compose up -d`. The Hermes bootstraps run again and set the new
   stale-stream timeout and auto-title settings on every profile.
4. Optional: delete `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `FAST_LLM`,
   `SMART_LLM`, `STRATEGIC_LLM` and `EMBEDDING` from `.env` (see below).

### New optional `.env` keys

| Key | Default | Purpose |
|---|---|---|
| `LLM_STREAM_IDLE_TIMEOUT` | `LLM_TIMEOUT` | Longest silent gap in a streamed reply before llm-proxy drops it |
| `HERMES_STREAM_RETRIES` | `1` | Hermes reconnects per failed stream (was Hermes' default, 2) |
| `HERMES_PROXY_STALE_TIMEOUT` | 300 local / 180 cloud | `agent.local_stream_stale_timeout` on every bootstrapped profile (was 900) |
| `HERMES_BROWSER_AUTO_TITLE` | `auto` | WebUI auto-titling: off for a local upstream, on for a cloud one |
| `LLM_PROXY_AWAIT_PUSH` | empty | llm-proxy routes that answer 503 until an admin push names them |

### Removed `.env` keys

`setup.sh --ollama` no longer writes `OPENAI_BASE_URL`, `OPENAI_API_KEY`,
`FAST_LLM`, `SMART_LLM`, `STRATEGIC_LLM` or `EMBEDDING`, and `.env.example`
no longer lists them. Compose sets them for `gpt-researcher` from `LLM_MODEL`
and `EMBEDDING_MODEL` and never read them from `.env`. You can delete them
from an existing `.env`.

### Behaviour changes

These change defaults. Set the keys above to get the old behaviour back.

- Hermes reconnects a failed stream once instead of twice.
- Bootstrap sets `agent.local_stream_stale_timeout` on every Hermes profile
  that it points at llm-proxy (default, `api-server`, `browser`, and any
  custom profile), sized from `LLM_BINDING_HOST`. Upstreams pushed later through the admin API are not
  seen; re-run the bootstraps after changing it.
- With a local upstream, the `browser` profile stops auto-titling sessions.
  The title call used the same model and ran ahead of the reply.

### Changes

- **llm-proxy** admin responses (`GET`/`POST /admin/upstreams`) gain a
  per-route `pushed` flag and a top-level `proxy` object with `boot_id` and
  `started_at`. `boot_id` changes on every start, so an admin client can tell
  the proxy restarted. Existing fields are unchanged.
- **llm-proxy** can hold routes until an admin client pushes them
  (`LLM_PROXY_AWAIT_PUSH`). A held route answers `503` with the reason
  instead of reaching the upstream without a key. Off by default.
- **docs/extending.md** documents starting the proxy with no key, the admin
  API as a stable interface, and the Hermes settings a downstream app should
  keep if it writes profile config itself.
- **llm-proxy** forwards each upstream chunk as it arrives. It used to hold
  small SSE events until 8 KB had collected, so a slow model looked stalled.
- **llm-proxy** drops the client connection when the upstream fails after the
  reply has started. It used to write a `502` status line into the middle of
  the chunked body. Failures before the reply starts are still a plain `502`.
- **llm-proxy** closes the upstream request when the client disconnects, even
  during prefill when nothing is being written. A dropped request no longer
  keeps a model server that runs one request at a time busy.

## v0.2.0 — 2026-10-06

### Upgrade steps

1. Re-run `./scripts/setup.sh` over the existing `.env` and `data/`.
2. If you overrode `lightrag` to use the published image, you can drop that
   override. If your `.env` sets `LIGHTRAG_BASE_IMAGE`, it still works. The
   new name is `LIGHTRAG_IMAGE`.
3. `docker compose up -d`. `lightrag` switches from the locally built
   `assistant-lightrag:local` to `ghcr.io/hkuds/lightrag:v1.4.9.3`, which is
   pulled on first start. You can then remove `assistant-lightrag:local`.

### Host requirements

- **python3 3.9 or newer** is now checked by `setup.sh` (it was already used,
  without a check). On macOS without the Command Line Tools, `/usr/bin/python3`
  is a stub. Run `xcode-select --install` or install Python first.
- **ssh-keygen** is now checked by `setup.sh` (used since v0.1.0 for the worker key).

### New optional `.env` keys

All default to the previous behaviour.

| Key | Default | Purpose |
|---|---|---|
| `CONTAINER_PREFIX` | empty | Prefix for every `container_name` |
| `IMAGE_PREFIX` | `assistant` | Built images are `${IMAGE_PREFIX}-<service>:local` |
| `NEO4J_HTTP_HOST_PORT` | `7474` | Host port for the Neo4j browser |
| `NEO4J_BOLT_HOST_PORT` | `7687` | Host port for Neo4j Bolt |
| `N8N_HOST_PORT` | `5678` | Host port for n8n (also used in `WEBHOOK_URL`) |
| `LIGHTRAG_IMAGE` | `ghcr.io/hkuds/lightrag:v1.4.9.3` | Replaces `LIGHTRAG_BASE_IMAGE` |
| `LLM_BINDING_API_KEY_FILE` | empty | Read the chat key from a file |
| `LIGHTRAG_LLM_BINDING_API_KEY_FILE` | empty | Read the LightRAG extraction key from a file |
| `EMBEDDING_BINDING_API_KEY_FILE` | empty | Read the embedding key from a file |

### Changes

- **llm-proxy** reads keys from `*_API_KEY_FILE` when set, and re-reads them on
  `SIGHUP` or `POST /admin/reload`. You can rotate a key without recreating the
  container, and it stays out of `docker inspect`.
- **LightRAG** runs the published image. The local patch build no longer
  built against v1.4.9.3.
- **Signal and Buzz profile syncs** edit `config.yaml` through the Hermes image
  instead of `docker compose exec hermes`. They now work before the first start
  and under any compose project name. They no longer append a second
  `platforms:` key, and they merge the duplicate keys left by earlier runs.
- **`make doctor`** fails when a nested `/opt/projects/<slug>` bind on `hermes`
  has no host folder.
- **`reset-neo4j-password.sh`** finds Neo4j through compose instead of by
  container name.
- **Docs:** new `docs/extending.md` covers second copies, overlay mounts,
  stable container paths, worker mounts and key rotation.

## v0.1.0 — 2026-10-06 (commit 259fd1f)

The first tagged release. It covers the work since `edf2465`.

### New services

| Service | Profile | Notes |
|---|---|---|
| `llm-proxy` | `core` | Holds the real model keys. Every client calls it with the placeholder `local-llm`. Admin API on `127.0.0.1:${LLM_PROXY_ADMIN_PORT:-4001}`. |
| `hermes-worker` | `core` | SSH target for Hermes shell and file tools when `HERMES_TERMINAL_BACKEND=ssh` (the default). Defined in `compose/hermes/terminal-ssh.yml`. |
| `opencode-mcp` | `coding` | Lets Hermes delegate `coding_*` tasks to OpenCode. |
| `caldav-mcp` | `calendar` | CalDAV calendars over MCP, with multiple accounts. |
| `signal-cli` | `signal` | Linked-device Signal for Hermes. |

### Required `.env` keys

`setup.sh` fills in any of these that are missing. It generates the secrets and prompts for or defaults `OPENCODE_WORKSPACE_HOST`.

- `LLM_PROXY_ADMIN_TOKEN`
- `LIGHTRAG_API_KEY`
- `OPENCODE_SERVER_PASSWORD`, `OPENCODE_WORKSPACE_HOST`
- `HERMES_DASHBOARD_PASSWORD`, `HERMES_DASHBOARD_AUTH_SECRET` (already required before)

### Host requirements

- `ssh-keygen`, to create the worker key under `compose/hermes/worker/ssh/`.
- `python3` (3.9+), for the setup and sync scripts.

### Notable changes

- Hermes shell and file tools run on `hermes-worker` by default. Hermes mounts
  `/opt/projects` read-only. Project folders and coding or context roots
  belong on the worker.
- `ASSISTANT_DATA_ROOT` moves projects, voice, memory and LightRAG inputs
  outside the repo.
- `sources.yaml` project registry, plus `CODING_EXTRA_ROOTS` and `CONTEXT_EXTRA_ROOTS`.
- Independent agents replace Buzz employees.
- Browser-harness sockets moved off the `data/hermes` bind mount.
