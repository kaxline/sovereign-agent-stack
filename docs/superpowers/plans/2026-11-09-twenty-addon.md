# Twenty add-on manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `addons/twenty/addon.json` in stack 0.3.13, so that Boundary can install Twenty and connect its workspaces through `scripts/addon.sh` the same way it will for Actual. Workspace files become generic add-on instances, and existing installs keep every workspace and key.

**Architecture:** Each workspace is an add-on instance under `data/addons/twenty/` (`instances/<slug>.json`, `secrets/<slug>.key_ro`, `secrets/<slug>.key_rw`). A new generic `mcp.instances` block in the manifest replaces `register_twenty_mcp_servers`: `addon_manifest.py instances-plan` lists one entry per instance and profile, and the bootstrap writes `mcp_servers.crm-<slug>` with a bearer header. `crm.sh` gets `connect`, `disconnect` and `migrate`, writes the store instead of `compose/twenty/workspaces/`, and keeps its existing CLI as wrappers. `addon.sh` is plan 3's, and this plan doesn't edit it.

**Tech Stack:** bash 3.2, POSIX sh (bootstrap), Python 3.11 standard library through `stack_python` (R11: no host `python3` or `make` needed), Twenty v2.55.0.

**Spec:** `docs/private/addon-manifest-spec.md` in the main checkout (gitignored): "Lifecycle fields for `addon.sh`", "`mcp.instances`: registering one entry per instance" and the Twenty item under "First ports". Boundary's request is CR 0020, section "`addon.sh` as merged" (read-only). Item 8 of `docs/private/cr-0020-addon-sh-stack-reply-draft.md` promises this in 0.3.13.

**Depends on:** plan 3 (`2026-11-09-addon-lifecycle.md`, branch `feat/addon-lifecycle`). This branch is cut from it at f234285. Merge order: `feat/addon-lifecycle`, then this branch. Tasks 1 to 4 need only plan 3's Task 1 (manifest fields, already on the base). Task 6 needs plan 3's Tasks 4 and 5 (`init`, `connect`, `disconnect`) merged in for the end-to-end tests and the live check.

## Global constraints

- Hook contract (plan 3, Global Constraints and Task 5): hooks get `--slug <slug> --json` (`connect`, `disconnect`) or `--json` (`init`), and secrets arrive only as one JSON object on stdin. In `--json` mode stdout carries only JSON lines `{"phase":"setup","status":"running"|"done","message":...}` and ends with exactly one `{"phase":"done",...}` or `{"phase":"error","error":<name>,"message":...}`. Without `--json`, output stays as today's `[crm] ...` lines.
- Error names: the shared list (`bad_slug`, `exists`, `not_found`, `login_refused`, `token_expired`, `no_access`, `timeout`, `server_unreachable`) plus Twenty's own, listed in `instances.errors`: `local_mismatch`, `local_off`, `missing_key`, `bad_value`.
- No key in argv, env of a child process other than the existing curl header file, a log line or `logTail`. `mcp_status` keeps passing the key through a 0600 header file.
- Fail closed: an instance whose `access` isn't a known option, or that lacks the key its access needs, gets no MCP entry, and the bootstrap logs why without the value.
- Back-compat (`boundary-downstream-consumer` memory): Boundary calls `crm.sh init`, `add`, `remove`, `access` and `login-link` today and keeps doing so until it moves to `addon.sh`. Their arguments, exit codes and stdout stay the same.
- bash 3.2: no associative arrays, no `${var,,}`, regexes for slugs (as `valid_slug` does).

## Decisions for Keith

Keith approved the default (first option) of each on 2026-11-09.

