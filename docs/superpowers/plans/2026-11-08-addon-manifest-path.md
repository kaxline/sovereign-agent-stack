# Add-on manifest path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give built-in add-ons gated MCP and service entries, settings, per-instance settings, secret files and read-only commands, and give Boundary one script, `scripts/addon.sh`, to list, configure, enable, disable and run them.

**Architecture:** `scripts/lib/addon_manifest.py` (validator, `mcp-plan`, `cron_fence`) learns gates, MCP lists, `instances`, `commands` and `mounts`. A new `scripts/lib/addon_store.py` owns everything under `data/addons/<short-id>/`. A new `scripts/lib/addon_cli.py` implements the subcommands, and `scripts/addon.sh` runs it through `stack_python`, so the host needs no python3. `sync-profiles.sh` reads gate rows the same sed-only way it reads `builtin`.

**Tech Stack:** Python 3.11 standard library, POSIX sh / bash 3.2, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-11-08-email-addons-design.md` (section "Manifest path additions"), on top of `docs/private/addon-manifest-spec.md`.

## Global Constraints

- Standard library only in `addon_manifest.py`, `addon_store.py` and `addon_cli.py`. The bootstraps run `addon_manifest.py` with the Hermes image's python3.
- Every manifest in `addons/` stays canonical: `json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"`.
- `sync-profiles.sh` must keep passing `scripts/test-no-host-python.sh` (no python3 on the host).
- Add-on data directory: `${ASSISTANT_DATA_ROOT:-./data}/addons/<short-id>/`, holding `settings.env`, `instances/<slug>.json`, `secrets/` (0700, files 0600) and `config/`.
- Instance slug: `^[a-z][a-z0-9-]{0,30}$`.
- A secret value never appears in argv, `.env`, `settings.env`, an instance file, `list` output, a log line or an error message. Error messages name the setting, never the value.
- `addon.sh` exit codes: 0 success, 2 invalid input, 3 a required setting is missing, 4 a command failed in its container.
- Existing manifests (`gptr`, `actual`) and their registration tests behave exactly as before.
- Release as a 0.3.x patch with a CHANGELOG entry; the new fields are experimental.

## Review Focus

- A manifest whose `mcp` is still a single object (gptr, actual) must give byte-identical `mcp-plan` output to `main`.
- `configure` called twice for one instance with a partial payload must keep the settings and secrets it wasn't given, not wipe them.
- A secret value containing `|`, `=`, a newline or a quote must round-trip unchanged into its file and never break `settings.env` parsing.
- `disable` must leave the user's data in place, and `--purge` must refuse to delete anything outside `data/addons/<short-id>/` (a short id of `..` or an empty string).
- A gate flag set to `1` while the add-on's main flag is `0`: the gated entry follows its own flag (needed for `MAIL_DRAFTS_ENABLED` parity in the next plan).

---

### Task 1: Gates and MCP lists in the validator and `mcp-plan`

**Files:**
- Modify: `scripts/lib/addon_manifest.py`
- Test: `scripts/test-addon-manifests.py`

**Interfaces:**
- Produces:
  - Top-level `gates`: built-in only, a list of `{"flag": ENV, "profile": NAME, "seed": bool}`. Each gate is one more `sync-profiles.sh` row.
  - `mcp` is an object or a non-empty list of objects. Each entry may carry `"title": str` (defaults to the manifest `title`) and `"when": {"gate": ENV}`. `"when"` must name a flag listed in `gates`.
  - A `services` entry may carry the same `"when"`.
  - `mcp_entries(manifest: dict) -> list[dict]`: returns `[mcp]` for an object and the list as is.
  - `mcp_plan()` prints one line per entry, in list order, in today's 11-field format. `enabled` comes from the entry's gate flag when it has `when`, else from `builtin.flag`. `title` is the entry's title.
  - `cron_fence()` fences every entry.

