# Add-on lifecycle in addon.sh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `scripts/addon.sh` the lifecycle Boundary asked for in its reply to the merged `addon.sh` (CR 0020 items 1 to 7): JSON-lines progress, a first-time `init` hook, `connect`/`disconnect`, reload without a `hermes` recreate, `remove --delete-data`, per-service resource figures, and a tested environment pass-through, for release 0.3.13.

**Architecture:** `addon.sh` stays the host-side driver and `scripts/lib/addon_cli.py` the parser that runs in the Hermes image. A new `scripts/lib/addon-events.sh` prints the JSON lines, so every subcommand reports through one emitter. Manifests gain `oneshot`, `hooks`, `onToggle`, `data` and `backup`, checked in `addon_manifest.py`. Waiting for an MCP server moves to Boundary: `addon.sh` names the servers in its `done` line, and Boundary checks them in `mcp.servers.status`, since it already holds a gateway connection and `addon.sh` doesn't.

**Tech Stack:** bash 3.2 (macOS `/bin/bash`), Python 3.11 standard library through `stack_python`, Docker Compose v2.29 or later (`--progress json`).

**Spec:** Boundary's request is `boundary-desktop/docs/change-requests/0020-ui-installable-addons.md`, section "`addon.sh` as merged" (read-only, don't edit that repo). The stack's triage and agreed answers are in `docs/private/cr-0020-addon-sh-reply-triage.md`, and the reply draft is `docs/private/cr-0020-addon-sh-stack-reply-draft.md`. Both are gitignored and live only in the main checkout (`/Users/admin/Documents/Work/Personal/assistant/docs/private/`). The manifest fields are settled in `docs/private/addon-manifest-spec.md`, section "Lifecycle fields for `addon.sh`" (Keith, 2026-11-09), in the same place. That section is the binding authority for `data`, `hooks`, `instances.connect`/`disconnect` and their error names. Read it and the triage file before Task 1. **Depends on:** plan 1 (`docs/superpowers/plans/2026-11-08-addon-manifest-path.md`, merged in 482601d). Plan 2 (`2026-11-08-email-addons.md`) runs after this plan, and its old Task 5 is this plan's Task 8.

## Global Constraints

- Output contract, in `--json` mode only: stdout carries one JSON object per line and nothing else. Every run ends with exactly one `{"phase":"done",...}` or `{"phase":"error",...}` line. Human-readable `error: ...` lines stay on stderr in both modes.
- Phases: `pull`, `setup`, `done`, `error`. A `setup` line is `{"phase":"setup","step":<name>,"status":"running"|"done"|"error"}`. Step names: `flags`, `start`, `init`, `check`, `register`, `reload`, `recreate`, `stop`, `delete`. A step that runs per service or path adds `"target":<compose service, volume or path>`.
- `done` line: `{"phase":"done","id":<short id>,"action":<subcommand>,"restart":[...],"mcp":[...]}`, plus `"account"` when a hook reported one. `restart` lists containers the run recreated. `mcp` lists the MCP server names Boundary should wait for in `mcp.servers.status`, which is the `name` of each manifest `mcp` entry whose gate is on, or the names a `connect` hook reported.
- `error` line: `{"phase":"error","error":<code>,"step":<name>,"message":<text>,"logTail":[<up to 20 lines>]}`, plus `"rolledBack":true` when `enable` restored the previous flags.
- Error codes from `addon.sh` and their exit codes: `bad_input` 2, `missing_settings` 3, `command_failed` 4, `compose_failed` 1, `busy` 1. A hook's error passes through unchanged with exit 4 when its name is a shared name (`bad_slug`, `exists`, `not_found`, `login_refused`, `token_expired`, `no_access`, `timeout`, `server_unreachable`) or listed in the manifest's `instances.errors`. Any other name becomes `command_failed`, with the hook's name and message in `message`.
- Secrets arrive only as one JSON object on stdin. Never put a value in argv, an env var, a log line, `logTail` or a file outside `data/addons/<id>/secrets` and the staging file Task 5 defines.
- Every output line is valid JSON: control characters other than tab are dropped, tab becomes a space, and a log line longer than 500 characters is cut to 500.
- Compose calls inherit the caller's environment. `addon.sh` never sets or unsets `COMPOSE_PROJECT_NAME` or `COMPOSE_PROFILES` for a compose call, with one exception: `list --json` runs `docker compose --profile '*' config`, which reads every service and starts nothing.
- No `make` calls from `addon.sh`, and no `hermes` recreate unless the manifest's `onToggle` asks for one.
- Built-in manifests stay canonical JSON (`addon_manifest.canonical`). New fields: `services.<k>.oneshot`, `hooks`, `instances.connect`, `instances.disconnect`, `instances.errors`, `onToggle`, `data`, `backup`. `data.volumes` replaces `services.<k>.volumes`; the validator accepts both in 0.3.13 and refuses the old form from the next patch.
- Tests use stub `docker` and `make` in `scripts/test-addon-cli.sh` and fixtures under `scripts/fixtures/`. No task runs a live compose stack. While iterating, run the targeted test files; run `make addon-test` once before each commit.
- Follow `~/.claude/rules/writing-style.md` for docs, comments and commit messages. Commits use Keith's git identity with no trailers.

## Review Focus