1. **`ask` access.** Twenty's MCP exposes generic `execute_tool`, so there is no write-tool list for the trust gate, and CR 0023 R3 hasn't landed. Registering `ask` with the `rw` key, as the spec's rule says, would let writes through without asking. The plan ships `access` options `["read", "write"]` and adds `ask` with R3. Alternative: ship `ask` as an option that registers no entry (fails closed) until R3.
2. **Default access for a new connect.** The spec example says `read`; `crm.sh add` defaults to `write` today. The plan uses `read` for `instances.settings.access.default`, and `crm.sh add` keeps sending `write` when `TWENTY_ACCESS` is unset, so its behaviour doesn't change. Migrated workspaces get their file's value written explicitly.
3. **Profile `.env` key name.** The spec says `<NAME>_KEY`, which turns `TWENTY_SALES_EU_KEY` into `CRM_SALES_EU_KEY`. The plan follows the spec, and the reconcile step drops the old `TWENTY_<SLUG>_KEY` lines once. Alternative: a manifest field `auth.envPrefix: "TWENTY"` to keep the old names.
4. **`backup.recommended`.** Plan 3's validator takes env names only, so "the local workspace file" can't be listed. `data/addons/twenty/` is backed up as a unit, which covers it. The plan lists `["TWENTY_PASSWORD", "TWENTY_PASSWORD_FILE"]`.
5. **Bitwarden secrets sync.** `compose/bitwarden-broker/secrets_sync.py` writes `twenty/<slug>/key-ro|key-rw` into `compose/twenty/workspaces/<slug>.env` and never creates files, so after migration it would skip those items silently. The plan retargets them to `data/addons/twenty/secrets/<slug>.key_ro|key_rw` (whole-file values, like the encryption key), with a compose mount change for `bitwarden-secrets-sync` (Task 5). Alternative: leave it and document the gap.

## Agreed with plan 3's session (2026-11-09)

- `crm.sh init` writes the local workspace with `addon_cli.py configure twenty --instance <slug>` through `stack_python`, passing `--addons-dir` and `--data-root` with the data root mounted, as `addon.sh`'s `cli()` does. `addon_cli.py` takes no lock; calling `scripts/addon.sh` from inside a hook would fail with `busy`. Its stdin is one JSON object of `instances.settings` names to values, secrets included; unknown keys are `bad_input` (exit 2). That shape is fixed for 0.3.13.
- The caller sends `local`. `crm.sh connect` refuses a mismatch with `local_mismatch`, listed in `instances.errors`, and `addon.sh` passes it through with exit 4.
- `enable` runs flags, pull, start, `init` (once), reload, and nothing on every enable. `migrate` runs at the start of `crm.sh init` and every other `crm.sh` command, and in `crm.sh ensure` (`make ensure-local`, `make up`). On an existing install the first `addon.sh enable twenty` has no `init` record, so `init` runs and migrates. Residual gap: a stack upgraded and started with plain `docker compose up`, with no `crm.sh` call, registers no `crm-*` entries until one runs; `make doctor` warns. Plan 3's owner offered to propose running `hooks.upgrade` on enable when `state.json`'s `init.version` differs from the manifest version; that is Keith's call and not assumed here.
- The hook gets `--slug <slug> --json` with the values on stdin, and its `done` line is `{"phase":"done","account":"<slug>","mcp":["crm-<slug>"]}`. `addon.sh` copies `account` and `mcp` into its own `done`, and relays earlier `phase` lines inside step `check`.
- Secret instance settings land at `secrets/<slug>.key_ro` and `secrets/<slug>.key_rw` (0600), written by `addon.sh` at step `register` after the hook's check passes.

## Files

- Create: `addons/twenty/addon.json`
- Modify: `scripts/lib/addon_manifest.py` (validate `mcp.instances`, drop the refusal, `instances-plan` command, cron fence and parked-retry URLs for instances)
- Modify: `scripts/lib/addon_store.py` only if an instance read helper for `instances-plan` is missing (`stored`/`slugs` become public in plan 3 Task 8; reuse them)
- Modify: `compose/hermes/bootstrap-api-profile.sh` (generic `register_addon_instance_servers`, reconcile, pins; delete the Twenty functions and `TWENTY_WORKSPACES_DIR`)
- Modify: `docker-compose.yml` (hermes and the api-server/browser container: mount `${ASSISTANT_DATA_ROOT:-./data}/addons/twenty/{instances,secrets}` read-only under `/bootstrap/addon-data/twenty/`; drop the `twenty-workspaces` mount; Bitwarden sync target)
- Modify: `scripts/crm.sh` (`connect`, `disconnect`, `migrate`, `--json`; existing commands on the store)
- Modify: `scripts/twenty_admin.py` (read and write workspace values through a small store adapter instead of `<slug>.env`)
- Modify: `compose/bitwarden-broker/secrets_sync.py` (Decision 5)
- Modify: `scripts/doctor.sh` (Twenty checks read the store; warn on unmigrated `compose/twenty/workspaces/*.env`)
- Modify tests: `scripts/test-twenty-registration.sh`, `test-twenty-reconcile.sh`, `test-twenty-secrets.sh`, `test-crm-sh.sh`, `test-crm-script.py`, `test-addon-manifests.py`, `test-bitwarden-secrets-sync.sh`, `test-bootstrap-stamp.sh` (hash input changes)
- Create: `scripts/test-crm-migrate.sh`
- Modify docs: `docs/crm.md`, `addons/README.md`, `docs/extending.md` (`mcp.instances`), `CHANGELOG.md` (Unreleased), `compose/twenty/workspace.env.example` (marked legacy: read only by `migrate`)