- [ ] **Step 1: Write the failing tests** in `scripts/test-addon-manifests.py`, built on a `two_entry()` variant of BASE (gptr), with `gates: [{"flag": "GPTR_EXTRA_ENABLED", "profile": "research-extra", "seed": False}]` and a second entry named `gptr-extra` with `when: {"gate": "GPTR_EXTRA_ENABLED"}` and title `"Extra"`:
  - `refused("when names an unknown gate", …, "builtin", "when.gate")`
  - `refused("gates on a packaged add-on", packaged(two_entry()), "official", "gates")`
  - `refused("two entries share a name", …, "builtin", "mcp names must be unique")`
  - `refused("api-server on any entry of a reads add-on", …, "builtin", "api-server")`
  - `mcp-plan` on a temp addons dir holding `two_entry()`, with env `{"GPTR_MCP_ENABLED": "0", "GPTR_EXTRA_ENABLED": "1"}` for profile `default`: two lines; the first has field 3 `0`, the second has fields 1, 2, 3 = `gptr-extra`, `Extra`, `1`.
  - `mcp-plan` over the real `addons/` gives the same lines as before. Assert against a fixture captured from `main` before the change: `python3 scripts/lib/addon_manifest.py mcp-plan addons default > scripts/fixtures/addon-mcp-plan-default.txt`, plus the same for `api-server` and `browser`.
  - `cron_fence` over `two_entry()` with `permissions.cron` false: names include both `gptr` and `gptr-extra`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python3 scripts/test-addon-manifests.py`
Expected: the new checks print `FAIL`; the existing ones still print `ok`.

- [ ] **Step 3: Implement** gates, `mcp_entries`, per-entry validation (`_validate_mcp` runs per entry; a name-uniqueness check across entries; the `cron "ro"` rule applies per entry), and per-entry `mcp_plan`/`cron_fence`. Keep the module docstring's field list accurate.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python3 scripts/test-addon-manifests.py && sh scripts/test-gptr-mcp-registration.sh && sh scripts/test-actual-registration.sh && sh scripts/test-addon-cron-fence.sh`
Expected: all `ok`; the existing scripts exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_manifest.py scripts/test-addon-manifests.py scripts/fixtures/addon-mcp-plan-*.txt
git commit -m "Let an add-on declare gated MCP entries"
```

### Task 2: Gate rows in `sync-profiles.sh`

**Files:**
- Modify: `scripts/sync-profiles.sh` (`addon_rows`, lines 53-79)
- Modify: `scripts/test-addon-manifests.py` (the section that checks manifests against `sync-profiles.sh` and `docker-compose.yml`)
- Test: `scripts/test-no-host-python.sh`

**Interfaces:**
- Consumes: Task 1's `gates` layout. In canonical JSON it opens with `  "gates": [`, closes with `  ],` or `  ]`, and each gate object has `"flag"`, `"profile"`, `"seed"` lines in that order (json.dumps keeps insertion order; the validator requires that key order).
- Produces: `sync-profiles.sh --list` prints one row per gate, after the add-on's `builtin` row.

- [ ] **Step 1: Write the failing checks.** In `test-addon-manifests.py`, the validator refuses a gate whose keys aren't in the order `flag, profile, seed` (needle `"gates keys"`). The compose cross-check requires each gate profile to exist in `docker compose config --profiles`. In `test-no-host-python.sh`, add a fixture manifest with one gate to its temp `addons/` and assert `sync-profiles.sh --list` shows that gate's flag and profile.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-addon-manifests.py; sh scripts/test-no-host-python.sh`
Expected: FAIL on the gate checks.

- [ ] **Step 3: Implement** gate parsing in `addon_rows` with `sed -n '/^  "gates": \[$/,/^  \]/p'` and a `while read` loop that emits `flag profile seed` each time it reads a `"seed"` line. It `die`s with the same message style as the `builtin` case when a gate block is unreadable. No python3 and no awk extensions beyond POSIX.

- [ ] **Step 4: Run them and confirm they pass**

