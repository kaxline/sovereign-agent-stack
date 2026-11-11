# Bitwarden broker reads the instance store

**Goal:** the broker loads its accounts from the add-on instance store
(`<data root>/addons/bitwarden/instances/<slug>.json`) instead of
`compose/bitwarden/accounts/<slug>.env`, with the store's slug rule. Target 0.3.15.

**Source:** Keith's decision in `docs/private/addon-scope-review.md` ("Self-contained adapters",
build-order step 6): read the store directly as Actual's launcher does, drop the account files and
the vault.sh code that keeps them in step with the store (cf9c1d3), no migration, slug rule
`^[a-z][a-z0-9-]{0,30}\Z`.

## Rulings

Recorded here because the decision leaves them open. Each says what it costs if wrong.

1. **Where an account is defined.** Non-secret values come from `instances/<slug>.json`, as
   `addon.sh` writes them: `mode` (`cloud` | `selfhosted`), `region` (`us` | `eu`, cloud only,
   default `us`), `url` (selfhosted), `scope` (default `folder:Agent`), `unlock` (default
   `manual`). `client_id` moves from the `.env` file to `secrets/<slug>/client_id` in Bitwarden's
   data folder, beside `client_secret` and `master_password`; the manifest keeps it `transient`,
   so the store never holds it. Unknown keys in the JSON are ignored. Wrong: client_id moves
   again later; one file.
2. **Mount.** The store and `BITWARDEN_HOST_DATA_DIR` can differ (`ASSISTANT_DATA_ROOT` moves the
   store, not the Bitwarden data folder). The broker mounts
   `${ASSISTANT_DATA_ROOT:-./data}/addons/bitwarden/instances` read-only at `/store/instances`
   (`BITWARDEN_INSTANCES_DIR`). The host folder must exist before compose mounts it, or Docker
   creates it as root on Linux: check that `addon-dirs.sh` / `addon_store.prepare` creates it on
   `make up`, and create it in `vault.sh` before any broker start if not.
3. **The `local` account** (make vault-init) stays out of the store, as today, so Boundary never
   lists it. `bitwarden-setup local-init` writes `broker/local-account.json`
   (`{"scope": "collection:<id>"}`, mode 600) and `secrets/local/client_id`; the broker loads
   it as mode `local`, server `https://vaultwarden.vault.internal`, unlock `stored`. A store
   instance named `local` is refused (`local` is reserved), as connect already refuses it. Local
   mode is otherwise frozen. Wrong: local moves into the store later.
4. **Hook ordering.** `addon.sh connect` runs `vault.sh add --json` before it writes the
   instance file, and the hook restarts the broker to confirm the sign-in. So `vault.sh add`
   (both modes) writes `instances/<slug>.json` itself, through `scripts/lib/addon_store.py`
   (`apply_instance` with the manifest, the same values and validation `addon.sh` uses), before
   it restarts the broker, and removes it again on any failure it rolls back. `addon.sh`'s later
   configure step then writes the same values. `vault.sh remove` (both modes) deletes the
   instance file before it restarts the broker; `addon_store.remove_instance` already tolerates a
   missing file. In `--json` mode vault.sh still never calls `addon.sh` (it holds the add-on
   lock). Terminal add and remove stop calling `addon.sh configure`. Wrong: if `addon.sh` later
   writes the file before the hook, the vault.sh write becomes redundant, not harmful.
5. **Removed with no migration:** the account-file parser in `broker/accounts.py` (replaced by an
   instance loader), `IGNORED_KEYS` and doctor's `BW_SECRETS_ITEM_SCOPE` warning,
   `store_values`/`store_record`/`store_forget`/`store_backfill` and the up-hook's account-file
   loop, `VAULT_ACCOUNTS_DIR`, `BITWARDEN_ACCOUNTS_DIR`, the compose mount,
   `compose/bitwarden/accounts/` (and its README, `.gitignore` lines and `.gitkeep` if any).
   `.env` account files left on disk are ignored; `make doctor` warns once if the folder still has
   `*.env` files and says to connect those accounts again. A store instance from before this
   change has no `secrets/<slug>/client_id`: the broker lists it as refused with
   `missing_client_id` until it is connected again.
6. **Refused instances.** Whatever made an account file refused (`bad_file` in `status --json`)
   now applies to an instance file: bad JSON, bad values, a slug outside the rule, a missing
   `client_id`. `status --json` keeps its line shape; only the cause changes. Document the error
   names Boundary can see.
7. **Slug rule** is the store's in the broker, setup and vault.sh, one regex each, all
   `^[a-z][a-z0-9-]{0,30}$` (Python `\Z`).

## Files