## Tasks

### Task 1: Validate `mcp.instances`

- [ ] **Step 1: Failing tests** in `scripts/test-addon-manifests.py`: a manifest with the block below passes; each of these fails: `name` without `{slug}`; a `name` whose rendered prefix collides with a core server or another add-on's `mcp.name`; `url` naming a setting the instance schema lacks; `auth.secret.ro` naming a setting that isn't a `secret` instance setting; `access` not `{setting:<choice setting>}`; `gate.setting` not a `bool` instance setting; `gate.flag` not `builtin.flag` or a listed gate; unknown keys; `mcp.instances` without a top-level `instances` block; `mcp.instances` alongside `mcp.service`.

```json
"mcp": {
  "instances": {
    "name": "crm-{slug}",
    "url": "{setting:url}/mcp",
    "access": "{setting:access}",
    "auth": {"header": "Authorization", "scheme": "Bearer", "secret": {"ro": "key_ro", "rw": "key_rw"}},
    "gate": {"setting": "local", "flag": "TWENTY_LOCAL_ENABLED"}
  }
}
```

- [ ] **Step 2:** Run `python3 scripts/test-addon-manifests.py`; the new cases fail on the "not implemented yet" refusal.
- [ ] **Step 3: Implement** the checks in `addon_manifest.validate` and remove the refusal. `access` levels map `read` to `ro` and `write` to `rw`; any other option is refused at validation in this patch (Decision 1).
- [ ] **Step 4:** Tests pass, real `addons/` still pass. Commit: "Validate mcp.instances in add-on manifests".

### Task 2: `addons/twenty/addon.json`

- [ ] **Step 1:** Add a case to `test-addon-manifests.py` that loads `addons/twenty/addon.json`, checks it is canonical JSON, and checks `data.paths[0].default` matches the `twenty-db` and `twenty-server` bind mounts.
- [ ] **Step 2: Write the manifest.** Fields:
  - `builtin`: `{"flag": "TWENTY_MCP_ENABLED", "profile": "crm", "seed": false}`; `gates`: `[{"flag": "TWENTY_LOCAL_ENABLED", "profile": "crm", "seed": false}]`. Check `sync-profiles.sh` reads these the way it treats the flags today (the `crm` compose profile follows `TWENTY_LOCAL_ENABLED`, not `TWENTY_MCP_ENABLED`); if `builtin.profile` would turn on the local instance for remote-only installs, use the profile the manifest rules allow for "no service of its own" and say so in the commit.
  - `services`: `server` (`twenty-server`, port 3000, `memoryLimit` 1152m), `worker` (`twenty-worker`, 1152m), `db` (`twenty-db`, 256m), `redis` (`twenty-redis`, 128m), each `"when": {"gate": "TWENTY_LOCAL_ENABLED"}`.
  - `instances`: `name` `workspace`; settings `url` (url), `access` (choice `["read","write"]`, default `read`, labels "Look things up"/"Make changes"), `local` (bool, default false), `workspace_url` (url, optional), `workspace_id` (string, optional), `key_ro` and `key_rw` (secret, optional); `connect`/`disconnect` `{"script": ["scripts/crm.sh", "connect"|"disconnect"]}`; `errors` `["local_mismatch", "local_off", "missing_key"]`.
  - `mcp`: `transport` `http`, `timeout` 60, `connectTimeout` 30, `env` `{"timeout": "TWENTY_MCP_TIMEOUT", "connectTimeout": "TWENTY_MCP_CONNECT_TIMEOUT"}`, `tools` and `pin` the seven names in `TWENTY_MCP_PINS`, `writeTools` `[]` (roles in Twenty are the boundary), `profiles` `{"default": "rw", "browser": "rw", "api-server": "ro"}`, and the `instances` block from Task 1.
  - `hooks.init`: `{"script": ["scripts/crm.sh", "init"]}`.
  - `data`: `{"paths": [{"env": "TWENTY_DATA_DIR", "default": "./data/twenty"}]}`.
  - `backup`: `{"secrets": ["TWENTY_ENCRYPTION_KEY", "TWENTY_DB_PASSWORD"], "recommended": ["TWENTY_PASSWORD", "TWENTY_PASSWORD_FILE"]}`.
  - `permissions`: `reads: []`, `writes: ["external:crm"]`, `sends: false`, `egress: ["internet"]`, `exposesToAgent: false`, `cron: "ro"` (api-server gets only the read-only key today, and cron runs there).
  - `minStackVersion` `0.3.13`.
