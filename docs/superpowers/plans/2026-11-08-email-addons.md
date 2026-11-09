# Email add-ons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe today's Mail.app setup as the `email-local` add-on with no behaviour change, and add the `email` add-on, which provides several IMAP accounts with read and drafts and runs entirely in containers, so Boundary can install it with `scripts/addon.sh`.

**Architecture:** `email-local` is a manifest over the existing `mail-mcp` and `mail-drafts-mcp` services. It replaces the hand-written mail registration in the bootstraps. `email` adds four compose services. `email-indexer` and `email-outbox` share one new image built from the existing indexer and drafts code, hold the passwords, and sit only on an egress network. `email-mcp` and `email-drafts-mcp` reuse the existing images on a new internal network shared with Hermes. A small entrypoint renders the indexer config from the add-on's instance files on each pass.

**Tech Stack:** Python 3.11 standard library, POSIX sh, Docker Compose, GreenMail `greenmail/standalone:2.1.3` for tests.

**Spec:** `docs/superpowers/specs/2026-11-08-email-addons-design.md`. **Depends on:** `docs/superpowers/plans/2026-11-08-addon-manifest-path.md`, merged first.

## Global Constraints

- Follow `~/.claude/rules/email-privacy.md`. Use synthetic mail and GreenMail only, never a real account. Real runs are Keith's. The mail hook blocks commands that name mail stores or Mail's metadata database; don't name them, and report any block.
- `email-local` changes nothing an install sets: `MAIL_MCP_ENABLED`, `MAIL_DRAFTS_ENABLED`, profiles `mail` and `mail-drafts`, MCP names `mail` and `drafts`, `MAIL_MCP_URL`/`MAIL_DRAFTS_MCP_URL` and their timeout overrides, `MAIL_INDEX_DIR`, `MAIL_DRAFTS_DIR`, `setup.sh --mail`/`--mail-drafts`, `make mail-triage-cron`.
- `email` names: id `io.github.kaxline.stack.email`, flag `EMAIL_ENABLED` (profile `email`), gate `EMAIL_DRAFTS_ENABLED` (profile `email-drafts`), MCP names `email` and `email-drafts`, named volume `email_index`, data directory `data/addons/email/`.
- Neither add-on registers on `api-server`. Both have `permissions.cron: false` and `reads: ["mail"]`.
- Container hardening on all four `email` services: `read_only: true`, `cap_drop: ["ALL"]`, `security_opt: ["no-new-privileges:true"]`, `tmpfs: ["/tmp"]`, `user: "${HERMES_UID:-1000}:${HERMES_GID:-1000}"`, no host ports.
- Password files: `data/addons/email/secrets/<slug>.password`, mounted read-only at `/run/addon/secrets` into `email-indexer` and `email-outbox` only.
- Commit messages and docs must not name Mail's metadata database (the hook blocks them).

## Review Focus

- An instance removed with `configure --remove` must disappear from the next index pass's config, and its messages must stop showing in search.
- A password the server refuses must log `mail-index:` with the account id and exit code 4 in the pass result, never the password. The loop must go on to the next pass and the other accounts.
- `drafts` turned off while drafts are queued: `email-outbox` stops, and the queue stays on disk for when it's turned back on.
- No instances configured yet but `EMAIL_ENABLED=1` (a hand-edited `.env`): `email-indexer` writes no index and logs one line per pass. `email-mcp` reports an empty `mail_status` instead of crashing.
- An instance with `security = "plain"` and a public host must be refused at render time with the instance slug named, for any host except `host.docker.internal`, `localhost`, `127.0.0.1` and the test server name `greenmail`.

---

### Task 1: Passwords from `MAIL_CREDENTIALS_DIR`

**Files:**
- Modify: `scripts/lib/mail_credentials.py`
- Test: `scripts/test-mail-imap.py` (new `test_credentials_dir`, run from `main()` beside `test_credentials_handoff`)

**Interfaces:**
- Produces: `password(account_id: str, password_env: str | None) -> str` checks, in order: `password_env`, then the launcher's file descriptor, then `MAIL_CREDENTIALS_DIR/<account_id>.password`. The file is read as UTF-8 and returned exactly, with no newline stripping. Constant `DIR_VARIABLE = "MAIL_CREDENTIALS_DIR"`. The `MissingPassword` message for the directory case names the account id and the variable, never a path inside it, and never a value.

