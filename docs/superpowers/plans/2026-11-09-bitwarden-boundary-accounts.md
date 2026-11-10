# Bitwarden accounts from Boundary (CR 0028) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boundary can connect, list, unlock, lock, sync and disconnect Bitwarden accounts
through `addon.sh` and `vault.sh` with JSON on stdin and stdout, and list logins the agent
returned that stayed in its folder.

**Architecture:** The broker gains a sign-in at start for manual accounts, two fields on
`GET /accounts`, and an `unreturned` list on `GET /requests`. `vault.sh` gains a `--json` mode
for `add`, `remove`, `status`, `unlock`, `lock` and `sync`, using budget.sh's event lines.
`addon.sh` relays an optional `retryAfter` from a hook's error line. The Bitwarden manifest gets
an `instances` block whose hooks are `vault.sh add` and `remove`. Terminal `add` and `remove`
and `make up` keep the add-on store's instance list in step with the account files.

**Tech Stack:** bash 3.2-compatible shell (macOS host), Python 3 (broker, addon_cli), Docker
Compose.

**Spec:** `docs/private/cr-0028-stack-reply.md` (main checkout, gitignored), which answers
boundary-desktop `docs/change-requests/0028-bitwarden-accounts-from-boundary.md` at 3ccc5829.
The reply is the authority. Where this plan corrects it, the ruling below says so.

## Rulings made while planning

- **The start sign-in doesn't catch a key revoked after connect.** `Bw._login` treats "already
  logged in" as success (`bwcli.py:295`). `bw/<slug>` keeps the login across restarts, so a later
  start makes no server call. Forcing `bw logout` then `login` at every start would catch it.
  It may also trigger Bitwarden's new-device sign-in email at each restart, and we can't check
  that without a real account. So the start sign-in only gives a manual account its first real
  key check, and connect's check is real because `add --json` starts from an empty `bw/<slug>`.
  A key revoked later shows at the next unlock, as today. Tell Boundary in the reply (Task 9).
  Cost if wrong: R2 shows a revoked manual key only after an unlock attempt.
- **No `prepare()` conflict.** The add-on store's `_mkdir` sets 0700, but `store.prepare` runs
  only for services with `mounts`, and the broker has none. `apply_instance` touches only
  `data/addons/bitwarden`, `config/` and `instances/`. The broker's bind mounts don't need
  the parent folder to be traversable. `secrets/` keeps vault.sh's 0711. Task 7 asserts the
  mode stays.
- **The JSON helpers are copied from budget.sh into vault.sh, not shared.** Two scripts don't
  justify a library. If a third appears, extract them.
- **`connect` keeps plan 3's reload step**, although Bitwarden needs no Hermes reload. Skipping it
  would need a manifest switch that no other add-on uses.

## Global Constraints

- No secret (`client_id`, `client_secret`, `master_password`) on any argv, stdout line or log.
  Values reach Python on stdin and come back as `eval`'d shell-quoted assignments (budget.sh's
  `read_input`). `client_id` counts as a secret here even though the account file holds it.
- In `--json` mode stdout carries only `{"phase": …}` lines (or, for `status`, `sync` and
  `requests`, the documented objects). Everything else goes to stderr.
- JSON-mode exit codes follow budget.sh's `emit_error`: 2 for `bad_input`, `bad_slug`, `exists`
  and `not_found`, 4 for every other error, 0 for done.
- `--json` mode never prompts, never offers bitwarden-setup and never writes `.env`.
- bash 3.2: regexes in variables, no `${x,,}`, no associative arrays (as in vault.sh today).
- Error names: `login_refused` (broker `auth`), `server_unreachable` (`network`),
  `rate_limited`, `timeout`, `broker_down`, `command_failed` (`unknown` in connect), and
  `bad_input`, `exists`, `not_found`. In status lines the broker's `unknown` stays `unknown` and
  `locked` becomes null.
- Terminal (non-JSON) behavior and output of every vault.sh command stay as they are, except the
  store sync added in Task 7.
- Tests: run only the affected test files while iterating. `make bitwarden-test`, `make
  addon-test` and the CI set run once through `~/.claude/bin/testq` in the background before the
  merge.
- Commits as Keith's identity, with no Co-Authored-By trailer. Prose follows the writing-style
  rules.

## Review Focus

1. **A failed connect leaves a half-written account.** Each failure after the files are written
   must remove the account file, `secrets/<slug>/` and `bw/<slug>/`, and restart the broker.
   The failures are `login_refused`, `server_unreachable`, `rate_limited`, `timeout`,
   `command_failed`, and SIGTERM from `addon.sh` in the middle of the check. Task 6 tests each.