- Two mutating `addon.sh` calls at once (a double click in Boundary): the second must fail with `busy` straight away and leave `.env` as the first call wrote it. Task 2 owns the lock and its test.
- An `enable` killed mid-run (SIGINT or SIGTERM from Boundary): the flags roll back and the lock is released, so the next call isn't `busy` forever. Task 2.
- A hook that echoes its stdin, so a password lands in its output: `logTail` must show `[redacted]` in place of every secret value from stdin. Task 5.
- `data.paths` that resolve outside `./data` and `ASSISTANT_DATA_ROOT`, or to one of them itself (`ACTUAL_DATA_DIR=/`, `ACTUAL_DATA_DIR=..`, a symlink pointing out): `remove` must keep the directory, report it as `kept` with the reason, and never follow the symlink. Task 7.
- A container log with a quote, backslash, ANSI escape or a 10,000-character line: every `logTail` line stays valid JSON. Task 2.

---

### Task 1: Manifest fields `oneshot`, `hooks`, `instances.connect`, `onToggle`, `data` and `backup`

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`validate`), `scripts/lib/addon_store.py` (`listing`)
- Modify: `scripts/test-addon-manifests.py`, `scripts/test-addon-store.py`
- Modify: `scripts/fixtures/addons-cli/fixture/addon.json`: move `fixture_cache` to `data.volumes`; add a `oneshot` service `seed` (compose `fixture-seed`), `hooks.init: {"command": "seed-init"}` with a matching `commands.seed-init`, and `data.paths: [{"env": "FIXTURE_DATA_DIR", "default": "./data/fixture"}]`
- Modify: `addons/bitwarden/addon.json` (`hooks` to the `script` form)

**Interfaces:**
- Produces, as manifest fields, with the shapes from the spec section "Lifecycle fields for `addon.sh`":
  - `services.<k>.oneshot: true`: a service that runs to completion. It may not be the target of `mcp` or have `when`. Built-in only.
  - A hook is `{"command": <commands key>}` (any source; the command must take no instance) or `{"script": [argv]}` (built-in only). `script[0]` must match `^scripts/[a-z0-9-]+\.sh\Z` and exist in the repo; the other items are non-empty strings without line breaks. Exactly one key.
  - `hooks: {init?, upgrade?, backup?, restore?}`, each a hook. The validator now checks `hooks`, which it skipped before, so Bitwarden's `"backup": "scripts/vault.sh backup"` becomes `{"script": ["scripts/vault.sh", "backup"]}`, and `restore` likewise.
  - `instances.connect` and `instances.disconnect`: hooks, as a pair. `instances.errors`: a list of distinct names matching `^[a-z][a-z0-9_-]*\Z` that aren't shared names.
  - `onToggle: {"recreate": [<compose service>], "script"?: [argv]}`. Not in the spec yet; Task 10 asks for it to be added. `recreate` is a non-empty list of distinct compose names.
  - `data: {"paths"?: [{"env"?, "default"}], "volumes"?: [...]}`. Built-in only. `default` is `./data/<rest>` with no `..` segment, and must equal the source of some bind mount in `docker-compose.yml`, either literally or as `${<env>:-<default>}`. `env` is an env name. `volumes` items are distinct compose volume names. If both `data.volumes` and any `services.<k>.volumes` are present, that is an error; `services.<k>.volumes` alone is accepted and read as `data.volumes`.
  - `backup: {"secrets"?: [ENV names], "recommended"?: [ENV names]}`. Any source.
  - Unknown keys inside each block are errors, as for `gates`.
- Produces `addon_manifest.data_volumes(manifest) -> list[str]` (from `data.volumes` or the old field) and `addon_manifest.hook(manifest, name) -> dict | None` (`name` is `init`, `connect`, `disconnect` and so on, looked up in `hooks` or `instances`).
- `addon_store.listing()` adds `"data"`, `"backup"`, `"hooks"` (sorted names of the hooks present, `connect`/`disconnect` included) and `"errors"` (`instances.errors`, or `[]`).

- [ ] **Step 1: Write the failing tests** in `test-addon-manifests.py`, one case per rule: a manifest with every new field passes; `services.x.volumes` alone passes and `data_volumes` returns it; both forms together fail; `oneshot` on the MCP target fails; a hook with both `command` and `script`, a `script` outside `scripts/`, a hook as a bare string, a `command` naming an instance command, `connect` without `disconnect`, and an `instances.errors` entry `timeout` each fail; a `data.paths` default of `../x`, `/etc`, `./data/../x`, or one no bind mount in a fixture compose file uses, fails; `{"env": "lower", ...}` fails; `onToggle.recreate: []` fails; `backup.secrets: ["lower"]` fails. The real `addons/` must still pass. In `test-addon-store.py`: `listing()` carries `data`, `backup`, `hooks` and `errors` for the fixture.
- [ ] **Step 2: Run them and confirm the new cases fail**

Run: `python3 scripts/test-addon-manifests.py; python3 scripts/test-addon-store.py`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** the rules, `data_volumes`, `hook` and the `listing()` fields. Update the fixture as listed under Files, and make `plan-disable` read volumes through `data_volumes`. In `addons/bitwarden/addon.json`, convert both hooks to the `script` form, written with `canonical()`. Bitwarden gets no `onToggle`: since R13 its plugin is always installed and `reload-plugins` (Task 6) replaces the recreate.
- [ ] **Step 4: Run them and confirm they pass**

Run: `make addon-test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_manifest.py scripts/lib/addon_store.py scripts/lib/addon_cli.py scripts/test-addon-manifests.py scripts/test-addon-store.py scripts/fixtures/addons-cli addons/bitwarden/addon.json
git commit -m "Add oneshot, hooks, instance connect hooks, onToggle, data and backup to add-on manifests"
```

### Task 2: JSON lines, the lock and rollback for `enable` and `disable`

