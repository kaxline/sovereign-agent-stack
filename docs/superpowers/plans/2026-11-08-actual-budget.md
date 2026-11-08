# Actual Budget Add-on Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Hermes read/write access to an Actual Budget budget (local or remote) as an
optional built-in add-on, registered from `addons/actual/addon.json`.

**Architecture:** A spike picks the MCP server. Four small extensions to the add-on manifest
path follow: a service per access level, `writeTools` dropped on `ro`, a fail-closed access cap
from the environment, and read-only cron. Then the Actual pieces: compose services, manifest,
`scripts/budget.sh`, doctor checks, the `budget` skill and docs.

**Tech Stack:** POSIX sh and bash 3.2 (bootstraps, scripts), Python 3 standard library
(`addon_manifest.py`, tests), Docker Compose, `actualbudget/actual-server`, a vendored Node MCP
server on `@actual-app/api`.

**Spec:** `docs/superpowers/specs/2026-11-08-actual-budget-design.md`

## Global Constraints

- Both flags (`ACTUAL_MCP_ENABLED`, `ACTUAL_LOCAL_ENABLED`) default to off; installs and Boundary see no change.
- `addons/gptr/addon.json` does not change, and GPT Researcher's written config stays byte-identical: `scripts/test-gptr-mcp-registration.sh` passes unmodified after every core task.
- `permissions.reads` stays `[]` for Actual. Don't change the `reads` rules in `addon_manifest.py` (decided 2026-11-08). Mail and Messages stay `cron: false`.
- Manifests stay in canonical layout: `json.dumps(manifest, indent=2, ensure_ascii=False)` plus a newline.
- First-run scripts (`setup.sh`, `budget.sh init|add`) need no host `python3` or `node`.
- Actual passwords live only in `compose/actual/budget.env` (mode 600, gitignored), never in `.env`, `config.yaml`, Hermes profile `.env` files or bootstrap containers.
- `ACTUAL_ACCESS` lives in `.env`; values `read`, `write` or empty. Anything else gives no entry.
- Each core extension (Tasks 2–5) is its own commit, separate from Actual-specific commits; the Bitwarden work may touch the same files.
- `ACTUAL_SERVER_IMAGE` and the MCP image's `@actual-app/api` version are pinned and bumped together.
- Never point anything at a real budget. Tests use stubs; the live check uses a throwaway local budget.
- Branch `feat/actual-budget`, worktree `../assistant-actual-budget`. Commit as Keith, no Claude trailers, no push.
- Prose follows `~/.claude/rules/writing-style.md`.

## Review Focus

- **Encrypted budget, wrong or missing encryption password:** `budget.sh add` refuses and writes nothing. The MCP's error names the encryption password, not a generic failure. Test in Task 8.
- **Server password changed in the Actual web app after setup:** both MCP instances fail to log in. `make doctor` says to re-run `make budget-add` or `make budget-init`. Test in Task 9.
- **`ACTUAL_ACCESS` with a typo or another case (`Read`, `readonly`):** no `budget` entry on default and browser, never `rw`. Doctor names the bad value. Tests in Tasks 4 and 9.
- **Flag turned off after being on:** both URLs' entries are dropped on every profile, and the cron fence's managed list no longer names `budget`. Tests in Tasks 2 and 5.
- **`ACTUAL_LOCAL_ENABLED=0` while `budget.env` still points at `http://actual-server:5006`:** doctor warns, and Hermes parks the server rather than failing the bootstrap. Test in Task 9.

---

### Task 1: Spike: pick the MCP server and confirm headless setup

The code from this task is throwaway. Its deliverable is a "Spike results" section appended to the spec.

**Files:**
- Modify: `docs/superpowers/specs/2026-11-08-actual-budget-design.md` (append `## Spike results`)
- Scratch only: everything under the session scratchpad

**Produces (for every later task):** the chosen server (repo, commit, license), its port,
transport (`http` or `sse`) and path, how it is told to run read-only, its env var names for
server URL, password, sync id, encryption password and cache dir, the exact tool list split into
read and write, the `actual-server` image tag and matching `@actual-app/api` version, and the
`actual-server` endpoints for first-password bootstrap, login and listing budgets.