Broker: `broker/accounts.py` (instance loader), `broker/server.py` (config, refused list),
`setup/setup.py` (reads instances for `--slug`; `local-init` writes rule 3's files),
`docker-compose.yml` (mount, env, comments). Host: `scripts/vault.sh` (add, remove, status,
up-hook, store code, account dir), `scripts/doctor.sh` (Bitwarden block), `scripts/addon-dirs.sh`
if rule 2 needs it. Docs: `docs/bitwarden.md`, `docs/contracts/credentials.md` (drop the
"Account files" bullet, fix anything else this makes wrong), `.env.example`, `CHANGELOG.md`
(Unreleased: Changes entry, Upgrade step: account files are no longer read, connect each account
again with `make vault-add` or from Boundary; `make vault-init` users run it again to write the
local account). Tests: `test-bitwarden-broker.py`, `test-bitwarden-access.py`,
`test-bitwarden-setup.sh`, `test-bitwarden-connect.sh`, `test-vault-sh.sh`,
`test-bitwarden-compose-config.py`, `test-vw-register.py`, `test-bitwarden-fill.py` and any other
that writes account files.

## Tests that pin the change

- The broker loads cloud (us, eu), selfhosted and the reserved local account from the new
  sources, with defaults applied; ignores unknown keys.
- Refused: bad JSON, a slug outside the rule (`9abc`, 32 chars, `Upper`), an instance named
  `local`, a selfhosted instance without `url`, a missing `client_id`; each listed with its cause,
  and the other accounts still load.
- `vault.sh add --json` writes the instance file before the broker restart and removes it on a
  failed sign-in; `remove --json` deletes it before the restart; neither calls `addon.sh`.
- Terminal add and remove write and delete the instance file without `addon.sh configure`.
- A `compose/bitwarden/accounts/x.env` left on disk is not loaded.
- Compose renders the instances mount read-only and no `/accounts` mount.
- `local-init` writes `broker/local-account.json` and `secrets/local/client_id`, and no `.env`.

## Rulings made during the build

Each says what it costs if wrong.

1. **Linux can't read the store (rule 2).** `scripts/lib/addon_store.py` keeps `instances/` at
   mode 700, owned by the host user, and sets that mode again on every write, so uid 10001 (the
   broker and `bitwarden-setup`) cannot read it on Linux. Not solved here, since it needs a change
   to `addon_store.py`'s modes or the containers' uid; docs/bitwarden.md says so. `addon-dirs.sh`
   (run by `make ensure-local`, so `make up`) creates the folder; `vault.sh` also creates it in
   `pre-up`, `add`, `init` and `restore`. Wrong: Linux installs load only the local account until
   it is solved.
2. **`bitwarden-setup` mounts.** It mounts `instances/` read-write at `/store/instances` and
   `broker/` read-write at `/broker` (rule 3 needs the second). After `create` makes a new
   collection, setup rewrites `scope` in the instance file itself (JSON, indent 2, mode 644, the
   store's temp-file pattern), as it rewrote `BW_SCOPE` before. Wrong: the store has a second
   writer that bypasses the manifest's validation; a change to the store's file format must
   follow here.
3. **Deletes use `rm`.** `vault.sh remove` and a rolled-back add delete `instances/<slug>.json`
   with `rm -f`, not `remove_instance`: the add-on stores no per-instance secret, so the effect is
   the same, and the rollback runs in an EXIT trap where a container start is slow. Writes go
   through `apply_instance` (with `replace=True`, so a `--force` drops values the new account
   leaves out), run by `stack_python` as `vault.sh` already runs Python. Wrong: a non-transient
   instance secret added later would be left behind.
4. **Disconnect retry.** With the instance file deleted before the restart (rule 4), a disconnect
   whose broker restart fails ends in `command_failed` with the store entry already gone; a retry
   is `not_found`, and the broker drops the account at its next restart. Wrong: Boundary cannot
   retry to make a running broker drop the account; `make up` does.
5. **Refusal causes (rule 6).** `bad_file` (not a JSON object, unreadable, a wrong value, a
   `client_id` with whitespace), `bad_slug` (outside the rule, or `local.json`), and
   `missing_client_id` (`secrets/<slug>/client_id` missing or empty). `status --json` keeps its
   refused line, `{"file","slug","state":"refused","error"}`; `slug` is null for `bad_slug`, and
   the local account's file is `broker/local-account.json`. `status --json` also finds `bad_slug`
   and an unreadable file on the host, so they show with the broker down. The broker's
   `/health` keeps `account_errors` and adds `refused` (`file`, `error`, `message`);
   `broker.cli accounts` prints the cause as a third column of a `refused` line.
6. **`status --json` still lists `local`** (mode `local`, from `broker/local-account.json`), as it
   listed `local.env`. "Boundary never lists it" is read as the store's listing. Wrong: drop the
   local line from `status --json`.
7. **Re-running `make vault-init`.** With `STEP=role` and the local account files missing, it runs
   `local-init`, which writes them from `local/state` without signing in. `local-init` also
   writes them whenever they are missing, at any step. Without this, the CHANGELOG's "run it
   again" would do nothing on a finished install.
8. **`vault.sh` and `ASSISTANT_DATA_ROOT`.** The store follows `ASSISTANT_DATA_ROOT` from the
   environment, then `.env`, as compose and `addon.sh` do. The test hooks `VAULT_ACCOUNTS_DIR`,
   `VAULT_ADDON_SH` and `VAULT_ADDON_STORE` are gone; tests set `ASSISTANT_DATA_ROOT`.
9. **Reading JSON in bash.** `vault.sh status --json`, `need_manual` and `make doctor` read
   `mode`, `scope` and `unlock` with sed from files laid out as `addon_store` writes them (one key
   per line), and pass the string contents through as JSON. A hand-written one-line file shows
   empty values in `status --json`; the broker still loads it. An instance without `unlock` is
   `manual` everywhere (rule 1).
10. **`--force` backup** is `instances/.<slug>.json.bak`, a dot name that neither the broker nor
    the store lists.
11. **Backup and restore** copy `instances/` and `broker/local-account.json` in place of the
    account files. Restoring an older backup that has `accounts/` says those files are not read.