**Files:**
- Create: `scripts/lib/addon-events.sh`
- Modify: `scripts/addon.sh` (`enable`, `disable`, option parsing), `scripts/lib/addon_cli.py` (new `pull-events`, `log-tail`)
- Create: `scripts/fixtures/compose-progress-pull.jsonl`
- Modify: `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 1's manifest fields; plan 1's `plan-enable`/`plan-disable` output.
- Produces in `addon-events.sh` (sourced by `addon.sh`; every function is a no-op in human mode except `ev_fail`):
  - `ev_init JSON_MODE` where `JSON_MODE` is `0` or `1`
  - `json_str TEXT`: prints TEXT as a JSON string literal, applying the Global Constraints rules for control characters and length
  - `ev_step STEP STATUS [TARGET]`
  - `ev_done ID ACTION RESTART_CSV MCP_CSV [ACCOUNT]`
  - `ev_fail CODE STEP MESSAGE LOGFILE`: prints the `error` line (JSON mode) or `error: MESSAGE` (stderr), then exits with the code's exit status from Global Constraints. `LOGFILE` may be empty.
  - `run_step STEP TARGET CMD...`: runs CMD with stdout and stderr to a per-step log under `$EV_TMP` in JSON mode (to stderr in human mode), wrapped in `ev_step` running/done. Returns CMD's status and leaves the log path in `EV_LAST_LOG`.
- Produces in `addon_cli.py`:
  - `pull-events SERVICES_CSV`: reads `docker compose --progress json pull` output on stdin and prints `{"phase":"pull","service":S,"current":N,"total":N}` lines, summed over layers, at most one per service per second, then a final line per service with `"status":"done"`. Lines that aren't JSON are ignored.
  - `log-tail [--lines 20]`: reads a log on stdin and prints its last lines as a JSON array, each cleaned by the same rules as `json_str`. Task 5 adds `--redact FILE`.
- `addon.sh enable <id> [--json]` and `disable <id> [--purge] [--json]`. `--json` may come before or after the other options.
- The lock: mutating subcommands (`configure`, `enable`, `disable`, `connect`, `disconnect`, `remove`) create `$DATA_ROOT/addons/.lock` with `mkdir` and remove it in an `EXIT` trap. If it exists, fail with `busy`. A lock directory whose `pid` file names a process that no longer runs (`kill -0` fails) is taken over.
- Rollback: before writing flags, `enable` records each flag's current `.env` value. On a failure in `start`, `init` or `reload`, and on SIGINT or SIGTERM, it writes those values back, runs `sync-profiles.sh`, and runs `docker compose stop` on the add-on's services if the builtin flag was `0` before. The `error` line then has `"rolledBack":true`. This replaces plan 1's behaviour, where a failed `up` left the flags on.

- [ ] **Step 1: Write the failing tests** in `test-addon-cli.sh`. Add a JSON check helper that runs `python3 -c` over the output and asserts each line parses and the last line's `phase`. Cases:
  - `enable fixture --json` prints `setup` lines for `flags`, `start` and `reload`, then `done` with `"id":"fixture","action":"enable","restart":[]`. Stdout holds nothing that isn't JSON.
  - With the docker stub set to fail `compose up` (`DOCKER_FAIL_UP=1`, printing `boom "quoted" \ back` and a 10,000-character line), `enable --json` exits 1, the last line has `"error":"compose_failed","step":"start","rolledBack":true`, `logTail` contains the `boom` line, every line is at most 500 characters, and `.env` has `FIXTURE_ADDON_ENABLED=0` again.
  - `enable` without `--json` on the same failure prints `error:` on stderr and nothing on stdout.
  - A pre-made `data/addons/.lock` with this shell's PID in `pid` makes `enable` fail with `busy` and exit 1, leaving `.env` unchanged. The same lock with PID `999999` is taken over.
  - A docker stub that sleeps on `compose up`, sent SIGTERM from the test, leaves no lock and `FIXTURE_ADDON_ENABLED=0`.
  - `pull-events` over `scripts/fixtures/compose-progress-pull.jsonl` prints a `done` line for each service.
- [ ] **Step 2: Capture the pull fixture.** Run `docker compose --progress json pull` against a scratch compose file in the scratchpad holding one public image (`busybox:1.36`) and save its output, unchanged, as `scripts/fixtures/compose-progress-pull.jsonl`. If the output has no per-layer `current`/`total`, record that in the fixture's commit message and make `pull-events` print only `running` and `done` per service.
- [ ] **Step 3: Run the tests and confirm they fail**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 4: Implement** `addon-events.sh`, the lock, rollback and `--json` on `enable` and `disable`. In JSON mode, `enable` runs `docker compose --progress json pull <services>` first, piped through `cli pull-events`. A pull failure is `compose_failed` at step `pull`. Keep `hermes_reload` as it is for now; Task 6 replaces it.
- [ ] **Step 5: Run the tests and confirm they pass**

Run: `sh scripts/test-addon-cli.sh && python3 scripts/test-addon-store.py`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/lib/addon-events.sh scripts/addon.sh scripts/lib/addon_cli.py scripts/fixtures/compose-progress-pull.jsonl scripts/test-addon-cli.sh
git commit -m "Report enable and disable as JSON lines, lock add-on changes and roll back a failed enable"
```

### Task 3: One-shot services and which container failed