2. **A secret reaches an argv.** A test runs `ps -ax -o args=` from the docker stub while
   `add --json` and `unlock --json` run and greps for the stub values. Tasks 5 and 6 own it.
3. **The broker is down or mid-restart while Boundary calls `status`.** Every account gets
   `offline`/`broker_down`, the exit code is 0, and no stderr text lands on stdout. Task 4.
4. **The release record and the denylist disagree.** A handle on `unreturned.json` must always
   be on the denylist too. Pruning drops both, and a regrant (`test_regrant_clears_denylist`)
   drops the record. Task 2.
5. **The store sync runs while an `addon.sh` call holds the lock.** Terminal `add`, run by
   `addon.sh connect`, must not call `addon.sh configure` (busy). Only non-JSON mode syncs.
   Task 7 tests that a JSON add never calls it.

---

### Task 1: Broker signs in manual accounts at start; `/accounts` gains `logged_in` and `retry_after`

**Files:**
- Modify: `compose/bitwarden-broker/broker/server.py` (`start`, `get_accounts`)
- Modify: `compose/bitwarden-broker/broker/bwcli.py` (a `logged_in` property, a `retry_after()` method)
- Modify: `compose/bitwarden-broker/broker/cli.py` (`cmd_accounts` appends two columns)
- Test: `scripts/test-bitwarden-broker.py`

**Interfaces:**
- Produces: `GET /accounts` rows `{slug, mode, scope, state, error_class, logged_in: bool,
  retry_after: int | null}`. `retry_after` is the whole seconds left until `Bw.retry_at`, at
  least 1, else null. `broker.cli accounts` TSV columns: `slug mode scope state error_class
  logged_in retry_after`. The last two are appended, so vault.sh's existing `$4 " " $5` awk
  keeps working. `logged_in` prints as `1` or `0`, `retry_after` as a number or empty.

- [ ] **Step 1: Write failing tests** in `test-bitwarden-broker.py`:
  - `test_manual_login_at_start`: a manual account with the stub bw accepting `login --apikey`
    shows `state == "locked"`, `error_class == ""` and `logged_in is True` once the start
    threads finish.
  - `test_manual_login_refused_at_start`: the stub bw refuses login with Bitwarden's
    invalid-client text. The account shows `state == "error"`, `error_class == "auth"` and
    `logged_in is False`.
  - `test_accounts_retry_after`: after the existing 429 stub with `Retry-After: 120`, the row
    has `30 < retry_after <= 120`, and null once `retry_at` has passed.
  - Extend `test_cli_for_vault_sh` to assert the seven-column TSV.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-broker.py` and confirm the new tests fail.
- [ ] **Step 3: Implement.** `start()` adds one `login-<slug>` thread per manual account calling
  `bw.login()` and catching `BwError`, as the stored threads do. Check that a failed `_login`
  goes through `_fail`, so the state is `error`/`auth` or `offline`. If it doesn't, route it
  there.
- [ ] **Step 4: Run** the file again; all pass. Also run `python3 scripts/test-bitwarden-access.py`
  and `python3 scripts/test-bitwarden-fill.py` (they share the broker).
- [ ] **Step 5: Commit** "Broker: sign in manual accounts at start; /accounts reports logged_in and retry_after".

### Task 2: Broker records logins Bitwarden wouldn't take back (R6)

**Files:**
- Modify: `compose/bitwarden-broker/broker/vault.py` (new `Unreturned` store next to `Denylist`)
- Modify: `compose/bitwarden-broker/broker/server.py` (`__init__`, `post_release`, `_prune`, `get_requests`, regrant path)
- Modify: `compose/bitwarden-broker/broker/cli.py` (human `requests` output lists them)
- Test: `scripts/test-bitwarden-access.py`

**Interfaces:**
- Produces: `broker/unreturned.json`, a JSON list of `{"handle", "account", "label", "site"}`,
  mode 600, written atomically like `Denylist._save`. A corrupt file raises at start, as the
  denylist does. `GET /requests` returns `{"pending", "recent", "unreturned": [{account, label,
  site}]}`, sorted by account and then label, without `handle`.

- [ ] **Step 1: Write failing tests** in `test-bitwarden-access.py`:
  - `test_unreturned_listed_after_refused_move`: a folder-scope account and a stub bw whose
    `edit item` fails. After `POST /release`, `GET /requests` has one `unreturned` entry with the
    item's name as `label` and its first fill origin's host as `site`.
  - `test_unreturned_cleared_by_sync`: the stub's next listing omits the item. After `POST /sync`
    the entry is gone from `unreturned` and from `denylist.json`.
  - `test_unreturned_survives_lock_and_restart`: the entry keeps its label and site after
    `POST /lock` and after a broker restart.
  - `test_unreturned_skips_returned_and_scope_all`: a release whose edit succeeds but whose
    refresh fails is not listed, and neither is a release on a `scope: all` account.
  - `test_regrant_clears_unreturned`: extends `test_regrant_clears_denylist`. A regrant drops
    the record too.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-access.py` and confirm they fail.
