# Actual Budget: budgets per profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Boundary connect any number of Actual budgets as `addon.sh` instances, list a server's budgets, check and change each one, and set up a local server without the single v0.3.12 budget (CR 0026 R1, R4 and R5).

**Architecture:** `budget.sh` gains the `connect`/`disconnect` hooks `addon.sh` runs, plus `budgets`, `status`, `access <slug>` and `init --server-only`, all printing `phase` JSON lines. Rows live in `addon.sh`'s instance store (`data/addons/actual/`). A launcher in each actual-mcp container watches that store and runs one MCP child per row behind `/<slug>/mcp`. The Hermes bootstraps register `budget-<slug>-ro` and `budget-<slug>` from the same store.

**Tech Stack:** bash 3.2 for host scripts, TypeScript and vitest in the vendored `compose/actual-mcp`, plain Node ESM for `actual-admin.mjs`, POSIX sh and embedded Python 3.11 in the Hermes bootstrap, `@actual-app/api` and `actualbudget/actual-server` 26.11.0.

**Spec:** `docs/superpowers/specs/2026-11-08-actual-budget-design.md`, section "Amendment 2026-11-09: budgets per profile (CR 0026)". Its last subsection, "Settled through round 6", wins over the Design subsection wherever they differ. The agreed wording sent to Boundary is in `docs/private/cr-0026-stack-reply.md` (gitignored, main checkout only), rounds 4 to 6.

**Depends on:**
- Plan 3, `docs/superpowers/plans/2026-11-09-addon-lifecycle.md` on `feat/addon-lifecycle`, merged to `main`. This plan uses its `hooks`, `instances.connect`/`disconnect`/`errors`, `data`, `addon.sh connect`/`disconnect`/`enable` and the `phase` contract. Start only after it merges.
- A `transient` flag on secret instance settings, which plan 3 doesn't have yet. `addon.sh connect` must validate such a setting, pass it to the hook on stdin and redact it, but never write it in step `register`. Without it, `addon.sh` would store the person's Actual password in `secrets/<slug>.password`. Agree it with the plan 3 owner and land it in plan 3 or as its first follow-up. Task 8 uses it.

## Global Constraints

- Output, in `--json` mode: stdout carries only JSON objects, one per line, each with a string `phase`. The last line is `{"phase":"done",...}` or `{"phase":"error","error":<name>,"message":<one sentence>}`, and nothing follows it. `addon.sh` parses only stdout and relays only objects with a string `phase`; stderr goes to its step log, so an error line written to stderr becomes `command_failed`. Human `error: ...` text goes to stderr in addition to the stdout line, never instead of it. Without `--json`, the existing `[budget]` log lines stay.
- Error names: the shared `bad_slug`, `exists`, `not_found`, `login_refused`, `token_expired`, `no_access`, `timeout`, `server_unreachable`, and Actual's `unknown_profile`, `bad_access`, `encryption_password`. Never `bad_input`, `missing_settings`, `command_failed`, `compose_failed` or `busy`: those are `addon.sh`'s own.
- Exit codes for `budget.sh` in `--json` mode: 0 for `done`; 2 for `bad_slug`, `unknown_profile`, `bad_access`, `not_found`; 4 for every other error name.
- No secret (password, session token, encryption password, server password) in argv, an env var visible to `docker inspect`, any stdout or stderr line, a log, or a file outside `data/addons/actual/secrets/` (mode 600) and `compose/actual/budget.env`. Containers get credentials by name (`-e NAME`) as `budget.sh` does today.
- Data paths: `${ACTUAL_ADDON_DATA_DIR:-./data/addons/actual}` on the host, which must equal `addon.sh`'s `data/addons/actual` under `ASSISTANT_DATA_ROOT`. Rows are `instances/<slug>.json`; secrets are `secrets/<slug>.session_token`, `secrets/<slug>.encryption_password` and `secrets/server_password`.
- Instance settings: `server` (string: an `http(s)` URL or `local`), `sync_id` (string), `profile` (string, default `default`), `access` (string, default `read`), and optional secrets `session_token`, `password` (transient), `encryption_password`.
- `profile` must be `default` in this build. MCP names: `budget-<slug>-ro` on `http://actual-mcp-ro:3000/<slug>/mcp`, and for `write` rows `budget-<slug>` on `http://actual-mcp-rw:3000/<slug>/mcp`. `browser` gets the same entries as `default`; `api-server` gets none.
- The local server is `http://actual-server:5006` from containers and `http://127.0.0.1:${ACTUAL_PORT:-5006}` from the host.
- v0.3.12 behaviour stays: `make budget-init`, `budget-add`, `budget-access ACCESS=…`, `budget-remove` and the single `budget` entry work as before when `ACTUAL_BUDGET_FILE` is set.
- Tests: stubs only, except Task 1's container test, which skips when the images are missing. While iterating, run the named test files. Run the full suite (Task 9 Step 3 lists it) once before merging, in the background.
- Follow `~/.claude/rules/writing-style.md` for docs, comments and commit messages. Commits use Keith's git identity with no trailers.