**Files:**
- Modify: `scripts/lib/addon_cli.py` (`plan-enable`), `scripts/addon.sh` (`enable`)
- Modify: `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 1's `oneshot`; Task 2's `run_step`, `ev_fail`.
- Produces: `plan-enable` prints `"oneshots": [...]` beside `"services"`, in manifest order, with the same `name@gate` form; one-shots are left out of `services`. `enable` runs each one-shot whose gate is on with `docker compose up --no-deps --exit-code-from <svc> <svc>` (step `start`, target the service), then `up -d --wait` on the rest.
- When `up -d --wait` fails, `enable` runs `docker compose ps -a --format json` on the add-on's services and one-shots. The first container, one-shots first and then manifest order, whose state is `exited` with a non-zero exit code, gives `target` and `logTail` (`docker compose logs --no-color --tail 20 <svc>`). If none exited non-zero, `target` is omitted and `logTail` comes from the `up` output.

- [ ] **Step 1: Write the failing tests.** Teach the docker stub to answer `compose ps -a --format json` from a file the test writes. Cases:
  - `enable fixture --json` logs `compose up --no-deps --exit-code-from fixture-seed fixture-seed` before `compose up -d --wait fixture-worker`, and `fixture-seed` is not in the `--wait` call.
  - With the one-shot exiting 1 (`DOCKER_FAIL_ONESHOT=1`), `enable --json` fails at step `start` with `target` `fixture-seed` and never calls `up -d --wait`.
  - With `up -d --wait` failing and `ps` reporting `fixture-seed` exited 1 and `fixture-worker` created but not started, the error names `fixture-seed` and its `logTail` comes from `compose logs --no-color --tail 20 fixture-seed`. This is n8n's case: `n8n-import` fails, and Docker reports the failure against `n8n`, which depends on it.
- [ ] **Step 2: Run and confirm they fail**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** the `oneshots` list and the failure lookup. Parse `ps --format json` in `addon_cli.py` (`failed-container SERVICES_CSV`, reading stdin, printing the service name or nothing), since compose prints either one JSON array or one object per line depending on version.
- [ ] **Step 4: Run and confirm they pass**

Run: `sh scripts/test-addon-cli.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_cli.py scripts/addon.sh scripts/test-addon-cli.sh
git commit -m "Run add-on one-shots before the wait and report the container that failed"
```

### Task 4: The `init` hook

**Files:**
- Modify: `scripts/lib/addon_cli.py` (`plan-hook`, `mark-init`), `scripts/lib/addon_store.py` (state file), `scripts/addon.sh` (`enable`, `--reinit`)
- Modify: `scripts/test-addon-store.py`, `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 1's `hook()`; Task 2's `run_step`; plan 1's `plan-run`.
- Produces:
  - `addon_store.state(manifest, data_root) -> dict` and `addon_store.set_state(manifest, data_root, key, value) -> None` on `data/addons/<id>/state.json` (mode `0600`, written through a temp file and `os.replace`). Init records `{"init": {"version": <manifest version>, "at": <UTC ISO 8601>}}`.
  - `plan-hook SID HOOK [--slug SLUG] [--reinit]` prints `script` and then the script's argv, or `service <compose name>` and then the command's argv, one item per line. It appends `--json`, and `--slug SLUG --json` for `connect` and `disconnect`. For `init` it prints nothing and exits 0 when `state.init` is recorded, unless `--reinit` is given.
  - `mark-init SID` records it.
- `enable <id> [--reinit] [--json]` runs `init` after `start` and before `reload`, as step `init`. A `command` hook runs with `docker compose run --rm --no-deps -T`; a `script` hook runs from `$ROOT` with stdin from `/dev/null`. In JSON mode, the hook's stdout lines that parse as JSON objects are printed inside the step, between its `running` and its `done` or `error` line, each with `"step":"init"` added; other lines go to the step log. A non-zero exit is `command_failed` unless the hook printed an `error` line, whose name is handled as in Global Constraints. If the hook's `done` line has a string `account`, `addon.sh`'s `done` carries it. `mark-init` runs only after exit 0, so a failed init runs again on the next `enable`.

- [ ] **Step 1: Write the failing tests.** `test-addon-store.py`: `set_state` then `state` round-trips; the file is `0600`. `test-addon-cli.sh`:
  - The first `enable --json` logs `compose run --rm --no-deps -T fixture-worker <seed-init argv> --json` and prints `init` running and done; a second `enable` doesn't run it; `enable --reinit` does.
  - With the hook failing (stub exit 5 on that `compose run`), `enable` fails `command_failed` at step `init`, rolls back, and the next `enable` runs the hook again.
  - A `script` hook in a second fixture (`scripts/fixtures/addons-cli/hooked/`, with `scripts/fixture-init.sh` copied into the scratch root) that prints `{"phase":"setup","status":"running","message":"creating budget"}` and then `{"phase":"done","account":"main"}` gives that first line inside the `init` step and a final `done` with `"account":"main"`.
- [ ] **Step 2: Run and confirm they fail**

Run: `python3 scripts/test-addon-store.py; sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** the state file, `plan-hook`, `mark-init`, `--reinit` and the `init` step.
- [ ] **Step 4: Run and confirm they pass**

Run: `make addon-test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_cli.py scripts/lib/addon_store.py scripts/addon.sh scripts/test-addon-store.py scripts/test-addon-cli.sh scripts/fixtures/addons-cli
git commit -m "Run an add-on's init hook once on enable and record it"
```

### Task 5: `connect` and `disconnect`

**Files:**
- Modify: `scripts/addon.sh` (new `cmd_connect`, `cmd_disconnect`, usage header), `scripts/lib/addon_cli.py` (`check-instance`, `log-tail --redact`)
- Modify: `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 4's `plan-hook`; plan 1's `configure --instance` and `remove-instance`; Task 2's emitter and lock.
- Produces:
  - `addon.sh connect <id> --instance <slug> [--json] < values.json`. Steps:
    1. Stage stdin to `$DATA_ROOT/addons/.staging.XXXXXX` (umask `077`), removed by the `EXIT` trap.
    2. `cli check-instance SID SLUG` validates the staged object against `instances.settings` without writing. Failure is `bad_input`.
    3. Step `check`, only when the manifest has `instances.connect`: run it (`plan-hook SID connect --slug SLUG`) with the staged file on stdin. The hook checks the credential, writes nothing on failure, and stores whatever it keeps itself. In JSON mode, relay the hook's JSON lines other than `done` and `error` inside the step. A hook `error` line becomes `addon.sh`'s `error`, named as in Global Constraints, at step `check`, with a `logTail` cleaned through `log-tail --redact STAGED`. A non-zero exit with no `error` line is `command_failed`. An existing slug is `exists` before the hook runs.
    4. Step `register`: `cli configure SID --instance SLUG` from the staged file, which writes the instance files the bootstraps read.
    5. Step `reload` (Task 6), then `done` with the hook's `account` and `mcp` when its `done` line has them.
  - Without `instances.connect`, `connect` skips step `check`: it is `configure --instance` plus `reload`, with no credential check.
  - `addon.sh disconnect <id> --instance <slug> [--json]`: step `check` runs `instances.disconnect`, if present, with `--slug SLUG --json` and stdin from `/dev/null`; then `register` runs `remove-instance`, then `reload`, then `done`. An unknown slug is `not_found` (exit 2).
  - `log-tail --redact FILE` replaces every occurrence of each `secret`-typed value in FILE's JSON object with `[redacted]`, longest value first.
  - The hook's line shape is the same as `addon.sh`'s, keyed on `phase`. Actual's `budget.sh connect` is the first hook; tell the Actual session before Step 3 if anything here changes.