- [ ] **Step 3:** `python3 scripts/test-addon-manifests.py` and `python3 scripts/test-addon-store.py` pass. Commit: "Add Twenty's add-on manifest".

### Task 3: `crm.sh` on the instance store, with `connect`, `disconnect` and `migrate`

The store is `$(data_root)/addons/twenty/`. Every write goes through `stack_python scripts/lib/addon_cli.py configure twenty --instance <slug>` or `remove-instance`, never hand-written JSON, so the file layout stays `addon_store`'s.

- [ ] **Step 1: Failing tests.** New `scripts/test-crm-migrate.sh` (synthetic workspaces in a scratch root, `STACK_PYTHON=host`):
  - Three files: local `main.env` (with `TWENTY_WORKSPACE_ID`, both keys, no `TWENTY_ACCESS`), remote `sales-eu.env` with `TWENTY_ACCESS=read` and only `TWENTY_KEY_RO`, and `bad_slug!.env`. After `crm.sh migrate`: `instances/main.json` has `url` `http://twenty-server:3000`, `local: true`, `access: "write"`, `workspace_id`; `secrets/main.key_ro` and `main.key_rw` hold the keys (mode 0600); `sales-eu` has `local: false`, `access: "read"`, no `key_rw` file; both `.env` files are renamed `.env.migrated`; the bad file is left and named on stderr.
  - A second `migrate` changes nothing (same mtimes) and exits 0.
  - A slug present in both the store and a leftover `.env` keeps the store's values and leaves the `.env` file, with a warning (no silent overwrite).
  - A write failure halfway (read-only `secrets/`) leaves the `.env` file in place, so the next run retries.
  - No key value appears in stdout or stderr.
  - `test-crm-sh.sh` additions: `connect --slug x --json` with stdin `{"url":"http://twenty-server:3000","access":"read","key_ro":"k"}` and no `local` ends with `{"phase":"error","error":"local_mismatch",...}`; `local: true` with `TWENTY_LOCAL_ENABLED=0` is `local_off`; `access: write` without `key_rw` is `missing_key`; a curl stub returning 401 is `login_refused`, 000 is `server_unreachable`, 403 is `no_access`; a curl timeout (exit 28) is `timeout`; success prints `done` with `account` and `mcp`. Nothing is written in any case (the hook stores nothing; `addon.sh` registers).
  - `disconnect --slug x --json` prints `done`; for a local instance its `done` carries a `message` that the workspace still exists in Twenty.
  - `add`, `remove` and `access` keep their stdout and exit codes, and land in the store.