- [ ] **Step 1:** Start a throwaway `actual-server` (latest stable tag) on a scratch port with a scratch data dir, and set its password via the bootstrap endpoint (believed to be `POST /account/bootstrap`). Record the endpoint and its response.
- [ ] **Step 2:** Create an empty budget headless with `@actual-app/api` (`runImport` or the equivalent), or confirm that it can't be done. Encrypt a second budget in the web app for Step 4.
- [ ] **Step 3:** For each candidate (giorgiobrullo/actual-mcp, Type-zero-labs/actual-mcp, agigante80/actual-mcp-server), run it in a container against the scratch server and check the five criteria in the spec's "Spike first" section: transport, no write tools when read-only (list tools, then call a write tool by name and expect failure), sync before read (change via rw, read via ro without restart), credentials from env or file with none logged (`docker logs` grep for the password), license.
- [ ] **Step 4:** For the chosen server, open the encrypted budget with a wrong encryption password and record the error text.
- [ ] **Step 5:** Append `## Spike results` to the spec with every item in **Produces**. If no candidate passes, write that down, then add Task 1b before Task 6: a minimal server in `compose/actual-mcp/` on `@actual-app/api` with the same contract. Ask Keith before starting it.
- [ ] **Step 6:** Stop the scratch containers by PID or by port (`lsof -i :PORT` first), delete the scratch data, and commit the spec change: `git commit -m "Record Actual Budget spike results"`.

### Task 2: Core: a service per access level

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`_validate_mcp`, `mcp_plan`, module docstring)
- Modify: `compose/hermes/bootstrap-api-profile.sh` (`addon_mcp_urls`, `register_addon_mcp_servers`)
- Modify: `scripts/test-addon-manifests.py`
- Create: `scripts/test-addon-mcp-access.sh` (fixture manifests under a temp `ADDONS_DIR`; same stub style as `test-gptr-mcp-registration.sh`)
- Create: `scripts/fixtures/addons/split/addon.json` (synthetic add-on `io.example.split`, services `rw`→`split-rw:9000`, `ro`→`split-ro:9000`, tools `get_a get_b set_a`, writeTools `set_a`, profiles default rw, browser rw, api-server ro)
- Modify: `.github/workflows/ci.yml` (run the new test)

**Interfaces:**
- Produces: `mcp.service` is a string or `{"ro": <service key>, "rw": <service key>}`. The `mcp-plan` line gains an 11th field, `all_urls`, which space-separates every URL the add-on can register, in sorted order. Field 5 (`url`) is the URL for this profile's access, or the `rw` URL (else `ro`) when the profile gets no entry. Fields 1–10 are unchanged for a string `service`.
- Produces: `addon_mcp_urls <profile>` prints each URL from field 11, one per line.

- [ ] **Step 1: Write failing validator tests** in `test-addon-manifests.py` with `refused()`/`check()`:
  - `mcp.service {"rw": "server"}` with only `server` defined is accepted.
  - `{"rw": "nope"}` is refused, needle `"mcp.service"`.
  - `{"rx": "server"}` is refused, needle `"mcp.service"`.
  - a mapped service without `port` is refused, needle `".port is needed"`.
  - `profiles {"api-server": "ro"}` with a map that has no `ro` key is refused, needle `"no service for ro"`.
- [ ] **Step 2: Write failing registration tests** in `test-addon-mcp-access.sh` against the fixture:
  - flag on, default → `register:default:split:…` with URL `http://split-rw:9000/mcp`, preceded by `drop:default:http://split-ro:9000/mcp;`.
  - api-server → registers `http://split-ro:9000/mcp`, preceded by a drop of the rw URL.
  - flag off, each profile → exactly `drop:<p>:http://split-ro:9000/mcp;drop:<p>:http://split-rw:9000/mcp;`.
  - `addon_mcp_urls default` prints both URLs.
  - In `test-addon-manifests.py`: `cron_fence` on the fixture (still `cron` unset, which means false) returns both URLs and the name `split`. Without the fix it returns one URL, or crashes on the dict.