- [ ] **Step 1: Write the failing tests** with the `hooked` fixture (`instances.connect: {"script": ["scripts/fixture-connect.sh"]}`, `instances.errors: ["encryption_password"]`), whose script checks it got `--slug work --json`, reads stdin and either prints `{"phase":"done","account":"work","mcp":["fixture-work"]}` or, when the host is `refuse.example.com`, prints its stdin and then `{"phase":"error","error":"login_refused","message":"refused"}` and exits 1. Cases:
  - A good connect prints `check`, `register` and `reload` steps and `done` with `"account":"work","mcp":["fixture-work"]`, and `instances/work.json` exists.
  - The refused connect exits 4 with `"error":"login_refused","step":"check"`, writes no instance file, and `logTail` contains `[redacted]` but not the password.
  - `{"bogus":1}` on stdin is `bad_input` before the hook runs (the stub log has no hook call). A second connect of `work` is `exists` with no hook call.
  - A hook error named `encryption_password` passes through; one named `weird` becomes `command_failed` with `weird` in `message`.
  - `connect` on the plain `fixture` (no hook) writes the instance and reports `register`, `reload` and `done` with no `check` step.
  - No `.staging.*` file is left after any case.
  - `disconnect` runs the hook with `--slug work --json`, removes `instances/work.json` and reports `done`; `disconnect` of an unknown slug is `not_found`, exit 2.
  - The secret-value scan at the end of the file covers every new output.
- [ ] **Step 2: Run and confirm they fail**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** `connect`, `disconnect`, `check-instance` and `--redact`, and add both subcommands to the usage header.
- [ ] **Step 4: Run and confirm they pass**

Run: `sh scripts/test-addon-cli.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/addon.sh scripts/lib/addon_cli.py scripts/test-addon-cli.sh scripts/fixtures/addons-cli
git commit -m "Add addon.sh connect and disconnect, which check a credential before storing it"
```

### Task 6: Reload without `make`, `onToggle`, and stopping unused `uses` services