- [ ] **Step 2:** Run them; they fail.
- [ ] **Step 3: Implement.**
  - `migrate`: for each `compose/twenty/workspaces/<slug>.env` with a valid slug, parse it with `ws_get`, build the JSON (url without `/mcp`, `access` default `write`, `local` from the URL test, `workspace_url`, `workspace_id`, keys), pipe it to `addon_cli.py configure twenty --instance <slug>`, then rename the file. Run it at the start of `ensure`, `init`, `add`, `remove`, `access`, `login-link` and `open`. With no `.env` files it returns before starting a container.
  - `connect --slug S --json` (stdin JSON): parse with `stack_python -I` (one small inline script that prints only shell-safe fields: url, access, local and which keys are present, and writes the keys to 0600 temp files the trap removes). Check `local` against the URL and `TWENTY_LOCAL_ENABLED`, check the key access needs, then `mcp_status` for each key. Map the status to error names as listed in Step 1. Print `done`.
  - `disconnect --slug S --json`: no checks (`addon.sh` removes the files); print `done`, with the local note.
  - `add`: build the same JSON and call `addon.sh connect twenty --instance S` with it on stdin (with `--force`: `addon.sh disconnect` first). Keep the `[crm]` log lines by translating its JSON lines. `remove`: `addon.sh disconnect`. `access`: check the key, then `addon_cli.py configure twenty --instance S` with `{"access": ...}`, then print the "make hermes-reload" line as now.
  - `init --json`: with `TWENTY_LOCAL_ENABLED` off, print `done` with `"message":"local instance off"` and exit 0. Otherwise as now, with `twenty_admin.py` reading and writing through the store (next bullet), and `phase` lines for owner, server admin and keys.
  - `twenty_admin.py`: replace `write_workspace_file`/`local_workspaces`/the `<slug>.env` reads with a small adapter over `addon_store` (imported from the mounted `scripts/lib`). `crm_admin` mounts the store dir instead of `WS_DIR`.
  - `local_slugs`, `default_slug`, `is_local_file`, `update_workspace_urls` read `instances/*.json` via one `addon_cli.py list --json` call.
- [ ] **Step 4:** Tests pass; `bash scripts/test-bash32.sh` passes. Commit: "Store Twenty workspaces as add-on instances; add crm.sh connect, disconnect and migrate".

### Task 4: Register `mcp.instances` in the bootstrap

- [ ] **Step 1: Failing tests.** Rewrite `test-twenty-registration.sh` and `test-twenty-reconcile.sh` against the generic functions, with a synthetic store under a temp `ADDON_DATA_DIR` and the real `addons/twenty/addon.json`. Keep every current case: api-server gets `key_ro` or no entry; default and browser follow `access`; a local instance is skipped while `TWENTY_LOCAL_ENABLED=0`, with no "no workspace" warning; an unknown access gets no entry; a removed instance loses its entry, `CRM_<SLUG>_KEY` and pins; the bare pins go when no `crm-*` entry is left; entries a client added under another name stay. New cases: old `TWENTY_<SLUG>_KEY` lines are removed; `instances-plan` output never contains a key value; `TWENTY_MCP_ENABLED=0` drops every `crm-*` entry. `test-twenty-secrets.sh` checks no key reaches logs on the new path.
- [ ] **Step 2:** Run; they fail.
- [ ] **Step 3: Implement.**
  - `addon_manifest.py instances-plan <addons-dir> <addon-data-dir> <profile>`: one line per instance of each add-on with `mcp.instances`: `name|title|on|url|keyenv|keyfile|timeout|connect|pins|reason`. `on` is 0 with a `reason` when the flag is off, the gate is off for a gated instance, the profile isn't in `mcp.profiles`, access is unknown, or the key file is missing or empty. `keyfile` is a path inside the mounted secrets dir; the value never goes through the plan.
  - Bootstrap `register_addon_instance_servers <profile>`: for each `on` line, `upsert_env` the key read from `keyfile` into the profile `.env` under `keyenv`, write the entry with the existing header writer (renamed `write_bearer_mcp_entry`), and pin. Then a generic reconcile that drops entries matching the name template's prefix (`crm-`) not in the plan, their `<NAME>_KEY`, their pins, the bare pins when none is left, and the legacy `TWENTY_<SLUG>_KEY`.
  - Feed instance names and pins into `reconcile_stack_eager_tools` and instance URLs into the parked-retry list in place of `twenty_workspace_slugs` and `TWENTY_LOCAL_MCP_URL`. Extend `cron_fence` so a `cron: "ro"` add-on's instance entries are treated as its `mcp` names.
  - Replace the `twenty` section of the bootstrap input hash with the store's `instances/*.json` and secret file names plus mtimes (not contents).
  - Delete `register_twenty_mcp_servers`, `reconcile_twenty_workspaces`, `twenty_*` helpers and the `TWENTY_WORKSPACES_DIR`/`TWENTY_LOCAL_MCP_URL` variables.
  - `docker-compose.yml`: mount the store read-only on hermes and the second Hermes container; remove the `twenty-workspaces` mount.