## Review Focus

- A person disconnects a budget and connects the same slug with another credential within 2 seconds: the launcher must still restart that child with an empty cache, so nothing cached under the old credential is pushed. Task 6 tests a changed token file within one poll.
- A row whose secret file is missing or unreadable (a half-finished `connect`, a hand-deleted file): the launcher starts no child for it, logs the slug and the reason, and keeps serving the other rows. Task 6.
- `budgets` or `connect` against a URL that answers HTML, a 502 or a redirect instead of Actual JSON: the result is `server_unreachable`, not a crash or a blank error. Task 2.
- A budget named with quotes, a backslash, a tab or non-ASCII text: every `budget` line stays valid JSON with the name intact apart from tabs and newlines becoming spaces. Task 2.
- The single budget and rows together on one install: `make budget-remove` drops only the `budget` entry while rows remain, and the rows keep working. Task 5 and Task 7.

---

### Task 1: `actual-admin.mjs` signs in with a session token

**Files:**
- Modify: `compose/actual-mcp/actual-admin.mjs`
- Create: `scripts/test-actual-admin.sh`
- Modify: `.github/workflows/ci.yml` (one step, which skips without the images)

**Interfaces:**
- Produces: `check` uses `ACTUAL_SESSION_TOKEN` when set (`api.init({ dataDir, serverURL, sessionToken })`) and `ACTUAL_PASSWORD` otherwise. Exit codes: 0 ok; 1 other; 2 `encryption`; 3 `login` (codes `invalid-password`, `unauthorized`, `token-expired`, `token-not-found`); 4 `timeout`; 5 `no_access` (`budget-not-found` from `downloadBudget`); 6 `unreachable` (`network-failure`). The word goes to stdout as today. The token joins `secrets` for scrubbing.
- `create` and `list` take the token too.

- [ ] **Step 1: Write the failing test.** `scripts/test-actual-admin.sh` (bash) exits 0 with `skip:` when `actualbudget/actual-server:26.11.0` or `assistant-actual-mcp:local` is missing. Otherwise it starts the server on a scratch network, bootstraps it with a made-up password, creates a budget with `actual-admin.mjs create Spike`, signs in over `/account/login` for a token, and checks: `check` with the token exits 0; with `tok-bogus` prints `login`, exits 3; with a made-up sync id prints `no_access`, exits 5; with `ACTUAL_SERVER_URL=http://actual-server:5999` prints `unreachable`, exits 6; no stdout or stderr line contains the password or the token. It removes its containers and network on exit.
- [ ] **Step 2: Run it and confirm the token, `no_access` and `unreachable` cases fail.** Run: `bash scripts/test-actual-admin.sh`. Expected: FAIL at the token case. If `network-failure` isn't the code a closed port gives, use the code the run shows and say so in the commit.
- [ ] **Step 3: Implement** the token path and the two new exit codes, and update the header comment.
- [ ] **Step 4: Run it and confirm it passes.** Run: `docker compose build actual-mcp-rw && bash scripts/test-actual-admin.sh`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add compose/actual-mcp/actual-admin.mjs scripts/test-actual-admin.sh .github/workflows/ci.yml
git commit -m "Let actual-admin.mjs sign in with a session token and name missing budgets and dead servers"
```

### Task 2: JSON lines and `budget.sh budgets` (R4)

**Files:**
- Modify: `scripts/budget.sh` (`emit_*`, `parse_user_files`, `cmd_budgets`, usage)
- Modify: `scripts/test-budget-sh.sh` (new `# --- budgets ---` section; the stub curl serves `encryptKeyId`)