- [ ] **Step 3:** Run `python3 scripts/test-addon-manifests.py` and `sh scripts/test-addon-mcp-access.sh` and see both fail.
- [ ] **Step 4:** Implement. Replace main's `_mcp_url(manifest, environ) -> str` with `_mcp_urls(manifest, environ) -> dict[str, str]`, mapping access level to URL. A string service gives `{"": url}`, honouring `mcp.env.url` as today. `cron_fence` (merged in 9e12194) fences every value, so a map add-on with `cron: false` fences both URLs. In `_validate_mcp`, accept the map form and check each key is in `ACCESS`, each value is a `services` key with a `port`, and each level in `profiles` has a service. In `mcp_plan`, resolve per access and append `all_urls`. In `register_addon_mcp_servers`, read the 11th field. When off or with no access, drop every URL in it. Otherwise drop every URL except field 5, then register. `addon_mcp_urls` reads field 11.
- [ ] **Step 5:** Run both tests plus `sh scripts/test-gptr-mcp-registration.sh` and `sh scripts/test-mcp-parked-retry.sh`. All pass, and the gptr test is unmodified.
- [ ] **Step 6: Commit** `git commit -m "Let an add-on map ro and rw to separate MCP services"`

### Task 3: Core: leave `writeTools` out of `ro` profiles

**Files:** `scripts/lib/addon_manifest.py` (`mcp_plan`), `scripts/test-addon-mcp-access.sh`

**Interfaces:**
- Produces: for a profile whose access is `ro`, fields 9 (tools) and 10 (pins) omit every `writeTools` entry. `rw` is unchanged.

- [ ] **Step 1: Failing test:** api-server's register call lists exactly `get_a get_b`, and default lists `get_a get_b set_a`. A fixture pin of `set_a` is absent from api-server's `pin:` call, as are api-server's `addon_mcp_pin_args`.
- [ ] **Step 2:** Run it and see it fail.
- [ ] **Step 3:** Implement the filter in `mcp_plan`, keeping the manifest's tool order.
- [ ] **Step 4:** Run `test-addon-mcp-access.sh`, `test-gptr-mcp-registration.sh` and `test-addon-manifests.py`. All pass.
- [ ] **Step 5: Commit** `git commit -m "Leave an add-on's writeTools off ro profiles"`

### Task 4: Core: fail-closed access cap from the environment

**Files:** `scripts/lib/addon_manifest.py` (`_validate_mcp` env keys, `mcp_plan`), `scripts/test-addon-manifests.py`, `scripts/test-addon-mcp-access.sh`, fixture manifest (add `"env": {"access": "SPLIT_ACCESS"}`)

**Interfaces:**
- Produces: `mcp.env` accepts the key `access` (an env var name). Let `cap` be that variable's value. `read` turns an `rw` profile into `ro`. `write` or empty keeps the manifest's level. Any other value (case-sensitive) gives the profile an empty access field, so it gets no entry. An `ro` profile stays `ro` under `read`, `write` and empty, and gets no entry under any other value.

- [ ] **Step 1: Failing tests:**
  - `SPLIT_ACCESS=read` → default registers the ro URL with `get_a get_b`.
  - `SPLIT_ACCESS=write` and unset → default registers rw.
  - `SPLIT_ACCESS=Read`, `ask` and `readonly` → default and api-server both only drop, never register.
  - The validator accepts `env.access` and still refuses unknown env keys.
  - `test-addon-manifests.py`'s check that both bootstrap services receive each `mcp.env` var applies to `access` too, so the fixture's var must not be required in compose. Scope that check to `addons/`, not fixtures.
- [ ] **Step 2:** Run the tests and see them fail.
- [ ] **Step 3:** Implement in `mcp_plan`. Apply the cap after the profile lookup and before choosing the URL and tools.
- [ ] **Step 4:** Run all three add-on tests. All pass.
- [ ] **Step 5: Commit** `git commit -m "Cap add-on access from an env var, failing closed"`

### Task 5: Core: read-only cron

Builds on the cron fence merged to main in 9e12194: `addon_manifest.cron_fence(addons_dir, environ) -> (names, urls)`, which `fence_mail_from_cron` already loads from `$ADDON_MANIFEST_PY`. Don't add a second module or mount, and don't add a CLI subcommand.

**Files:**
- Modify: `scripts/lib/addon_manifest.py`. `validate` accepts `permissions.cron` `true`, `false` or `"ro"`, with `"ro"` only when `permissions.reads` is empty. `cron_fence` handles `"ro"`.
- Modify: `scripts/test-addon-manifests.py` (validator and `cron_fence` cases)
- Modify: `scripts/test-addon-cron-fence.sh` (a `"ro"` case in the host wiring and in the v0.22.7-image run)
- Modify: the `split` fixture from Task 2: `"permissions": {"cron": "ro"}`