Run: `python3 scripts/test-addon-manifests.py && sh scripts/test-no-host-python.sh`
Expected: all `ok`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/sync-profiles.sh scripts/test-addon-manifests.py scripts/test-no-host-python.sh
git commit -m "Read add-on gate rows in sync-profiles"
```

### Task 3: Validator rules for settings, instances, commands and mounts

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`validate`)
- Test: `scripts/test-addon-manifests.py`

**Interfaces:**
- Produces, in `validate()`:
  - Settings: an entry may have `"label"`, `"default"`, `"optional": bool`, and for `bool` only `"flag": ENV`. `flag` must be `builtin.flag` or a gate flag. A setting with neither `default` nor `optional: true` is required.
  - Setting types gain `list` (list of strings) and `date` (`YYYY-MM-DD`). Defaults must match the type.
  - `instances`: `{"name": NAME, "settings": {…}}` with the same setting rules, except `flag` is refused inside an instance. An instance schema must not declare `slug`, which is reserved.
  - `secret` (top-level or in an instance) is refused unless the source is `builtin` or `official` (needle `"secret settings"`).
  - `services.<name>.mounts`: a map with keys from `{"secrets", "config", "instances"}` and values `"ro"` or `"rw"`. They name fixed container paths: `secrets` → `/run/addon/secrets`, `config` → `/run/addon/settings.env`, `instances` → `/run/addon/instances`. For built-ins the compose service does the mounting. `test-addon-manifests.py` checks that the compose service mounts exactly these paths, from `data/addons/<short-id>/`, with the declared mode. A service that mounts `secrets` may not be one an `mcp` entry points at (needle `"holds secrets"`).
  - `commands`: map of NAME to `{"service": key, "instance": bool, "argv": [str, …]}`. `service` must be in `services`. For an add-on with non-empty `reads`, a command may not use an MCP service (needle `"command runs in an MCP service"`).
  - Constant `RESERVED_SETTINGS = {"slug"}`.

- [ ] **Step 1: Write the failing tests:** one `refused(...)` per rule, each with the needle named above, plus a positive check that a manifest using every new field correctly gives `validate(...) == []` for `builtin`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-addon-manifests.py`
Expected: the new checks FAIL.

- [ ] **Step 3: Implement** the rules in `validate`. Put setting checks in `_validate_settings(settings: dict, *, source: str, reads: bool, flags: set[str], in_instance: bool) -> list[str]` and call it for top-level settings and for `instances.settings`.

- [ ] **Step 4: Run them and confirm they pass**

Run: `python3 scripts/test-addon-manifests.py`
Expected: all `ok`.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_manifest.py scripts/test-addon-manifests.py
git commit -m "Validate add-on settings, instances, commands and mounts"
```

### Task 4: `addon_store.py`, the add-on data directory

**Files:**
- Create: `scripts/lib/addon_store.py`
- Test: `scripts/test-addon-store.py`

**Interfaces:**
- Consumes: `addon_manifest.short_id`, `validate`.
- Produces:
  - `class InputError(ValueError)` (maps to exit 2) and `class Missing(RuntimeError)` (exit 3), each with a `.setting` attribute holding the setting's name only.
  - `addon_dir(data_root: Path, manifest: dict) -> Path`. Resolves to `data_root/"addons"/short_id`, and raises `InputError` if the result isn't strictly inside `data_root/"addons"`.
  - `apply_settings(manifest, data_root, values: dict) -> dict[str, str]`. Validates top-level values against the schema and merges them into `settings.env`, keeping keys not given. Writes `secret` values to `secrets/<name>` (0600). Returns `{ENV: "1"|"0"}` for `bool` settings that carry `flag`, for the caller to write to `.env`. Never writes a flag itself.
  - `apply_instance(manifest, data_root, slug: str, values: dict) -> None`. Same rules, writing `instances/<slug>.json` (non-secret values only, merged) and `secrets/<slug>.<name>` (0600).
  - `remove_instance(manifest, data_root, slug: str) -> None`. Deletes the instance file and every `secrets/<slug>.*`.
  - `missing(manifest, data_root) -> list[str]`. Required settings and instance settings without a value, as `"<name>"` or `"<slug>.<name>"`. At least one instance is required when `instances` is declared.
  - `listing(manifest, data_root, environ) -> dict`: `{"id", "shortId", "title", "tagline", "permissions", "platform", "enabled", "settings": {name: value-or-{"set": bool} for secrets}, "instances": {slug: {...same masking}}, "missing": [...]}`. `platform` is `host.platform` when present, else `"any"`. `enabled` is `truthy(environ[builtin.flag])`.
  - `settings.env` format: one `KEY=value` per line, where KEY is `ADDON_<SHORT>_<NAME>` upper-cased with `-` as `_`. Values are written with `shlex.quote`, and lists as JSON. Reads use `shlex.split` on each line.
  - Directories are created 0700. A file is written to a temp name in the same directory and then renamed.

- [ ] **Step 1: Write the failing tests** in `scripts/test-addon-store.py` (same `check()` style as `test-addon-manifests.py`), against a fixture manifest in a temp dir:
  - `apply_settings` with `{"interval": 600}`, then `{"drafts": True}`: `settings.env` holds both, and the second call returns `{"GPTR_EXTRA_ENABLED": "1"}`.
  - `apply_instance(..., "work", {"host": "imap.example.com", "password": "p|w=\n'x\""})`, then `apply_instance(..., "work", {"user": "a@example.com"})`: the instance file has host and user; `secrets/work.password` reads back the exact password; its mode is `0o600`; the password string is in no file except that one (walk the whole tree).
  - `listing(...)["instances"]["work"]["password"] == {"set": True}`, and the JSON dump of `listing` doesn't contain the password.
  - `apply_instance(..., "Bad Slug", {})` raises `InputError` with `.setting == "slug"`.
  - `apply_settings(..., {"interval": "x"})` raises `InputError`, and `str(exc)` doesn't contain `"x"`.
  - `missing()` lists `"since"` and then `"work.mailboxes"` until they're set.
  - `addon_dir` with a manifest whose id ends in `..` raises `InputError`.
  - `remove_instance(..., "work")` leaves no `work.*` under `secrets/`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-addon-store.py`