- [ ] **Step 3: Implement.** Take the label and site from `self.vault.get(handle)` before the
  denylist add. The site is `urlsplit(meta.origins[0]).hostname`. Record only when
  `return_item` returned false and the scope kind is folder or collection. `_prune` drops a
  record whose denylist entry it drops. The human `requests` output adds a section listing them
  after the pending requests.
- [ ] **Step 4: Run** the access, broker and fill tests; all pass.
- [ ] **Step 5: Commit** "Broker: list logins Bitwarden refused to move out of the agent's scope".

### Task 3: `addon.sh` relays `retryAfter` from a hook's error line

**Files:**
- Modify: `scripts/lib/addon_cli.py` (`cmd_hook_result` prints a sixth line)
- Modify: `scripts/addon.sh` (`run_hook` reads it into `EV_RETRY_AFTER`; header comment)
- Modify: `scripts/lib/addon-events.sh` (`ev_error` adds `"retryAfter":N`)
- Modify: `scripts/fixture-connect.sh` (a mode that prints an error line with `retryAfter`)
- Test: `scripts/test-addon-cli.sh`

**Interfaces:**
- Produces: an `addon.sh` error line `{"phase":"error","error":…,"retryAfter":120,…}` when the
  hook's error line had a positive integer `retryAfter`. The field is absent otherwise, and for
  non-integer, boolean or ≤0 values. Any add-on may use it.

- [ ] **Step 1: Write failing tests** in `test-addon-cli.sh`, beside the existing connect cases:
  a hook error with `"retryAfter":120` gives an `addon.sh` error line with `"retryAfter":120`.
  `"retryAfter":"soon"` and `true` give none.
- [ ] **Step 2: Run** `bash scripts/test-addon-cli.sh`; confirm they fail.
- [ ] **Step 3: Implement** the sixth line (empty when absent or invalid), `EV_RETRY_AFTER` set
  only from it, and the field in `ev_error` after `target`.
- [ ] **Step 4: Run** `bash scripts/test-addon-cli.sh` and `python3 scripts/test-addon-manifests.py`; all pass.
- [ ] **Step 5: Commit** "addon.sh: relay a hook's retryAfter on the error line".

### Task 4: `vault.sh` JSON helpers and `status --json` (R2)

**Files:**
- Modify: `scripts/vault.sh`: the `emit_line`, `emit_done`, `emit_error`, `json_str`, `json_escape`
  and `read_input` helpers copied from budget.sh (`read_input` taking the key list as arguments),
  `source lib/python.sh`, and `cmd_status`
- Test: `scripts/test-vault-sh.sh` (docker stub gains `/accounts` TSV with seven columns and refused rows)

**Interfaces:**
- Produces: `vault.sh status --json`, one line per `*.env` in the accounts folder, sorted by
  slug: `{"slug","mode","scope","unlock","state","error","retry_after"}`. Broker `error_class`
  maps to `error` like this: `auth` → `login_refused`, `network` → `server_unreachable`,
  `rate_limited` → `rate_limited`, `unknown` → `unknown`, and `locked` or empty → null. A refused
  file is `{"file","slug","state":"refused","error":"bad_file"}`, where `slug` is the file stem
  when it matches `SLUG_RE`, else null. With the broker down, every file gets
  `"state":"offline","error":"broker_down","retry_after":null`. Exit 0 in all of these.
  `status` without `--json` is a usage error for now.
- Consumes: Task 1's TSV columns.

- [ ] **Step 1: Write failing tests:**
  - one stored account unlocked and one manual locked
  - an `auth` row mapping to `login_refused`
  - a refused file
  - broker down (stub `ps` empty) giving `broker_down` lines and exit 0
  - no accounts, giving no output and exit 0
  - stdout parsing as JSON lines in every case