**Interfaces:**
- Consumes: Task 2's per-access URL resolution (the same helper `mcp_plan` uses for `all_urls`).
- Produces: `cron_fence` signature unchanged. For an add-on with `cron: "ro"` it adds no name, and adds every URL in the add-on's `all_urls` except its `ro` URL. A string `mcp.service` with `cron: "ro"` adds nothing, because its one URL is the add-on's only URL and the validator ties `"ro"` to a map with an `ro` service. `cron: false` and invalid manifests behave exactly as on main.

- [ ] **Step 1: Failing tests in `test-addon-manifests.py`:**
  - `cron: "ro"` with empty reads and a service map with an `ro` key is accepted.
  - `cron: "ro"` with `reads ["mail"]` is refused, needle `"permissions.cron false"`.
  - `cron: "ro"` with a string `mcp.service` is refused, needle `"cron ro needs an ro service"`.
  - `cron: "yes"` is refused.
  - `cron_fence` on the fixture returns `([], ["http://split-rw:9000/mcp"])`.
  - with `cron: false` on the fixture, it returns `(["split"], [both URLs])`.
- [ ] **Step 2: Failing case in `test-addon-cron-fence.sh`:** with a `"ro"` add-on registered as `split` at the ro URL on a profile, the cron list keeps `split`. Registered at the rw URL, the cron list loses it.
- [ ] **Step 3:** Run both tests and see the new cases fail.
- [ ] **Step 4:** Implement in `validate` and `cron_fence`. Don't touch the heredoc in `fence_mail_from_cron`, unless the URL match needs it. It already matches by URL, so it shouldn't.
- [ ] **Step 5:** Run `python3 scripts/test-addon-manifests.py`, `sh scripts/test-addon-cron-fence.sh`, `sh scripts/test-gptr-mcp-registration.sh` and `sh scripts/test-bootstrap-stamp.sh`. All pass.
- [ ] **Step 6: Commit** `git commit -m "Let an add-on's ro service stay in cron when its manifest says cron ro"`

### Task 6: Compose services, env keys and profile rows

**Files:**
- Modify: `docker-compose.yml`. Add `actual-server` (profile `budget-local`) and `actual-mcp-rw`/`actual-mcp-ro` (profile `budget`) per the spec's Compose table, using Task 1's image, port and env names. MCP services use `env_file: [{path: ./compose/actual/budget.env, required: false}]`, named volumes `actual_mcp_rw_cache` and `actual_mcp_ro_cache`, the `budget` network plus the network `hermes` uses for MCP, and a healthcheck from Task 1. Pass `ACTUAL_MCP_ENABLED` and `ACTUAL_ACCESS` to both `hermes-*-bootstrap` services.
- Modify: `.env.example` (`ACTUAL_MCP_ENABLED=0`, `ACTUAL_LOCAL_ENABLED=0`, `ACTUAL_ACCESS=`, `ACTUAL_PORT=5006`, `ACTUAL_DATA_DIR=`, `ACTUAL_SERVER_IMAGE=…`, `ACTUAL_MCP_IMAGE=…`)
- Create: `compose/actual/budget.env.example`; modify `.gitignore` (`compose/actual/*`, `!compose/actual/budget.env.example`)
- Vendor: the chosen MCP server under `compose/actual-mcp/` at Task 1's commit, with a `VENDORED.md` naming the upstream repo, commit and license, and a Dockerfile building `${IMAGE_PREFIX:-assistant}-actual-mcp:local`
- Modify: `scripts/sync-profiles.sh`: add the row `ACTUAL_LOCAL_ENABLED budget-local 0` to `SERVICES_CORE`
- Modify: `scripts/test-sync-profiles.sh`, `scripts/test-compose-env-allowlist.py`
- Create: `scripts/test-actual-compose.sh`, modelled on `test-twenty-compose.sh`