**Interfaces:**
- Produces, in `budget.sh`:
  - `emit_line JSON`, `emit_done [KEY JSON_VALUE]...`, `emit_error NAME MESSAGE` (prints the line, `error: MESSAGE` on stderr, and exits with the code from Global Constraints). In non-JSON mode `emit_error` keeps today's `[budget] ERROR:` text.
  - `read_input`: reads one JSON object from stdin into `in_server`, `in_sync_id`, `in_profile`, `in_access`, `in_session_token`, `in_password`, `in_encryption_password`, using the image's `python3` through `stack_python` (as `addon.sh` does) rather than a regex.
  - `server_base SERVER`: `local` gives `http://127.0.0.1:${ACTUAL_PORT:-5006}` and reads `secrets/server_password`; an `http://` or `https://` URL gives itself. Anything else is `emit_error server_unreachable "server must be an http(s) URL or local"`.
  - `sign_in`: a token from `in_session_token`, or `POST /account/login` with `in_password` (or the server password for `local`). `invalid-password` → `login_refused`; 401 `token-not-found` or `token-expired` → `token_expired`; no answer, non-JSON or a non-2xx answer → `server_unreachable`; curl's `--max-time 45` → `timeout`.
  - `parse_user_files` prints `<sync id>\t<name>\t<1|0>`, the last field from a non-empty `encryptKeyId`. Callers that use two fields keep working.
  - `budget.sh budgets --json < input`: `sign_in`, then `GET /sync/list-user-files`, then one `{"phase":"budget","sync_id":…,"name":…,"encrypted":true|false}` per row and `{"phase":"done"}`.

- [ ] **Step 1: Write the failing tests.** The stub server lists two files, one with `encryptKeyId` and a name holding `"`, `\` and a tab. Cases: the right password gives two `budget` lines (the encrypted flag right, the name round-tripping through `python3 -c 'json.loads'`) then `done`, exit 0; a wrong password gives only `{"phase":"error","error":"login_refused",…}`, exit 4; the stub answering `<html>` gives `server_unreachable`; `down` gives `server_unreachable`; `{"server":"local"}` uses the stored server password; `{"server":"ftp://x"}` is an error; no output line and no `curl.argv` line contains the password.
- [ ] **Step 2: Run and confirm they fail.** Run: `bash scripts/test-budget-sh.sh`. Expected: FAIL in the `budgets` section only.
- [ ] **Step 3: Implement** the helpers and `cmd_budgets`.
- [ ] **Step 4: Run and confirm they pass.** Run: `bash scripts/test-budget-sh.sh && sh scripts/test-bash32.sh`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add scripts/budget.sh scripts/test-budget-sh.sh
git commit -m "Add budget.sh budgets, which lists the budgets a credential can open as JSON lines"
```

### Task 3: `connect` and `disconnect` hooks

**Files:**
- Modify: `scripts/budget.sh` (`cmd_connect`, `cmd_disconnect`)
- Modify: `scripts/test-budget-sh.sh` (`# --- connect ---`, `# --- disconnect ---`)