- [ ] **Step 1: Write the failing test** `test_credentials_dir`. In a temp dir, write `work.password` containing `"pw with spaces\n"`, set `MAIL_CREDENTIALS_DIR`, and assert `password("work", None) == "pw with spaces\n"`. Assert `password("other", None)` raises `MissingPassword` and its message contains `MAIL_CREDENTIALS_DIR` but not the temp path. With `password_env` set, the env value wins.
- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 scripts/test-mail-imap.py`
Expected: FAIL in `test_credentials_dir` (the GreenMail parts skip without Docker).

- [ ] **Step 3: Implement** the directory source in `password()` and update the module docstring.
- [ ] **Step 4: Run it and confirm it passes**

Run: `python3 scripts/test-mail-imap.py`
Expected: the `test_credentials_dir` checks pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/mail_credentials.py scripts/test-mail-imap.py
git commit -m "Read IMAP passwords from MAIL_CREDENTIALS_DIR"
```

### Task 2: Indexer loop and per-account mailbox listing

**Files:**
- Modify: `scripts/mail-index.py`
- Test: `scripts/test-mail-indexer.py`

**Interfaces:**
- Produces:
  - `mail-index.py run --every SECONDS [--passes N]` repeats `run` every SECONDS, until N passes if `--passes` is given (for tests). Each pass prints its result JSON as one line on stdout. A pass that hits exit code 4 or 5 prints `{"error": "<code>"}` plus the `mail-index:` line on stderr, then waits and continues. A config error (2) still exits.
  - `mail-index.py mailboxes --account ID` lists only that account. An unknown id exits 2 with `mail-index: config: no account <id>`.

- [ ] **Step 1: Write the failing tests** against the synthetic store config the file already builds:
  - `mailboxes --account <first synthetic account id>` lists only that account.
  - `mailboxes --account nope` exits 2.
  - `run --every 1 --passes 2` prints two JSON lines; the second shows nothing new fetched.
- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-mail-indexer.py`
Expected: FAIL, unrecognized arguments `--account` and `--every`.

- [ ] **Step 3: Implement** both flags in `main()`, keeping the existing exception-to-exit-code mapping in one helper that both the single run and the loop use.
- [ ] **Step 4: Run them and confirm they pass**

Run: `python3 scripts/test-mail-indexer.py && make mail-index-test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mail-index.py scripts/test-mail-indexer.py
git commit -m "Add an indexer loop and per-account mailbox listing"
```

### Task 3: `email-local` manifest replaces the hand-written mail registration

**Files:**
- Create: `addons/email-local/addon.json` (canonical JSON)
- Modify: `scripts/lib/addon_manifest.py` (remove `"mail"` and `"drafts"` from `CORE_MCP_NAMES`)
- Modify: `compose/hermes/bootstrap-api-profile.sh`: delete `register_mail_mcp_servers`, `register_mail_drafts_mcp_servers`, their call sites (lines ~2304-2305 and ~2514-2515), and the two `MAIL_*` blocks in the eager-tools reconcile (~924-941). Keep the `MAIL_*` variables that `fence_mail_from_cron` and the doctor still read.
- Modify: `scripts/sync-profiles.sh` (drop the two `MAIL_*` rows from `SERVICES_CORE`)
- Modify: `scripts/test-mail-mcp-registration.sh`, `scripts/test-mail-drafts-registration.sh`
- Modify: `addons/README.md`

**Interfaces:**
- Consumes: plan 1's gates, MCP lists, per-entry `title` and `when`.
- Produces: the manifest content below (values copied from today's bootstrap):
  - `builtin`: `{"flag": "MAIL_MCP_ENABLED", "profile": "mail", "seed": false}`
  - `gates`: `[{"flag": "MAIL_DRAFTS_ENABLED", "profile": "mail-drafts", "seed": false}]`
  - `services`: `reader: {compose: "mail-mcp", port: 8000}` and `drafts: {compose: "mail-drafts-mcp", port: 8000, when: {gate: "MAIL_DRAFTS_ENABLED"}}`
  - `mcp[0]`: name `mail`, title `Mail (read-only)`, service `reader`, transport `http`, path `/mcp`, timeout 60, connectTimeout 30, env `{url: MAIL_MCP_URL, timeout: MAIL_MCP_TIMEOUT, connectTimeout: MAIL_MCP_CONNECT_TIMEOUT}`, tools and pin `mail_status mail_search mail_get mail_thread`, writeTools `[]`, profiles `{default: rw, browser: rw}`
  - `mcp[1]`: name `drafts`, title `Mail drafts (no sending)`, service `drafts`, `when: {gate: MAIL_DRAFTS_ENABLED}`, the same transport, path and timeouts with the `MAIL_DRAFTS_MCP_*` env names, tools and pin `mail_draft_create mail_draft_status`, writeTools `["mail_draft_create"]`, profiles `{default: rw, browser: rw}`
  - `permissions`: `{reads: ["mail"], writes: ["external:mail-drafts"], sends: false, egress: [], exposesToAgent: false, cron: false}`
  - `host`: `{platform: "macos", install: "macos/mail-apps/install.sh", uninstall: "macos/mail-apps/install.sh uninstall reader", grants: ["automation:mail", "full-disk-access (store source only)"], grantTarget: "~/Applications/Assistant Mail Reader.app", check: "make mail-index-doctor"}`
  - title `Email on this Mac`; tagline and description from the opening paragraph of `docs/email.md`; version `1.0.0`; `minStackVersion` is the release that ships plan 1.

- [ ] **Step 1: Rewrite the two registration tests** to drive `addon_mcp_plan` and `register_addon_mcp_servers` the way `scripts/test-gptr-mcp-registration.sh` does, against the real `addons/` dir with `STACK_PYTHON=host`. Keep every expected call string they assert today: register on default and browser with the four (or two) tools, drop-only when off, drop-only on api-server. Add one case: `MAIL_MCP_ENABLED=0 MAIL_DRAFTS_ENABLED=1` registers drafts and drops mail.
- [ ] **Step 2: Run them and confirm they fail**

Run: `sh scripts/test-mail-mcp-registration.sh; sh scripts/test-mail-drafts-registration.sh`
Expected: FAIL, since there is no `mail` entry in the plan yet.

- [ ] **Step 3: Write the manifest and delete the hand-written registration**, as listed under Files.
- [ ] **Step 4: Run the mail and add-on tests**

Run: `sh scripts/test-mail-mcp-registration.sh && sh scripts/test-mail-drafts-registration.sh && python3 scripts/test-addon-manifests.py && sh scripts/test-addon-cron-fence.sh && sh scripts/test-no-host-python.sh`
Expected: all exit 0. `sync-profiles.sh --list` still shows `MAIL_MCP_ENABLED mail` and `MAIL_DRAFTS_ENABLED mail-drafts`.

- [ ] **Step 5: Live parity check.** Ask Keith before starting, because it recreates his `hermes` container. On `main` and then on this branch, run `make hermes-reload` with each of the four combinations of `MAIL_MCP_ENABLED`/`MAIL_DRAFTS_ENABLED`. Copy `config.yaml` and `.mail-cron-toolsets.json` for the default, browser and api-server profiles to the scratchpad and `diff` them. Expected: identical, or differing only in list order. Any other difference fails the task. These files hold config, not session content. Read nothing else under `data/hermes`.
- [ ] **Step 6: Commit**

```bash
git add addons/email-local scripts/lib/addon_manifest.py compose/hermes/bootstrap-api-profile.sh scripts/sync-profiles.sh scripts/test-mail-mcp-registration.sh scripts/test-mail-drafts-registration.sh addons/README.md
git commit -m "Register Mail.app mail and drafts from the email-local manifest"
```

### Task 4: The `email-imap` image and config renderer

**Files:**
- Create: `scripts/lib/email_addon.py`
- Create: `compose/email-imap/Dockerfile` (build context: repo root, so it can copy `scripts/`)
- Create: `compose/email-imap/.dockerignore`, or a root-context ignore list in the Dockerfile's `COPY` lines, so only the needed files are copied
- Test: `scripts/test-email-addon.py`

**Interfaces:**
- Consumes: `mail_index.load_config`, the instance file and `settings.env` formats from plan 1 Task 4 (`ADDON_EMAIL_<NAME>` keys, values written with `shlex.quote`, lists as JSON).
- Produces:
  - `render(settings_env: Path, instances_dir: Path, *, index_path: str, drafts_dir: str) -> str` returns TOML in `mail-index.toml` format.
    - Top level: `source = "imap"`, `index`, `since`, `bodies`, `attachments`, `max_body_chars`.
    - One `[[accounts]]` per instance file, sorted by slug, with `id = slug` and the instance's `host`, `port`, `security`, `user`, `address`, `addresses`, `mailboxes` and `drafts_mailbox`.
    - When the instance has a `tls_ca` secret, `tls_ca = "/run/addon/secrets/<slug>.tls_ca"`.
    - Never a password.
    - Raises `ValueError` naming the slug for `plain` on a host outside `{"host.docker.internal", "localhost", "127.0.0.1", "greenmail"}`.
  - `email_addon.py index` renders to `/tmp/config.toml` before each pass, then runs `mail-index.py run --config /tmp/config.toml` every `ADDON_EMAIL_INTERVAL` seconds (default 900). With no instances it logs `email-indexer: no accounts configured` and sleeps.
  - `email_addon.py mailboxes --account SLUG` renders, then execs `mail-index.py mailboxes --account SLUG`.
  - `email_addon.py outbox` renders, then execs `mail-drafts.py watch --no-mail-app --config /tmp/config.toml --dir /data/drafts`.
  - All three set `MAIL_CREDENTIALS_DIR=/run/addon/secrets`.
  - Image tag: `${IMAGE_PREFIX:-assistant}-email-imap:local`. Base `python:3.11-slim`. Copies only `scripts/mail-index.py`, `scripts/mail-drafts.py` and the `scripts/lib/` modules they import. Runs `python -I -B`.

- [ ] **Step 1: Write the failing tests** in `scripts/test-email-addon.py`:
  - `render` with two instance files (`work`, `home`) and a `settings.env` holding `since` and `bodies` gives TOML that `tomllib.loads` parses to two accounts in slug order. The password string written to a sibling `secrets/` dir appears nowhere in the output.
  - `mail_index.load_config` accepts the rendered file.
  - `plain` on `imap.example.com` raises `ValueError` containing `work`.
  - `plain` on `greenmail` passes.
  - An empty instances dir gives zero accounts, and `email_addon.py index` with `--passes 1` (passed through to `--every`) logs `no accounts configured` and exits 0.
- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-email-addon.py`
Expected: `ModuleNotFoundError: email_addon`.