- [ ] **Step 4:** All `test-twenty-*`, `test-bootstrap-stamp.sh` and `test-compose-env-allowlist.py` pass; `docker compose config` is valid with `core,crm`. Commit: "Register Twenty workspaces through mcp.instances".

### Task 5: Bitwarden sync, doctor and docs

- [ ] **Step 1:** In `test-bitwarden-secrets-sync.sh`, `twenty/main/key-ro` updates `secrets/main.key_ro` in a synthetic store, refuses to create a missing one, and exits 3 (Hermes reads it). Doctor test: a leftover `compose/twenty/workspaces/x.env` gives a warning naming `crm.sh migrate`.
- [ ] **Step 2: Implement** the sync retarget and compose mount (Decision 5), and doctor's checks.
- [ ] **Step 3: Docs.** `docs/crm.md`: workspaces live in `data/addons/twenty/`, `addon.sh connect/disconnect`, migration on upgrade; `docs/extending.md` and `addons/README.md`: `mcp.instances`; `CHANGELOG.md` Unreleased: Twenty manifest, migration, `CRM_<SLUG>_KEY` rename, Bitwarden target change, and that Boundary can move Twenty onto `addon.sh`.
- [ ] **Step 4:** Commit: "Point Bitwarden's Twenty keys and doctor at the instance store; document the Twenty add-on".

### Task 6: End-to-end and live check

Needs plan 3 Tasks 4 and 5 merged into this branch (rebase on `feat/addon-lifecycle` once they land).

- [ ] **Step 1:** In `test-addon-cli.sh`'s style, a docker-stubbed run of `addon.sh enable twenty --json` (init runs `crm.sh init`), `connect twenty --instance sales --json` (stubbed curl 200), `disconnect`, `disable`, `remove twenty --delete-data`: last-line phases, `instances/sales.json` present then gone, `mcp` in `done` is `["crm-sales"]`, `data/twenty` deleted only under the data root.
- [ ] **Step 2:** Full suite once through the test queue (`run_in_background`), as `CONTRIBUTING`/CI run it.
- [ ] **Step 3: Live check on a scratch stack** (a copy of the repo with its own `COMPOSE_PROJECT_NAME`, `CONTAINER_PREFIX`, `ASSISTANT_DATA_ROOT` and ports; never Keith's stack). Seed it with a synthetic `compose/twenty/workspaces/legacy.env` pointing at the local instance, to exercise migrate. Then: `addon.sh enable twenty --json` (migrate, init, Hermes registers `crm-main` and `crm-legacy`); `crm.sh mode 1`; create a second workspace with `crm.sh init --slug sales`, disconnect it, and reconnect it with `addon.sh connect twenty --instance sales` using its keys on stdin; check `mcp_servers.crm-sales` and an MCP `initialize` through Hermes; `addon.sh disconnect`; `addon.sh disable`; `addon.sh remove twenty --delete-data`; check `data/twenty` and `data/addons/twenty` are gone and nothing outside the scratch data root changed. Record timings and memory.
- [ ] **Step 4:** Report what the live check couldn't cover: a remote Twenty over HTTPS, Keith's real workspaces (Keith runs `./scripts/crm.sh migrate` himself, and reports counts), Boundary's UI, and Bitwarden-synced keys with a real vault.

## Not in this plan

- `ask` access (CR 0023 R3) and the R3 direct-mode spike.
- Boundary moving Twenty off `crm.sh`; that happens in Boundary after 0.3.13.
- Removing `compose/twenty/workspaces/` and the `.env.migrated` files; a later patch, once Keith confirms his install migrated.