**Interfaces:**
- Consumes: Task 1's exit codes; Task 2's `read_input`, `sign_in`, `emit_*`.
- Produces:
  - `budget.sh connect --slug SLUG --json < input`. Checks in order, writing nothing until all pass: slug matches `^[a-z0-9][a-z0-9-]{0,30}\Z` (`bad_slug`); `profile` is `default` (`unknown_profile`); `access` is `read` or `write` (`bad_access`); `sign_in`; `actual-admin.mjs check` with the token, sync id and encryption password (exit 2 → `encryption_password`, 3 → `token_expired` when the input had a token and `login_refused` when it had a password, 4 → `timeout`, 5 → `no_access`, 6 → `server_unreachable`). Then it writes `secrets/<slug>.session_token` and, if given, `secrets/<slug>.encryption_password` (mode 600, temp file and `mv`), and prints `{"phase":"done","account":SLUG,"mcp":[…]}` with the names from Global Constraints.
  - `budget.sh disconnect --slug SLUG --json`: removes `secrets/<slug>.*` and prints `done`. A slug with no files is `not_found`. `addon.sh` removes the instance file after the hook.

- [ ] **Step 1: Write the failing tests.** Cases: a password connect writes `secrets/main.session_token` holding `tok-1` (mode 600), no file holds the password, and `done` has `"account":"main","mcp":["budget-main-ro"]`; with `"access":"write"`, `mcp` also has `budget-main`; a token connect stores the token as given; each error name above, from the stub docker's exit code or the stub server, leaves `secrets/` empty; `--slug Bad_Slug` is `bad_slug` with exit 2; `"profile":"alice"` is `unknown_profile`; `disconnect --slug main` removes both secret files; `disconnect --slug nope` is `not_found`, exit 2. The existing "no password in any output" scan covers both commands.
- [ ] **Step 2: Run and confirm they fail.** Run: `bash scripts/test-budget-sh.sh`. Expected: FAIL in the new sections only.
- [ ] **Step 3: Implement** `cmd_connect` and `cmd_disconnect`, and add both to the usage text as hooks `addon.sh` runs.
- [ ] **Step 4: Run and confirm they pass.** Run: `bash scripts/test-budget-sh.sh`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add scripts/budget.sh scripts/test-budget-sh.sh
git commit -m "Add budget.sh connect and disconnect, the hooks addon.sh runs for an Actual budget"
```

### Task 4: `status` and `access <slug>`

**Files:**
- Modify: `scripts/budget.sh` (`cmd_status`, `cmd_access`), `scripts/lib/doctor-actual.sh`
- Modify: `scripts/test-budget-sh.sh`, `scripts/test-actual-doctor.sh`

**Interfaces:**
- Consumes: Task 3's secret layout; Task 1's exit codes.
- Produces:
  - `budget.sh status [--slug SLUG] --json`: for each `instances/*.json` (or the one slug, `not_found` if absent), runs `check` with the stored token and prints `{"phase":"budget","slug","profile","access","state"}`. `state` is `ok`, `token_expired` (exit 3), `no_access` (5), `encryption_password` (2), `timeout` (4), `server_unreachable` (6), or `bad_access` when the stored access isn't `read` or `write`. A row with no token file is `token_expired`. Ends with `done`; per-row states never make it exit non-zero.
  - `budget.sh access SLUG read|write --json`: `bad_access` or `not_found` as above; otherwise writes `access` into `instances/<slug>.json` through `addon.sh configure actual --instance SLUG` with `{"access":…}` on stdin, runs both Hermes bootstraps with `HERMES_BOOTSTRAP_FORCE=1` (the same two `docker compose run` calls as plan 3's `reload_hermes`), and prints `done` with `mcp`. `access read|write` with one argument keeps its v0.3.12 behaviour.
  - `make doctor` lists each row as `budget-<slug>: <state>`, warning on anything but `ok`.

- [ ] **Step 1: Write the failing tests.** Two rows, one with a token the stub docker accepts and one it rejects with exit 3, give `ok` and `token_expired` lines and `done`, exit 0; `--slug nope` is `not_found`; a row with `"access":"admin"` is `bad_access`. `access main write` logs the `addon.sh configure` call with `{"access":"write"}` and both bootstrap runs, and prints `mcp` with both names; `access main sudo` is `bad_access`; `access read` (one argument) still changes `ACTUAL_ACCESS`. The doctor test shows one warning line for the expired row.
- [ ] **Step 2: Run and confirm they fail.** Run: `bash scripts/test-budget-sh.sh && bash scripts/test-actual-doctor.sh`. Expected: FAIL on the new cases only.
- [ ] **Step 3: Implement** both commands and the doctor lines.
- [ ] **Step 4: Run and confirm they pass.** Same command. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add scripts/budget.sh scripts/lib/doctor-actual.sh scripts/test-budget-sh.sh scripts/test-actual-doctor.sh
git commit -m "Add budget.sh status and per-budget access, and show each connected budget in make doctor"
```

### Task 5: `init --server-only` and `remove` beside rows (R5)

**Files:**
- Modify: `scripts/budget.sh` (`cmd_init`, `cmd_remove`), `Makefile` (comment only)
- Modify: `scripts/test-budget-sh.sh`

**Interfaces:**
- Consumes: Task 2's `emit_*` and `parse_user_files`.
- Produces:
  - `budget.sh init --server-only [--json]`: on an unbootstrapped server, bootstraps it with a generated password kept in `secrets/server_password` (the pending-file logic moves there). On a bootstrapped server, uses `secrets/server_password`, else the password in `compose/actual/budget.env`, else `login_refused`. Creates `Budget` when the server lists no budget. `done` carries `sync_id` and `name` only when exactly one budget is listed afterwards. Writes no `budget.env`, no `ACTUAL_BUDGET_FILE` and no `ACTUAL_MCP_ENABLED`. Re-running it is a no-op apart from the `done` line.
  - `budget.sh remove`: deletes `budget.env` and clears `ACTUAL_BUDGET_FILE` as now, and sets `ACTUAL_MCP_ENABLED=0` only when `instances/` holds no `*.json`.

- [ ] **Step 1: Write the failing tests.** A fresh stub server: `init --server-only --json` leaves `secrets/server_password` (mode 600), creates `Budget`, prints `done` with its `sync_id` and `"name":"Budget"`, and `.env` has no `ACTUAL_BUDGET_FILE` or `ACTUAL_MCP_ENABLED`. A second run creates nothing. With two budgets on the server, `done` has no `sync_id`. A server set up by `init` (budget.env present) is adopted with no new password. A bootstrapped server with no known password is `login_refused`. `remove` with one row file leaves `ACTUAL_MCP_ENABLED=1`; with none, sets it to 0.
- [ ] **Step 2: Run and confirm they fail.** Run: `bash scripts/test-budget-sh.sh`. Expected: FAIL on the new cases only.
- [ ] **Step 3: Implement** `--server-only` and the `remove` check.
- [ ] **Step 4: Run and confirm they pass.** Run: `bash scripts/test-budget-sh.sh && sh scripts/test-bash32.sh`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add scripts/budget.sh scripts/test-budget-sh.sh Makefile
git commit -m "Set up a local Actual server without the single budget, and keep the add-on on while budgets are connected"
```

### Task 6: The launcher in the actual-mcp containers

**Files:**
- Create: `compose/actual-mcp/src/launcher/rows.ts`, `compose/actual-mcp/src/launcher/proxy.ts`, `compose/actual-mcp/src/launcher/main.ts`
- Create: `compose/actual-mcp/src/launcher/rows.test.ts`, `compose/actual-mcp/src/launcher/proxy.test.ts`
- Modify: `compose/actual-mcp/Dockerfile` (`ENTRYPOINT ["node", "build/launcher/main.js"]`), `compose/actual-mcp/VENDORED.md`

**Interfaces:**
- Produces:
  - `readRows(dir: string, write: boolean): Row[]` in `rows.ts`, where `Row = { slug, serverUrl, syncId, token, encryptionPassword?, fingerprint }`. It reads `instances/*.json` and `secrets/<slug>.*` under `dir`. `write` keeps only rows whose `access` is `write`. A row with an invalid slug, `access` not `read`/`write`, or a missing or empty token is skipped with one stderr line naming the slug and why. `server: "local"` becomes `http://actual-server:5006`. `fingerprint` is a hash of the row's JSON and secret file contents and mtimes, so any change restarts the child.
  - `class Children` in `rows.ts`: `reconcile(rows: Row[])` starts, stops and restarts children so they match `rows`. Each child is `node build/index.js --sse --port <4100+n> [--enable-write]` with `cwd` an empty per-slug directory (so dotenv loads no `.env`), and env `ACTUAL_SERVER_URL`, `ACTUAL_SESSION_TOKEN`, `ACTUAL_BUDGET_SYNC_ID`, `ACTUAL_BUDGET_ENCRYPTION_PASSWORD`, `ACTUAL_DATA_DIR=/data/rows/<slug>`, `ACTUAL_SYNC_TTL_MS=0` and `ACTUAL_RECONNECT_HINT="reconnect it with addon.sh disconnect actual --instance <slug>, then addon.sh connect"`. Stopping a child (SIGTERM, then SIGKILL after 5 s) deletes `/data/rows/<slug>`. A child that exits on its own restarts after 2 s, at most once a minute.
  - `createProxy(lookup: (slug: string) => number | undefined, legacyPort: number | undefined): http.Server` in `proxy.ts`: `/<slug>/mcp` goes to that child's `/mcp`; `/mcp`, `/sse` and `/messages` go to the legacy child; an unknown slug is 404. A child's 400 whose body contains `No valid session ID provided` becomes 404 with the same body. Streaming responses pass through unbuffered.
  - `main.ts`: listens on `:3000`, starts the legacy child (`node build/index.js --sse --port 4099 [--enable-write]`, `cwd` `/app`) exactly as the container ran before, polls `ACTUAL_ROWS_DIR` (default `/budgets`) every 2 s, and passes `--enable-write` to children when started with it.

- [ ] **Step 1: Write the failing tests.** `rows.test.ts`, on a temp dir: two valid rows give two `Row`s; `write: true` keeps only the `write` one; `"local"` maps to `http://actual-server:5006`; a row with no token file, one with `access: "admin"` and one named `Bad_Slug` are each skipped with a stderr line; rewriting a token file changes `fingerprint` even with the same length and an mtime within the same second. `Children` with a fake spawn: a new row spawns once with the env above and `cwd` not `/app`; removing it kills the child and deletes its data dir; a changed fingerprint kills, deletes and respawns; a child crash respawns after 2 s, and a second crash within a minute waits. `proxy.test.ts` with fake child servers: routing for `/a/mcp`, `/mcp` and `/nope/mcp`; the 400-to-404 rewrite; an SSE response arrives in chunks.
- [ ] **Step 2: Run and confirm they fail.** Run: `cd compose/actual-mcp && npx vitest run src/launcher`. Expected: FAIL, modules missing.
- [ ] **Step 3: Implement** the three modules and the Dockerfile change. Add the launcher to VENDORED.md's list of local changes.
- [ ] **Step 4: Run and confirm they pass.** Run: `cd compose/actual-mcp && npx vitest run && npx tsc --noEmit -p tsconfig.json`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add compose/actual-mcp/src/launcher compose/actual-mcp/Dockerfile compose/actual-mcp/VENDORED.md
git commit -m "Run one Actual MCP child per connected budget behind a launcher that follows the instance store"
```

### Task 7: Compose and Hermes registration

**Files:**
- Modify: `docker-compose.yml` (`actual-mcp-rw`, `actual-mcp-ro`: mount `${ACTUAL_ADDON_DATA_DIR:-./data/addons/actual}` read-only at `/budgets`; both bootstrap services: mount its `instances/` read-only at `/bootstrap/actual-instances` and pass `ACTUAL_BUDGET_FILE`)
- Modify: `compose/hermes/bootstrap-api-profile.sh` (`register_actual_budget_rows`, the single-budget drop, the fence match)
- Create: `scripts/lib/actual_rows.py` (runs in the bootstrap's `python3`)
- Create: `scripts/test-actual-rows.sh`
- Modify: `scripts/test-actual-compose.sh`, `scripts/test-compose-env-allowlist.py` (if it lists bootstrap env), `scripts/test-addon-cron-fence.sh`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the instance layout; the manifest's `mcp.tools`, `writeTools`, `pin`, `timeout` and `connectTimeout` through `addon_manifest.py`.
- Produces:
  - `actual_rows.py plan INSTANCES_DIR ADDONS_DIR PROFILE`: one line per entry, `name|title|url|timeout|connect_timeout|tools|pins`, using the manifest's read tools for `-ro` and all tools for the `write` entry. Only rows with `profile` `default` appear, on `default` and `browser`; `api-server` gets nothing. Skips rows as `readRows` does.
  - `register_actual_budget_rows PROFILE`, called after `register_addon_mcp_servers` on `default` and on `$PROFILE`: when `ACTUAL_MCP_ENABLED` is off, drops every entry whose URL starts with `http://actual-mcp-ro:3000/` or `http://actual-mcp-rw:3000/` and isn't the legacy `/mcp`; otherwise registers the plan's entries, pins their tools and drops `budget-*` entries on those hosts that the plan no longer lists. When `ACTUAL_BUDGET_FILE` is empty or ends in `no-budget.env`, it also drops the legacy `budget` entry.
  - `fence_mail_from_cron` treats a server as fenced when its URL's scheme, host and port match a fenced add-on URL's, instead of the exact URL.

- [ ] **Step 1: Write the failing tests.** `scripts/test-actual-rows.sh` runs `actual_rows.py plan` on fixture rows: a `read` row gives one `-ro` line with 18 tools; a `write` row gives both lines, the second with the full list; a `profile` `alice` row gives nothing; `api-server` gives nothing. It then runs `register_actual_budget_rows` in the Hermes image against a scratch config (as `test-actual-registration.sh` does) and checks the entries, that a removed row's entries go, and that the legacy `budget` entry goes when `ACTUAL_BUDGET_FILE` is unset and stays when it's set. `test-addon-cron-fence.sh` gains `budget-main` on `http://actual-mcp-rw:3000/main/mcp`, which ends up fenced while `budget-main-ro` stays. `test-actual-compose.sh` checks both mounts are read-only.
- [ ] **Step 2: Run and confirm they fail.** Run: `sh scripts/test-actual-rows.sh; sh scripts/test-addon-cron-fence.sh --host-only; sh scripts/test-actual-compose.sh`. Expected: FAIL on the new cases only.
- [ ] **Step 3: Implement** the compose changes, `actual_rows.py`, the bootstrap function, the drop and the fence match.
- [ ] **Step 4: Run and confirm they pass.** Same commands, plus `sh scripts/test-actual-registration.sh`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml compose/hermes/bootstrap-api-profile.sh scripts/lib/actual_rows.py scripts/test-actual-rows.sh scripts/test-actual-compose.sh scripts/test-compose-env-allowlist.py scripts/test-addon-cron-fence.sh .github/workflows/ci.yml
git commit -m "Register each connected Actual budget on default and browser, and keep its write entry out of cron"
```

### Task 8: Actual's manifest

**Files:**
- Modify: `addons/actual/addon.json` (written with `addon_manifest.canonical`)
- Modify: `scripts/test-addon-manifests.py`, `scripts/test-actual-registration.sh` (if the plan output changes)

**Interfaces:**
- Consumes: plan 3's fields and the `transient` flag; Tasks 3 and 5's commands.
- Produces, in the manifest:
  - `gates: [{"flag": "ACTUAL_LOCAL_ENABLED", "profile": "budget-local", "seed": false}]`, a `services.server` entry for `actual-server` (port 5006) with `"when": {"gate": "ACTUAL_LOCAL_ENABLED"}`, and a top-level `local` bool setting with flag `ACTUAL_LOCAL_ENABLED`, default `false`.
  - `hooks.init: {"script": ["scripts/budget.sh", "init", "--server-only"]}`.
  - `instances: {"name": "budget", "settings": {…as Global Constraints, password with "transient": true…}, "connect": {"script": ["scripts/budget.sh", "connect"]}, "disconnect": {"script": ["scripts/budget.sh", "disconnect"]}, "errors": ["bad_access", "encryption_password", "unknown_profile"]}`.
  - `data: {"paths": [{"env": "ACTUAL_DATA_DIR", "default": "./data/actual/server"}], "volumes": ["actual_mcp_rw_cache", "actual_mcp_ro_cache"]}`, replacing any `services.<k>.volumes`.

- [ ] **Step 1: Write the failing test.** In `test-addon-manifests.py`: the real Actual manifest validates; `addon_manifest.hook(actual, "init")`, `"connect"` and `"disconnect"` return the script forms above; `listing()` shows `errors` and `hooks`; `mcp-plan` output for `default`, `browser` and `api-server` is unchanged from before this task.
- [ ] **Step 2: Run and confirm it fails.** Run: `python3 scripts/test-addon-manifests.py`. Expected: FAIL on the Actual cases.
- [ ] **Step 3: Implement** the manifest change with `canonical()`.
- [ ] **Step 4: Run and confirm it passes.** Run: `python3 scripts/test-addon-manifests.py && sh scripts/test-actual-registration.sh && make addon-test`. Expected: exit 0.
- [ ] **Step 5: Commit**

```bash
git add addons/actual/addon.json scripts/test-addon-manifests.py scripts/test-actual-registration.sh
git commit -m "Give the Actual manifest its init and connect hooks, its error names and its data"
```

### Task 9: Docs, changelog and the full suite

**Files:**
- Modify: `docs/budget.md`, `CHANGELOG.md` (Unreleased), `docs/superpowers/specs/2026-11-08-actual-budget-design.md` (amendment header: "Built in …"), `Makefile` (`budget-connect` and `budget-disconnect`, wrapping `addon.sh`)

- [ ] **Step 1: Docs.** In `docs/budget.md`, a section on connected budgets: `addon.sh connect actual --instance <slug>` with the stdin keys, `make budget-connect SLUG=… URL=…` (which reads the password from the terminal and pipes JSON to `addon.sh`), `budget.sh budgets`, `status`, `access <slug>`, `init --server-only`, what lives under `data/addons/actual/`, that only `default` is supported, and that a password change on the server ends every connected budget until it is reconnected. CHANGELOG under Unreleased: the new commands, the launcher, the new mounts, `remove` keeping the flag on while budgets are connected, and the single `budget` entry now needing `ACTUAL_BUDGET_FILE`.
- [ ] **Step 2: Live-check notes.** Add the new commands to `docs/private/actual-budget-live-check.md` in the main checkout (gitignored; don't commit it): `init --server-only`, `budgets`, `addon.sh connect` for a local and a remote budget, `status`, `access <slug> write`, `disconnect`, and a `create-transaction` through `budget-<slug>` that a fresh download then shows.
- [ ] **Step 3: Run the full suite once, in the background.** There is no `make check`: run every step of the `compose-config`, `project-sources` and `shellcheck` jobs in `.github/workflows/ci.yml` locally, after stubbing `.env` and the overlays the way the "Stub local overlays" step does, plus `cd compose/actual-mcp && npx vitest run`. Expected: every step exits 0.
- [ ] **Step 4: Commit**

```bash
git add docs/budget.md CHANGELOG.md docs/superpowers/specs/2026-11-08-actual-budget-design.md Makefile
git commit -m "Document connected Actual budgets"
```

## Out of scope

- Rows on Hermes profiles other than `default`: the bootstraps only register add-ons on `default`, `browser` and `api-server`. Member profiles need that first.
- Keeping `budget-<slug>-ro` out of chats on `write` rows (the toolset allowlist), and bearer auth on the MCP ports (R3).
- A live run. Before release, Keith runs the connect, budgets, status and init commands in `docs/private/actual-budget-live-check.md` on his stack, which Task 9 extends with the new commands.