- [ ] **Step 2: Run** `bash scripts/test-vault-sh.sh`; confirm they fail.
- [ ] **Step 3: Implement.** Read `mode`, `scope` and `unlock` with `load_file`/`text_get`.
  A `BW_MODE=local` file reports `"mode":"local"`.
- [ ] **Step 4: Run** `bash scripts/test-vault-sh.sh`; all pass.
- [ ] **Step 5: Commit** "vault.sh status --json: one line per account for Boundary".

### Task 5: `unlock --json`, `lock --json` and `sync --json` (R3, R4)

**Files:**
- Modify: `scripts/vault.sh` (`cmd_unlock`, `cmd_lock`, `cmd_sync`)
- Modify: `compose/bitwarden-broker/broker/cli.py` (`sync --json` prints the reply)
- Test: `scripts/test-vault-sh.sh`, `scripts/test-bitwarden-broker.py` (`test_cli_for_vault_sh`)

**Interfaces:**
- Produces:
  - `unlock --slug S --json` reads `{"master_password"}` from stdin and makes one attempt.
    It ends with `done` or an error. `broker.unlock` exit 2 is `login_refused`, exit 3 is
    `not_found`, and anything else is `command_failed`. A stored account or bad JSON is
    `bad_input`, and a missing account file is `not_found`.
  - `lock --slug S --json` ends with `done` or an error from the same set, without
    `login_refused`.
  - `broker_down` comes before any broker call.
  - `sync --json` prints `POST /sync`'s reply as one line. With the broker down it exits 1 with
    stderr only, as `requests --json` does.

- [ ] **Step 1: Write failing tests:**
  - unlock success, wrong password, stored account, missing file and broker down
  - lock success and stored account
  - sync's four reply shapes, passed through, and broker down
  - an argv check: the docker stub runs `ps -ax -o args=` during unlock and the test greps it
    for the stub password
- [ ] **Step 2: Run** `bash scripts/test-vault-sh.sh` and `python3 scripts/test-bitwarden-broker.py`; confirm the new cases fail.
- [ ] **Step 3: Implement.** The password goes from `read_input` to `broker.unlock`'s stdin with
  `printf '%s\n'`, as today.
- [ ] **Step 4: Run** both test files; all pass.
- [ ] **Step 5: Commit** "vault.sh unlock, lock and sync --json for Boundary".

### Task 6: `add --json` and `remove --json`, the connect and disconnect hooks (R1)

**Files:**
- Modify: `scripts/vault.sh` (`cmd_add`, `check_account`, `wait_for_account`, `cmd_remove`, `note_locked_manual`)
- Test: `scripts/test-vault-sh.sh`

**Interfaces:**
- Consumes: Task 1's `logged_in` column, Task 3's `retryAfter` relay, Task 4's helpers.
- Produces:
  - `vault.sh add --slug S --json` reads `{mode, region?, url?, scope?, unlock?, client_id,
    client_secret, master_password?}` on stdin. `scope` defaults to `folder:Agent` and `unlock`
    to `manual`. It prints `{"phase":"setup","step":"write"…}` and `{"phase":"setup",
    "step":"check"…}` lines, then `{"phase":"done","account":S,"message":…}`. The message names
    the manual accounts the restart locked, if any.
  - Errors: `bad_input` (validation, `scope: all`, slug `local`, `--force`), `exists` (account
    file present), `broker_down`, `login_refused`, `server_unreachable`, `rate_limited` (with
    `retryAfter` from the `retry_after` column when there is one), `timeout`, and
    `command_failed`.
  - `vault.sh remove --slug S --json` prints `done` with the revoke-the-key reminder as
    `message`. `local` is `bad_input` and a missing file is `not_found`.

- [ ] **Step 1: Write failing tests:**
  - a cloud/manual add with a stub broker that reports `logged_in=1`, ending with `done` and
    writing the file and `secrets/S/`
  - a manual add whose login is refused, ending with `login_refused` and leaving nothing behind
  - stored adds ending in `network`, `rate_limited` (`retry_after` 120, giving `retryAfter` 120)
    and the wait expiring (`timeout`), each leaving nothing behind
  - `scope: all` and `--force` refused
  - broker down, giving `broker_down` with nothing written and `.env` unchanged
  - a leftover `bw/S/` emptied before the write
  - SIGTERM during the check leaving nothing behind
  - stdout holding only JSON lines
  - `ps` showing no secret
  - remove: done, local refused and missing file
  - the restart message naming another locked manual account