Expected: `ModuleNotFoundError: addon_store`.

- [ ] **Step 3: Implement** `scripts/lib/addon_store.py` with the interfaces above.

- [ ] **Step 4: Run them and confirm they pass**

Run: `python3 scripts/test-addon-store.py`
Expected: all `ok`.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_store.py scripts/test-addon-store.py
git commit -m "Store add-on settings, instances and secrets under data/addons"
```

### Task 5: `scripts/addon.sh` and `addon_cli.py`

**Files:**
- Create: `scripts/lib/addon_cli.py`, `scripts/addon.sh`
- Create: `scripts/test-addon-cli.sh`, `scripts/fixtures/addons-cli/fixture/addon.json`
- Modify: `Makefile` (`addon-list`, plus `addon-test` running the three add-on tests)

**Interfaces:**
- Consumes: Task 4's store functions, `addon_manifest.load_dir` and `validate`, `scripts/lib/env.sh` (`env_get`, `upsert_env`), and `scripts/lib/python.sh` (`stack_python`).
- Produces (Boundary's contract):
  - `addon.sh list --json` prints a JSON array of `listing()` objects, one per add-on, sorted by short id.
  - `addon.sh configure <short-id> [--instance <slug>] [--remove]` reads one JSON object on stdin. Without `--instance` it calls `apply_settings` and writes each returned flag with `upsert_env .env`. With `--instance` it calls `apply_instance`, and with `--remove` it calls `remove_instance` (stdin ignored).
  - `addon.sh run <short-id> <command> [--instance <slug>]` runs `docker compose run --rm --no-deps -T <compose service> <argv…>`. When `instance` is true it appends `--account <slug>`. It prints the container's stdout and exits 4 if the container fails.
  - `addon.sh enable <short-id>`:
    1. exit 3 with the `missing()` names if any are missing;
    2. `upsert_env .env <builtin.flag> 1`;
    3. `scripts/sync-profiles.sh`;
    4. `docker compose up -d --wait` on the services whose gate is on (services without `when` always);
    5. `make hermes-reload`.
  - `addon.sh disable <short-id> [--purge]`:
    1. set `builtin.flag` and every gate flag to 0;
    2. `scripts/sync-profiles.sh`;
    3. `docker compose stop` on the add-on's services;
    4. `make hermes-reload`.

    `--purge` also deletes `addon_dir` and every named volume listed in `services.<name>.volumes` (a new optional list of compose volume names, validated in this task: each must exist in `docker-compose.yml`).
  - `addon_cli.py` does the parsing and file work, prints JSON or a one-line error, and exits with the codes in Global Constraints. `addon.sh` handles `.env`, compose and make. It runs `addon_cli.py` with `stack_python --mount "$data_root"` so it works without host python3.

- [ ] **Step 1: Write the failing test** `scripts/test-addon-cli.sh`. It uses `STACK_PYTHON=host`, a temp `ROOT` with a copy of `.env.example` as `.env`, `ADDONS_DIR` pointing at `scripts/fixtures/addons-cli`, and stubs for `docker` and `make` on `PATH` that append their argv to a log file. It checks:
  - `printf '{"interval": 600}' | addon.sh configure fixture` exits 0.
  - `printf '{"host":"h","password":"s3cr3t-value"}' | addon.sh configure fixture --instance work` exits 0.
  - `addon.sh enable fixture` exits 3 and prints the missing setting names.
  - After the missing settings are configured, `enable` exits 0. `.env` has the flag at 1, and the docker log shows `up -d --wait` with the fixture's compose service.
  - `addon.sh list --json` parses with `python3 -m json.tool`.
  - `grep -r s3cr3t-value` over `.env`, the docker/make stub logs and the captured output of every command finds nothing.
  - `addon.sh disable fixture --purge` removes `data/addons/fixture`, and the stub log shows `volume rm` for the declared volume.
  - `echo '{' | addon.sh configure fixture` exits 2.

- [ ] **Step 2: Run it and confirm it fails**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL, `scripts/addon.sh: not found`.

- [ ] **Step 3: Implement** `addon_cli.py` (subcommands `list`, `configure`, `remove-instance`, `plan-enable`, `plan-disable`). The `plan-*` subcommands print the flags, services and volumes as JSON for `addon.sh` to act on. Then write `addon.sh`, with `set -euo pipefail` and bash 3.2-safe arrays, following `scripts/sync-profiles.sh` for structure.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `sh scripts/test-addon-cli.sh && python3 scripts/test-addon-manifests.py && python3 scripts/test-addon-store.py`
Expected: exit 0, all `ok`.

- [ ] **Step 5: Commit**

```bash
git add scripts/addon.sh scripts/lib/addon_cli.py scripts/test-addon-cli.sh scripts/fixtures/addons-cli Makefile scripts/lib/addon_manifest.py scripts/test-addon-manifests.py
git commit -m "Add scripts/addon.sh for listing, configuring and enabling add-ons"
```

### Task 6: Contract docs and release notes

**Files:**
- Modify: `docs/extending.md` (new section "Add-ons: scripts/addon.sh")
- Modify: `addons/README.md`
- Modify: `docs/private/addon-manifest-spec.md` (gitignored; edit in the main checkout's copy, not committed)
- Modify: `CHANGELOG.md` (Unreleased)

- [ ] **Step 1: Write the `docs/extending.md` section.** Cover the five commands with their stdin/stdout shapes and exit codes, the `data/addons/<short-id>/` layout, and that secrets are accepted only on stdin. Say the contract is experimental in 0.3.x and that field names may change between patch releases with a changelog entry.
- [ ] **Step 2: Update `addons/README.md`.** Cover `gates`, MCP lists, `instances`, `commands`, `mounts` and `volumes`, and the canonical-layout rule for `gates`.
- [ ] **Step 3: Update the private spec.** Record what's now implemented (gates by flag rather than by setting, `settings.<name>.flag`, `list` and `date` types, `services.volumes`). Remove the Twenty two-flag open question's "decide first" note, since gates answer it.
- [ ] **Step 4: Add the CHANGELOG entry** under Unreleased: new manifest fields, `scripts/addon.sh`, no change for existing installs.
- [ ] **Step 5: Run the full add-on suite once**

Run: `make addon-test && sh scripts/test-no-host-python.sh`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add docs/extending.md addons/README.md CHANGELOG.md
git commit -m "Document the add-on CLI contract"
```