**Files:**
- Modify: `scripts/addon.sh` (`hermes_reload` becomes `reload_hermes`, `disable`), `scripts/lib/addon_cli.py` (`plan-enable`, `plan-disable` print `onToggle`, `uses` and active `mcp` names)
- Modify: `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 1's `onToggle`; Task 2's `run_step`.
- Produces:
  - `reload_hermes`: step `reload` runs `docker compose run --rm -e HERMES_BOOTSTRAP_FORCE=1 hermes-api-bootstrap`, then `docker compose run --rm --no-deps -e HERMES_BOOTSTRAP_FORCE=1 hermes-browser-bootstrap`, then `./scripts/vault.sh reload-plugins` (R13, branch `fix/bwbroker-always-installed`: it sends Hermes' `reload-plugins` verb to profiles the bootstrap marked with `.addon-plugins-changed`, and is silent when none is marked or hermes is down). A bootstrap failure is `compose_failed`; a `reload-plugins` failure is `command_failed`. No `make`, no `up hermes`, and no `vault.sh follow-hermes` unless step `recreate` ran.
  - On `enable` and `disable` only, when the manifest has `onToggle` and the builtin flag changed value in this run: step `recreate` runs `docker compose up -d --no-deps --force-recreate <each recreate>`, then the `onToggle.script` argv from `$ROOT`, and `done` lists the recreated services in `restart`. `connect` and `disconnect` never recreate.
  - `done.mcp` on `enable` lists the `name` of each `mcp` entry whose gate is on; on `disable` it is `[]`.
  - `disable`, after the flags and `sync-profiles.sh`: for each name in the manifest's `uses`, if it isn't in `docker compose config --services` (which applies the active profiles), step `stop` with that target runs `docker compose stop <name>`. A service that no profile gates always appears in that list, so it is never stopped.

- [ ] **Step 1: Write the failing tests.** Replace the `make hermes-reload` assertions with: the stub log has both bootstrap runs with `HERMES_BOOTSTRAP_FORCE=1` followed by a `vault.sh reload-plugins` call (a stub `scripts/vault.sh` in the scratch root logs its argv), no `make` line, no `--force-recreate` and no `follow-hermes`. A stub `reload-plugins` exiting 1 makes `enable --json` fail `command_failed` at step `reload` and roll back. Add a `toggled` fixture with `onToggle: {"recreate": ["hermes"], "script": ["scripts/fixture-follow.sh", "follow"]}`: the first `enable` logs `compose up -d --no-deps --force-recreate hermes` and the script, and `done.restart` is `["hermes"]`; a second `enable` while enabled logs neither. For `uses`: give the fixture `"uses": ["neo4j"]`, make the stub answer `compose config --services` from a file, and check that `disable` stops `neo4j` when the list lacks it and leaves it when the list has it.
- [ ] **Step 2: Run and confirm they fail**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** the reload, `onToggle`, `done.mcp` and the `uses` stop. Delete `hermes_reload`.
- [ ] **Step 4: Run and confirm they pass**

Run: `make addon-test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/addon.sh scripts/lib/addon_cli.py scripts/test-addon-cli.sh scripts/fixtures/addons-cli
git commit -m "Reload Hermes with the bootstraps alone, recreate only for onToggle, and stop unused stack services on disable"
```

### Task 7: `remove --delete-data`

**Files:**
- Modify: `scripts/addon.sh` (new `cmd_remove`; `disable --purge` calls it), `scripts/lib/addon_cli.py` (`plan-remove`, replacing `purge`)
- Modify: `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: Task 1's `data_volumes` and `data.paths`; Task 3's one-shots; Task 2's emitter and lock.
- Produces:
  - `addon.sh remove <id> --delete-data [--json]`. Without `--delete-data` it is a usage error (exit 2). It refuses with `bad_input` while the builtin flag is `1`.
  - `plan-remove SID --env JSON` prints `{"services": [...], "volumes": [...], "delete": [...], "kept": [{"path", "reason"}]}`. `services` holds every service and one-shot. For each `data.paths` entry, the path is `env`'s value from `--env` (the caller's environment first, then `.env`) or else `default`, made absolute against the repo root and normalised without following symlinks (`os.path.normpath`, never `resolve()`). It goes to `delete` when it lies inside `<repo>/data` or `ASSISTANT_DATA_ROOT` and isn't one of those directories itself, and isn't declared by another add-on whose builtin flag is `1`. Otherwise it goes to `kept` with reason `outside the data directory`, `is the data directory` or `used by <short id>`. `data/addons/<id>` is always the last item of `delete`.
  - `remove` runs `docker compose rm -f -s` on `services` (step `delete`, no target), then `docker volume rm <project>_<vol>` for each volume that exists (step `delete`, target the volume), then deletes each `delete` path (step `delete`, target the path): a symlink is unlinked, never followed, and a directory is removed with `shutil.rmtree`, which doesn't follow symlinks inside it. Each `kept` item prints `{"phase":"setup","step":"delete","status":"done","kept":<path>,"reason":<reason>}`. Then `done` with `restart: []` and `mcp: []`.
  - `disable <id> --purge` runs `disable` and then `remove --delete-data`, in one lock, with one `done` line whose `action` is `disable`.

- [ ] **Step 1: Write the failing tests:**
  - `remove fixture --delete-data --json` on a disabled fixture with `FIXTURE_DATA_DIR` unset deletes `data/fixture` and `data/addons/fixture`, logs `compose rm -f -s fixture-seed fixture-worker fixture-extra` before `volume rm fixtureproj_fixture_cache`, and prints a `delete` step per volume and path.
  - With `FIXTURE_DATA_DIR=/` in `.env`, then `FIXTURE_DATA_DIR=..`, then `FIXTURE_DATA_DIR=./data`: exit 0, the path is reported as `kept` with its reason, and nothing outside `data/addons/fixture` is deleted.
  - With `data/fixture` a symlink to a directory outside the scratch root: the link is gone and the outside directory and its files still exist.
  - With a second enabled fixture declaring the same path, the path is `kept` with `used by <short id>`.
  - `remove` while enabled exits 2. `remove fixture` without `--delete-data` exits 2.
  - The plan 1 purge checks keep passing through `disable --purge`.
- [ ] **Step 2: Run and confirm they fail**