- [ ] **Step 3: Implement** `email_addon.py` and the Dockerfile.
- [ ] **Step 4: Run the tests and build the image**

Run: `python3 scripts/test-email-addon.py && docker build -f compose/email-imap/Dockerfile -t assistant-email-imap:local .`
Expected: all `ok`; the build succeeds. `docker run --rm assistant-email-imap:local python -I -B /app/scripts/mail-index.py --help` exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/email_addon.py compose/email-imap scripts/test-email-addon.py
git commit -m "Add the email-imap image and its config renderer"
```

### Task 5: `email` compose services, networks and manifest

**Files:**
- Modify: `docker-compose.yml`: the four services, networks `email` (`internal: true`) and `email-egress` (`{}`), volume `email_index`, `hermes` joins `email`, and `EMAIL_ENABLED`/`EMAIL_DRAFTS_ENABLED` in both `hermes-*-bootstrap` environments next to `GPTR_MCP_ENABLED`
- Create: `addons/email/addon.json`
- Modify: `scripts/test-compose-env-allowlist.py` (the new profiles and env keys)
- Modify: `.env.example` (commented `EMAIL_ENABLED=0`, `EMAIL_DRAFTS_ENABLED=0`)
- Create: `scripts/test-email-registration.sh`

**Interfaces:**
- Produces, in compose:

| Service | Image | Profiles | Networks | Mounts | Command |
|---|---|---|---|---|---|
| `email-indexer` | `email-imap` | `email` | `email-egress` | `email_index:/data/index`; secrets ro; settings.env ro; instances ro | `python -I -B /app/scripts/lib/email_addon.py index` |
| `email-mcp` | `mail-mcp` image | `email` | `email` | `email_index:/data/mail:ro` | default; `MAIL_INDEX_PATH=/data/mail/index.sqlite` |
| `email-drafts-mcp` | `mail-drafts-mcp` image | `email-drafts` | `email` | `${ASSISTANT_DATA_ROOT:-./data}/addons/email/drafts:/data/mail-drafts` | default |
| `email-outbox` | `email-imap` | `email-drafts` | `email-egress` | the same drafts dir at `/data/drafts`; secrets ro; settings.env ro; instances ro | `python -I -B /app/scripts/lib/email_addon.py outbox` |

  `email-indexer` and `email-outbox` get `extra_hosts: ["host.docker.internal:host-gateway"]` for Proton Bridge. `email-mcp` and `email-drafts-mcp` copy the healthchecks of `mail-mcp` and `mail-drafts-mcp`.
- Produces, in the manifest:
  - `builtin`: `{flag: EMAIL_ENABLED, profile: email, seed: false}`
  - `gates`: `[{flag: EMAIL_DRAFTS_ENABLED, profile: email-drafts, seed: false}]`
  - `services`:
    - `indexer`: `{compose: email-indexer, mounts: {secrets: ro, config: ro, instances: ro}, volumes: ["email_index"]}`
    - `mcp`: `{compose: email-mcp, port: 8000}`
    - `drafts-mcp`: `{compose: email-drafts-mcp, port: 8000, when}`
    - `outbox`: `{compose: email-outbox, mounts: {secrets: ro, config: ro, instances: ro}, when}`
  - `mcp`: entries `email` (title `Email (read-only)`) and `email-drafts` (title `Email drafts (no sending)`, `when` gate). Tools, pins and writeTools as in `email-local`, profiles `{default: rw, browser: rw}`, no `env` block.
  - `settings`:
    - `since`: date, required
    - `bodies`: bool, default false
    - `attachments`: bool, default false
    - `max_body_chars`: int, default 100000
    - `interval`: int, default 900, min 300
    - `drafts`: bool, default false, flag `EMAIL_DRAFTS_ENABLED`
  - `instances`: `{name: "accounts", settings: …}` with the spec's account table: `host`; `port` (default 993); `security` (choice, default `tls`); `user`; `address`; `addresses` (list, optional); `mailboxes` (list, required); `tls_ca` (secret, optional); `drafts_mailbox` (optional); `password` (secret, required).
  - `commands`: `{"mailboxes": {service: indexer, instance: true, argv: ["python", "-I", "-B", "/app/scripts/lib/email_addon.py", "mailboxes"]}}`
  - `permissions`: `{reads: ["mail"], writes: ["external:mail-drafts"], sends: false, egress: ["internet"], exposesToAgent: false, cron: false}`
  - title `Email (IMAP)`.

- [ ] **Step 1: Write the failing tests.**
  - `scripts/test-email-registration.sh`: modelled on Task 3's rewritten tests. Off drops both URLs. On registers `email` on default and browser. `EMAIL_DRAFTS_ENABLED=1` adds `email-drafts`. api-server only ever drops.
  - `test-addon-manifests.py` (already generic) must pass for `addons/email`, including the plan 1 checks that mounts match compose and that services holding secrets aren't MCP services.
- [ ] **Step 2: Run them and confirm they fail**

Run: `sh scripts/test-email-registration.sh; python3 scripts/test-addon-manifests.py`
Expected: FAIL, since there is no `email` manifest yet.

- [ ] **Step 3: Add the compose services, networks, volume and manifest.**
- [ ] **Step 4: Run the tests and compose config**

Run: `sh scripts/test-email-registration.sh && python3 scripts/test-addon-manifests.py && python3 scripts/test-compose-env-allowlist.py && docker compose --profile email --profile email-drafts config -q`
Expected: all exit 0.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml addons/email scripts/test-compose-env-allowlist.py .env.example scripts/test-email-registration.sh
git commit -m "Add the email add-on: IMAP indexer, MCP, drafts and outbox services"
```