- [ ] **Step 1: Failing tests:**
  - `test-sync-profiles.sh`: `ACTUAL_LOCAL_ENABLED=1` adds `budget-local`.
  - `test-actual-compose.sh` (`docker compose config` with both profiles): `actual-server` publishes only on `127.0.0.1`; neither MCP service publishes a port; both have the env file as optional; `ACTUAL_PASSWORD` appears in no service's `environment`; the bootstraps get `ACTUAL_ACCESS` but no `ACTUAL_PASSWORD`.
  - `test-compose-env-allowlist.py`: add the new services' allowed key names.
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement the files above.
- [ ] **Step 4:** Run the three tests, plus `bash scripts/test-bash32.sh`. All pass. Run `docker compose --profile budget --profile budget-local build actual-mcp-rw` and see it succeed.
- [ ] **Step 5: Commit** `git commit -m "Add Actual Budget compose services and flags"`

### Task 7: Manifest and registration

**Files:**
- Create: `addons/actual/addon.json`. It follows the spec's manifest block with Task 1's values: `version 1.0.0`, `minStackVersion` set to the next 0.3.x patch, `builtin.seed false`, `mcp.timeout 120`, `mcp.connectTimeout 60` (the first sync downloads the whole budget), `pin` set to the read tools for accounts, categories, transactions and month summary.
- Modify: `addons/README.md` (Actual is the second add-on)
- Create: `scripts/test-actual-registration.sh`, modelled on `test-gptr-mcp-registration.sh` against the real `addons/` directory
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Failing test.** With `ACTUAL_MCP_ENABLED=1`:
  - default and browser register `budget` at the rw URL with all tools.
  - api-server registers the ro URL with read tools only.
  - `ACTUAL_ACCESS=read` puts default on ro.
  - `ACTUAL_ACCESS=nope` gives no entry on any profile.
  - With the flag off, every profile drops both URLs.
  - `cron-fence-urls default` prints the rw URL.
  - `mcp_tool_name budget <tool>` matches the `pin` values.
- [ ] **Step 2:** Run it and see it fail.
- [ ] **Step 3:** Write the manifest in canonical layout. Its `description` states that it holds financial data and that `reads` stays empty until the finance class ships.
- [ ] **Step 4:** Run `test-actual-registration.sh`, `test-addon-manifests.py` and `test-gptr-mcp-registration.sh`. All pass.
- [ ] **Step 5: Commit** `git commit -m "Register Actual Budget from an add-on manifest"`

### Task 8: `scripts/budget.sh` and make targets

**Files:**
- Create: `scripts/budget.sh` (bash 3.2; sources `scripts/lib/env.sh`; HTTP via host `curl`; anything that needs `@actual-app/api` runs `node` inside the MCP image via `docker compose run --rm --no-deps actual-mcp-rw node /opt/actual-admin.mjs …`)
- Create: `compose/actual-mcp/actual-admin.mjs`, built into the image at `/opt/actual-admin.mjs`. Subcommands: `check` exits 0 if it opens the budget, prints `encryption` or `login` on failure, and exits 2 or 3. `create <name>` prints the new sync id. `list` prints `<sync id>\t<name>` lines. It reads credentials from the environment only.
- Modify: `Makefile` (`budget-init`, `budget-add`, `budget-open`, `budget-remove`, `budget-access`, next to the crm targets)
- Modify: `scripts/setup.sh` (`--budget` sets `ACTUAL_MCP_ENABLED=1` and `ACTUAL_LOCAL_ENABLED=1`; help text in both places `--crm` appears)
- Modify: `scripts/test-no-host-python.sh` (add `scripts/budget.sh`)
- Create: `scripts/test-budget-sh.sh`, using a stub `curl` and a stub `docker` on `PATH`, modelled on `test-crm-sh.sh`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `budget.sh init [--rotate]`, `budget.sh add --url URL [--force]` (reads `ACTUAL_PASSWORD`, `ACTUAL_SYNC_ID` and `ACTUAL_ENCRYPTION_PASSWORD` from the environment, otherwise prompts), `budget.sh open`, `budget.sh remove`, `budget.sh access read|write`. Exit 0 on success, 1 on usage or check failure; nothing is written on failure.

- [ ] **Step 1: Failing tests:**
  - `add` with the stub returning a login failure writes no `budget.env` and prints that the server password was refused.
  - `add` with `check` exiting 2 (encryption) writes nothing and names the encryption password.
  - a successful `add` writes `budget.env` mode 600 with the five keys and sets `ACTUAL_MCP_ENABLED=1` in `.env`.
  - `add` over an existing file without `--force` refuses.
  - `init` with the bootstrap stub and one existing budget adopts it.
  - `init` with two budgets and no `SYNC_ID` lists names and exits 1.
  - `access maybe` exits 1 and leaves `.env` alone.
  - `access read` sets `ACTUAL_ACCESS=read`.
  - `remove` deletes the file and sets the flag to 0.
  - No test output contains the stub password.