Run: `sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** `remove`, `plan-remove` and the new `--purge`. Delete the old `purge` subcommand from `addon_cli.py`.
- [ ] **Step 4: Run and confirm they pass**

Run: `sh scripts/test-addon-cli.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/addon.sh scripts/lib/addon_cli.py scripts/test-addon-cli.sh
git commit -m "Add addon.sh remove --delete-data for data paths and volumes"
```

### Task 8: Mount sources, gated-off services, volume ownership and the Linux user check

This is plan 2's former Task 5, moved here because it changes the same `enable` path. `data.volumes` replaces `services.<k>.volumes` in it.

**Files:**
- Modify: `scripts/lib/addon_store.py` (public `prepare`, `stored`, `slugs`), `scripts/lib/addon_cli.py` (`plan-enable`), `scripts/addon.sh` (`enable`)
- Modify: `scripts/test-addon-manifests.py`, `scripts/test-addon-store.py`, `scripts/test-addon-cli.sh`

**Interfaces:**
- Consumes: plan 1's `_mkdir_addon`, `_stored`, `_slugs`; Task 2's steps.
- Produces:
  - `addon_store.prepare(manifest: dict, data_root: Path) -> None` creates `config/`, `instances/` and `secrets/` under `addon_dir` at `0700` without touching existing files. `plan-enable` calls it after the missing-settings check when some service declares `mounts`. `_stored` and `_slugs` become public as `stored` and `slugs`.
  - `plan-enable` prints a `stop` list: the add-on's gated services and one-shots whose gate ends at `0`. `enable` runs `docker compose stop` on it after `start` (step `stop`). A failure is `compose_failed`.
  - `enable` refuses with `bad_input` and the message `HERMES_UID <n> is not your user id <m>; set HERMES_UID and HERMES_GID in .env` when `uname -s` is `Linux`, some service declares `mounts`, and `HERMES_UID` from `.env` (default `1000`) differs from `id -u`. The same applies to `HERMES_GID` and `id -g`. The check runs before any flag is written.
  - `test-addon-manifests.py`: each name from `data_volumes()` is mounted in `docker-compose.yml` only by that add-on's compose services.

- [ ] **Step 1: Write the failing tests.**
  - `test-addon-store.py`: `prepare` on an empty data root creates the three directories at `0700`; a second call leaves an existing `secrets/x` and its mode alone.
  - `test-addon-cli.sh`: `enable` before any `configure` leaves the three directories in place before the stubbed `compose up` runs (the stub appends `ls` of the data dir to its log on `up`). After `configure fixture {"extra": false}` on the enabled fixture, `enable` logs `compose stop fixture-extra`. `HERMES_UID=4242` in `.env` with `uname` stubbed to print `Linux` makes `enable --json` fail with `bad_input`, and `.env` keeps `FIXTURE_ADDON_ENABLED` unchanged.
  - `test-addon-manifests.py`: a compose snippet where a core service also mounts the add-on's volume is reported.
- [ ] **Step 2: Run and confirm they fail**

Run: `python3 scripts/test-addon-store.py; sh scripts/test-addon-cli.sh; python3 scripts/test-addon-manifests.py`
Expected: FAIL on the new checks only.

- [ ] **Step 3: Implement** the four changes.
- [ ] **Step 4: Run and confirm they pass**

Run: `make addon-test && sh scripts/test-no-host-python.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_store.py scripts/lib/addon_cli.py scripts/addon.sh scripts/test-addon-manifests.py scripts/test-addon-store.py scripts/test-addon-cli.sh
git commit -m "Prepare add-on mount sources, stop gated-off services and check volume ownership"
```

### Task 9: Per-service resources in `list --json`

**Files:**
- Modify: `scripts/addon.sh` (`cmd_list`), `scripts/lib/addon_cli.py` (`list`, new `images`), `scripts/lib/addon_store.py` (`listing` takes the figures)
- Create: `scripts/fixtures/compose-config.json`, `scripts/fixtures/manifest-inspect-verbose.json`
- Modify: `scripts/test-addon-cli.sh`, `scripts/test-addon-store.py`

**Interfaces:**
- Produces:
  - `addon.sh list --json [--sizes]`. It runs `docker compose --profile '*' config --format json` and passes the output to `cli list` on stdin. If that call fails, `list` still prints the catalog without the figures and writes one `warning:` line to stderr.
  - Each listing entry gains `"services": [{"key", "compose", "oneshot", "image", "memory": {"reservation", "limit"}, "downloadSize"?}]`. `image` comes from compose config. Memory figures are integer bytes taken from `deploy.resources.reservations.memory` and `deploy.resources.limits.memory`, falling back to `mem_reservation` and `mem_limit`, else `null`.
  - With `--sizes`, `cli images` prints the distinct images. For each, `addon.sh` writes `docker manifest inspect --verbose <image>` to `$DATA_ROOT/addons/.sizes.XXXXXX/<n>.json` and the ref to `<n>.image`, and passes the directory as `cli list --sizes-dir DIR`. `downloadSize` is the sum of layer sizes in the manifest for the server's platform (`docker version --format '{{.Server.Os}}/{{.Server.Arch}}'`), and is left out when the manifest or platform isn't found. The directory is removed on exit.
- [ ] **Step 1: Capture the fixtures.** Save `docker compose --profile '*' config --format json` for this repo, trimmed to the fixture's three compose services, as `scripts/fixtures/compose-config.json` with one service carrying `deploy.resources` and one `mem_limit`. Save `docker manifest inspect --verbose busybox:1.36` as `scripts/fixtures/manifest-inspect-verbose.json`. Neither holds secrets; check before committing.
- [ ] **Step 2: Write the failing tests:** the stub answers `compose ... config --format json` and `manifest inspect` from those fixtures. `list --json` carries the services with their memory figures and no `downloadSize`; `list --json --sizes` adds `downloadSize` for the matching platform; with `config` failing, `list --json` still exits 0 with a warning. `test-addon-store.py` covers the unit parsing (`"1g"` and `1073741824` both give `1073741824`).
- [ ] **Step 3: Run and confirm they fail**

Run: `sh scripts/test-addon-cli.sh; python3 scripts/test-addon-store.py`
Expected: FAIL on the new cases only.

- [ ] **Step 4: Implement** the figures and `--sizes`.
- [ ] **Step 5: Run and confirm they pass**

Run: `make addon-test`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/addon.sh scripts/lib/addon_cli.py scripts/lib/addon_store.py scripts/fixtures/compose-config.json scripts/fixtures/manifest-inspect-verbose.json scripts/test-addon-cli.sh scripts/test-addon-store.py
git commit -m "Report image, memory and download size per add-on service in addon.sh list"
```

### Task 10: Environment pass-through, docs and changelog

**Files:**
- Modify: `scripts/test-addon-cli.sh`
- Modify: `docs/extending.md` ("Add-ons: scripts/addon.sh"), `addons/README.md`, `CHANGELOG.md` (Unreleased)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the pass-through test.** The docker stub also logs `COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME COMPOSE_PROFILES=$COMPOSE_PROFILES` for each call. With `COMPOSE_PROJECT_NAME=callerproj` and `COMPOSE_PROFILES=core,fixture` exported, run `enable`, `connect`, `disconnect`, `disable --purge` and `list --json`. Every logged call, including both bootstraps, shows the exported values unchanged, apart from the `--profile '*'` argument on `list`'s `config` call. `volume rm` uses `callerproj_fixture_cache`.
- [ ] **Step 2: Run it.** If it passes at once, that confirms item 7 and the test stays as the guard. If it fails, fix `addon.sh` so it doesn't touch those variables.