### Task 6: `make email-test`, end to end on GreenMail

**Files:**
- Create: `scripts/test-email-e2e.py`
- Modify: `scripts/test-mail-imap.py` (expose `greenmail()`, `seed()`, `server_state()`, `rows()` for import; no behaviour change)
- Modify: `Makefile` (`email-test`, added to `.PHONY`)

**Interfaces:**
- Consumes: `scripts/addon.sh` (plan 1), Tasks 4-5, and the GreenMail helpers.
- Produces: `make email-test`. It runs a throwaway compose project (`COMPOSE_PROJECT_NAME=email-e2e`, `ASSISTANT_DATA_ROOT` set to a temp dir, a temp `.env` copied from `.env.example`). GreenMail runs as container `greenmail` on that project's `email-egress` network. The test skips with a message when Docker isn't running, and always runs `docker compose down -v` for its project.

- [ ] **Step 1: Write the test** `scripts/test-email-e2e.py`:
  - Seed GreenMail with the synthetic messages for two users.
  - Configure two instances (`work`, `home`) through `addon.sh configure email --instance …` with `host: greenmail`, `port: 3143`, `security: plain`, the GreenMail test passwords on stdin, and `mailboxes`.
  - `addon.sh run email mailboxes --instance work` lists `INBOX` with a count.
  - Set `since` and `drafts: true`, `addon.sh enable email` (with `make hermes-reload` stubbed through `PATH`), and wait for the first pass in `docker compose logs email-indexer`.
  - Copy the index out of `email_index` with `docker compose cp`. Its rows match what `test-mail-imap.py` builds with the host IMAP source for the same accounts, by the same `rows()` comparison.
  - `server_state()` is unchanged after indexing.
  - Write a draft request and a reply request into the drafts dir in the queue format `mail-drafts-mcp` writes (use `compose/mail-drafts-mcp/drafts.py` to build them). Within 60 s both appear in each account's Drafts on GreenMail, and nothing appears in Sent.
  - Isolation:
    - `docker inspect` shows no mount at `/run/addon/secrets` on `email-mcp` or `email-drafts-mcp`.
    - From `email-mcp`, a TCP connect to `greenmail:3143` fails, and so does one to `1.1.1.1:443`.
    - From `email-indexer`, resolving `hermes` and `email-mcp` fails.
  - No password string appears in `docker compose logs` or in `docker inspect` output for any service.
  - `addon.sh configure email --instance home --remove`, then the next pass's `mail_status` (via `docker compose exec email-mcp`) lists only `work`.