- [ ] **Step 2:** Run `bash scripts/test-budget-sh.sh` and see it fail.
- [ ] **Step 3:** Implement. `init` generates the password with `openssl rand -hex 24`, calls the bootstrap endpoint from Task 1, then `create "Budget"` (or adopts, per Task 1's S2 result). `open` uses `open` on macOS, otherwise `xdg-open`, and copies the password with `pbcopy` when present; otherwise it prints the file path.
- [ ] **Step 4:** Run `test-budget-sh.sh` and `test-no-host-python.sh`. Both pass.
- [ ] **Step 5: Commit** `git commit -m "Add budget.sh and make targets for Actual Budget"`

### Task 9: `make doctor` checks

**Files:** `scripts/doctor.sh` (a block next to the Twenty one), a test case in `scripts/test-budget-sh.sh` or a new `scripts/test-actual-doctor.sh` with stubbed `docker`/`curl`

- [ ] **Step 1: Failing tests.** With the flag on:
  - a missing `budget.env` fails with "run make budget-init or make budget-add".
  - mode 644 fails.
  - `ACTUAL_ACCESS=Read` fails, naming the value.
  - `actual-admin.mjs check` returning `login` fails with "the server password changed; re-run make budget-add (remote) or make budget-init --rotate (local)".
  - an ro instance listing a `writeTools` entry fails.
  - `ACTUAL_LOCAL_ENABLED=0` with `ACTUAL_SERVER_URL=http://actual-server:5006` warns.
  - With the flag off, nothing is printed.
- [ ] **Step 2:** Run and see it fail.
- [ ] **Step 3:** Implement with doctor's existing `ok`/`warn`/`bad` helpers. Read the tool list with an MCP `tools/list` over curl from inside the `hermes` container, the way the Twenty block reaches its MCP.
- [ ] **Step 4:** Run and see it pass.
- [ ] **Step 5: Commit** `git commit -m "Check the Actual Budget add-on in make doctor"`

### Task 10: `budget` skill

**Files:** Create `compose/hermes/skills/budget/SKILL.md`, following `compose/hermes/skills/crm/SKILL.md`'s frontmatter and length

- [ ] **Step 1:** Write the skill with the four rules in the spec's Hermes section. Name the read tools from Task 1 that resolve account, category and payee ids, and say that amounts are integer cents.
- [ ] **Step 2: Commit** `git commit -m "Add the budget skill for Hermes"`

### Task 11: Docs, changelog and live check

**Files:** Create `docs/budget.md`; modify `docs/README.md`, `CHANGELOG.md` (Unreleased: new add-on, new `.env` keys, the four manifest extensions, the `mcp-plan` 11th field)

- [ ] **Step 1:** Write `docs/budget.md`: local and remote quick paths, the two instances table (from the spec's Hermes section), where data lives (server data dir, the two cache volumes, which hold financial data), `ACTUAL_ACCESS`, cron on `api-server`, and re-testing after image bumps.
- [ ] **Step 2:** There is no `make test`. Run every test step listed in `.github/workflows/ci.yml` once, as one background command prefixed with `~/.claude/bin/testq`. Fix failures against the failing files only.
- [ ] **Step 3: Live check on a test stack** with a throwaway local budget, never a real one: `./scripts/setup.sh --budget && make up && make budget-init && make hermes-reload && make doctor`. Then:
  - In WebUI, add a transaction and ask for this month's spending.
  - Through the `api-server` API, ask Hermes to add a transaction. It must fail, or have no write tool.
  - Set `ACTUAL_ACCESS=read`, recreate the bootstraps, and confirm WebUI loses write tools.
  - Turn the flag off and confirm `budget` is gone from every profile's `config.yaml` and cron list.
  - Record the results for the changelog.
- [ ] **Step 4: Commit** `git commit -m "Document the Actual Budget add-on"`
- [ ] **Step 5:** Write a memory file for the facts to re-test on image bumps (the `@actual-app/api` and server pairing, the tool names, the bootstrap endpoint) and add it to `MEMORY.md`.