- [ ] **Step 2: Run** `bash scripts/test-vault-sh.sh`; confirm they fail.
- [ ] **Step 3: Implement.**
  - Branch at the top of `cmd_add` on `--json`. The JSON path builds the same variables from
    stdin and then shares the write code.
  - `log` goes to stderr in JSON mode.
  - `check_account` in JSON mode treats every outcome except `unlocked` (stored) or `locked`
    with `logged_in=1` and no error (manual) as a failure that rolls back. `wait_for_account`
    for manual waits for `logged_in=1` or a non-locked state.
  - The existing EXIT trap covers SIGTERM. Extend it to the post-write stage in JSON mode.
- [ ] **Step 4: Run** `bash scripts/test-vault-sh.sh`; all pass.
- [ ] **Step 5: Commit** "vault.sh add and remove --json: the Bitwarden connect and disconnect hooks".

### Task 7: The manifest's `instances`, the store sync and the end-to-end connect test

**Files:**
- Modify: `addons/bitwarden/addon.json`: `instances` with `name: "account"`, `optional: true`,
  the settings table from the reply (with `unlock` defaulting to `manual` and `scope` to
  `folder:Agent`), `connect`/`disconnect` script hooks, and `errors: ["rate_limited",
  "broker_down"]`. Keep the canonical layout (`canonical()`).
- Modify: `scripts/vault.sh`:
  - terminal `add` (after success) and terminal `remove` call `scripts/addon.sh configure bitwarden
    --instance S` (non-secret values on stdin) or `--remove`. A failure logs a warning and
    doesn't fail the command.
  - `cmd_up_hook` registers each account file without a store entry, except `local`.
  - JSON mode never calls `addon.sh`.
- Create: `scripts/test-bitwarden-connect.sh`, running the real `addon.sh connect bitwarden
  --instance home --json` and `disconnect` with `STACK_PYTHON=host`, a docker stub that answers
  the reload step and the broker, and temp dirs. Model the stubs on `test-addon-cli.sh`.
- Modify: `Makefile` (`BITWARDEN_TESTS` gains the new test), `scripts/test-addon-manifests.py`
  (Bitwarden's instances block validates; the transient secrets aren't in the store).

- [ ] **Step 1: Write the failing end-to-end test:**
  - connect ends with `done` and `"account":"home"`
  - `data/addons/bitwarden/instances/home.json` holds no `client_id`, `client_secret` or
    `master_password`, and no `secrets/home.*` file exists
  - the mode of `secrets/` is unchanged (0711)
  - a refused key gives `login_refused` and no store entry
  - a rate-limited connect's error line has `retryAfter`
  - disconnect removes both the account and the store entry
  - a terminal-mode add with a stub `addon.sh` is called with `configure`, and a JSON add isn't
- [ ] **Step 2: Run** `bash scripts/test-bitwarden-connect.sh` and `python3 scripts/test-addon-manifests.py`; confirm failures.
- [ ] **Step 3: Implement** the manifest and the sync.
- [ ] **Step 4: Run** both, plus `bash scripts/test-vault-sh.sh`; all pass.
- [ ] **Step 5: Commit** "Bitwarden accounts as addon.sh instances; keep the store in step with terminal adds".

### Task 8: Docs and changelog (R5)

**Files:**
- Modify: `docs/bitwarden.md`:
  - a section for Boundary listing `status --json`, `unlock`/`lock --json` and `sync --json`
  - `requests --json`'s contract fields: `pending[].request_id`, `account`, `site`, `reason`,
    `created` (Unix seconds), and `unreturned[].account`, `label`, `site`
  - the start sign-in for manual accounts, and that a key revoked later shows at the next unlock
  - connect and disconnect locking the other manual accounts
  - `unreturned.json`
- Modify: `scripts/vault.sh` `usage()` for the `--json` forms and `status`.
- Modify: `CHANGELOG.md` Unreleased: one Changes entry per user-visible change, and an
  upgrade note that terminal-added accounts get a store entry at the next `make up`.

- [ ] **Step 1: Write the docs**, checking each claim against the code from Tasks 1–7.
- [ ] **Step 2: Run** `make bitwarden-test` and `make addon-test` through
  `~/.claude/bin/testq` in the background, then the CI set once. Everything passes.
- [ ] **Step 3: Commit** "Document the Bitwarden JSON commands Boundary calls".

### Task 9: Tell Boundary what differs from the reply

Not code. Update `docs/private/cr-0028-stack-reply.md` in the main checkout:
- The start sign-in ruling: R2 shows a manual account's revoked key only after an unlock attempt
  once it has signed in before.
- Status lines carry `retry_after`.

Then send the Boundary add-ons session a short note pointing at the changes.