Run: `sh scripts/test-addon-cli.sh`
Expected: exit 0.

- [ ] **Step 3: Update the docs.** In `docs/extending.md`, document transient instance secrets (Task 11), the JSON-lines contract (phases, step names, `done` and `error` fields, error codes and exit codes from Global Constraints), `connect`/`disconnect`, `remove --delete-data`, the lock, rollback, `list --json` figures and the new manifest fields. Say that `addon.sh` doesn't wait for MCP servers and that callers check `done.mcp` in `mcp.servers.status`. Update `addons/README.md`'s field list. Add `onToggle` and `services.<k>.oneshot` to `docs/private/addon-manifest-spec.md`'s lifecycle section only if its owner hasn't by then (it is gitignored; don't commit it). In `CHANGELOG.md` under Unreleased: the new subcommands and fields, `services.<k>.volumes` becoming `data.volumes` (still accepted in this patch), Bitwarden's hooks moving to the `script` form, the reload change (bootstraps plus `vault.sh reload-plugins`, no `hermes` recreate; plugin changes such as Bitwarden's tools reach new sessions only), and a line saying Boundary can move Actual onto `addon.sh` once Actual's manifest has `hooks.init`, `instances.connect` and `data`.
- [ ] **Step 4: Run the full checks once**

Run (in the background, through the test queue): `make addon-test && sh scripts/test-no-host-python.sh && python3 scripts/test-addon-manifests.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/test-addon-cli.sh docs/extending.md addons/README.md CHANGELOG.md
git commit -m "Test that addon.sh passes the compose project and profiles through, and document the add-on lifecycle"
```

## Out of scope

- Actual's manifest (`hooks.init: {"script": ["scripts/budget.sh", "init"]}`, `instances.connect`/`disconnect` with `budget.sh connect`/`disconnect`, `instances.errors`, `data.paths: [{"env": "ACTUAL_DATA_DIR", "default": "./data/actual/server"}]`, `data.volumes: ["actual_mcp_rw_cache", "actual_mcp_ro_cache"]`) belongs to the Actual session, which adds it once `budget.sh connect` exists.
- `mcp.instances` (one MCP entry per instance, for Twenty) and `crm.sh migrate`. They live in the bootstraps and `crm.sh`, not in `addon.sh`, and need an owner. The validator's `mcp.instances` refusal stays until that work lands.
- n8n's manifest and its `oneshot` `n8n-import` belong to the n8n plan (`docs/n8n-lightrag-addons-plan`).
- Twenty's manifest, which needs `mcp.instances`.
- Keith took R13 (2026-11-09): the Bitwarden plugin is always installed, on branch `fix/bwbroker-always-installed`. Plan 3's reload calls `vault.sh reload-plugins` from that branch, so that branch merges to `main` before this one. `onToggle` stays as a generic field with no built-in user.
- A live run. Before tagging 0.3.13, Keith runs `scripts/addon.sh enable gptr --json` and `disable gptr --json` on his stack; it runs the bootstraps but recreates nothing.

### Task 11: Transient instance secrets

Added 2026-11-09 at the Actual session's request, with Keith's go-ahead. Runs after Task 9 and before Task 10, so the docs cover it. Boundary sends the Actual password on `connect`'s stdin; `budget.sh connect` signs in with it and stores only the session token it gets back (CR 0026), so `addon.sh` must not store the password.

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`validate`), `scripts/lib/addon_store.py` (`apply_instance`, `missing`, `listing`)
- Modify: `scripts/test-addon-manifests.py`, `scripts/test-addon-store.py`, `scripts/test-addon-cli.sh`, `scripts/fixtures/addons-cli/hooked/addon.json`

**Interfaces:**
- Manifest: `"transient": true` is allowed on an `instances.settings` entry of type `secret`, in a built-in manifest that has `instances.connect`. It is an error on any other type, outside `instances.settings`, or without a connect hook.
- `check_instance` validates a transient setting like any other secret, CR/LF refusal included.
- The connect hook gets it on stdin unchanged, and redaction (`cli redact`, `log-tail --redact`) covers it as a secret.
- `apply_instance` (step `register`, and `configure --instance`) never writes it: no `secrets/<slug>.<name>` file and no key in `instances/<slug>.json`.
- `missing()` never reports a transient setting. `listing()` shows it in the instance schema with `"transient": true`, and never as set or unset in an instance view.

- [ ] **Step 1: Write the failing tests.** Validator: `transient` on a string setting, on a top-level setting, and in a manifest without `instances.connect` each fail; on an instance secret with a connect hook it passes. Store: `apply_instance` with a transient `password` writes neither `secrets/<slug>.password` nor a `password` key; `missing()` skips it; `listing()` carries `"transient": true` in the schema. CLI, with the `hooked` fixture's `password` made transient: a good connect passes the password to the hook (the fixture script checks it on stdin) and leaves no `secrets/work.password`; the end-of-file secret scan covers the new outputs.
- [ ] **Step 2: Run and confirm they fail**

Run: `python3 scripts/test-addon-manifests.py; python3 scripts/test-addon-store.py; sh scripts/test-addon-cli.sh`
Expected: FAIL on the new cases only.

- [ ] **Step 3: Implement** the validator rule and the store changes.
- [ ] **Step 4: Run and confirm they pass**

Run: `make addon-test`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_manifest.py scripts/lib/addon_store.py scripts/test-addon-manifests.py scripts/test-addon-store.py scripts/test-addon-cli.sh scripts/fixtures/addons-cli
git commit -m "Let an instance secret be transient: passed to the connect hook, never stored"
```