- [ ] **Step 2: Run it**

Run: `make email-test`
Expected: every check `ok`. It may fail at first on the network isolation or remove checks; fix the compose file or `email_addon.py`, not the test.

- [ ] **Step 3: Commit**

```bash
git add scripts/test-email-e2e.py scripts/test-mail-imap.py Makefile
git commit -m "Test the email add-on end to end against GreenMail"
```

### Task 7: Docs, changelog and the real-account handoff

**Files:**
- Modify: `docs/email.md`
- Modify: `addons/README.md`, `CHANGELOG.md`, `docs/README.md` (index line)

- [ ] **Step 1: `docs/email.md`.**
  - Retitle it "Email". Open with the two add-ons: `email-local`, which is the existing content, unchanged apart from headings, and `email`.
  - Add an "IMAP add-on (`email`)" section covering the four services and the diagram from the spec, `addon.sh` configure/run/enable with the account settings table, and Proton through `host.docker.internal`.
  - State the password rule as the spec words it: never in a container Hermes can reach, `.env`, environment, arguments or `docker inspect`. Say that `email-local` with the Keychain keeps the stricter rule.
  - Point the "IMAP source" section's first lines at the `email` add-on for new IMAP users.
- [ ] **Step 2: CHANGELOG.** Under Unreleased:
  - `email-local` is a manifest only, with no install change.
  - New `email` add-on: services, the `email` and `email-egress` networks (hermes joins `email`), the `email_index` volume, `EMAIL_ENABLED` and `EMAIL_DRAFTS_ENABLED`.
  - "Not tested on a real account yet."
- [ ] **Step 3: Write the real-account steps** in `docs/email.md` under "First real run" for `email`, for Keith to run himself: configure one account through `addon.sh` with the password on stdin (`read -rs`, then pipe a JSON payload built with `python3 -c`), `addon.sh run email mailboxes`, enable, then check `docker compose logs email-indexer` for counts. Ask only for counts and pass/fail.
- [ ] **Step 4: Run the full suite once**

Run: `make addon-test && make mail-imap-test && make email-test && sh scripts/test-mail-mcp-registration.sh && sh scripts/test-mail-drafts-registration.sh && sh scripts/test-email-registration.sh`
Expected: all exit 0.

- [ ] **Step 5: Commit**

```bash
git add docs/email.md docs/README.md addons/README.md CHANGELOG.md
git commit -m "Document the email and email-local add-ons"
```
