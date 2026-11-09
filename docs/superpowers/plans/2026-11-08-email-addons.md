# Email add-ons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe today's Mail.app setup as the `email-local` add-on with no behaviour change, and add the `email` add-on, which provides several IMAP accounts with read and drafts and runs entirely in containers, so Boundary can install it with `scripts/addon.sh`.

**Architecture:** `email-local` is a manifest over the existing `mail-mcp` and `mail-drafts-mcp` services. It replaces the hand-written mail registration in the bootstraps. `email` adds four compose services. `email-indexer` and `email-outbox` share one new image built from the existing indexer and drafts code, hold the passwords, and sit only on an egress network. `email-mcp` and `email-drafts-mcp` reuse the existing images on a new internal network shared with Hermes. A small entrypoint renders the indexer config from the add-on's instance files on each pass.

**Tech Stack:** Python 3.11 standard library, POSIX sh, Docker Compose, GreenMail `greenmail/standalone:2.1.3` for tests.

**Spec:** `docs/superpowers/specs/2026-11-08-email-addons-design.md`. **Depends on:** `docs/superpowers/plans/2026-11-08-addon-manifest-path.md`, merged to `main` in 482601d. Where this plan and the spec disagree about the add-on data layout, follow the code on `main` and `docs/extending.md` ("Add-ons: scripts/addon.sh"); plan 1's review changed a few details after the spec was written, listed under Global Constraints.

## Global Constraints

- Follow `~/.claude/rules/email-privacy.md`. Use synthetic mail and GreenMail only, never a real account. Real runs are Keith's. The mail hook blocks commands that name mail stores or Mail's metadata database; don't name them, and report any block.
- `email-local` changes nothing an install sets: `MAIL_MCP_ENABLED`, `MAIL_DRAFTS_ENABLED`, profiles `mail` and `mail-drafts`, MCP names `mail` and `drafts`, `MAIL_MCP_URL`/`MAIL_DRAFTS_MCP_URL` and their timeout overrides, `MAIL_INDEX_DIR`, `MAIL_DRAFTS_DIR`, `setup.sh --mail`/`--mail-drafts`, `make mail-triage-cron`.
- `email` names: id `io.github.kaxline.stack.email`, flag `EMAIL_ENABLED` (profile `email`), gate `EMAIL_DRAFTS_ENABLED` (profile `email-drafts`), MCP names `email` and `email-drafts`, named volume `email_index`, data directory `data/addons/email/`.
- Neither add-on registers on `api-server`. Both have `permissions.cron: false` and `reads: ["mail"]`.
- Container hardening on all four `email` services: `read_only: true`, `cap_drop: ["ALL"]`, `security_opt: ["no-new-privileges:true"]`, `tmpfs: ["/tmp"]`, `user: "${HERMES_UID:-1000}:${HERMES_GID:-1000}"`, no host ports.
- Password files: `data/addons/email/secrets/<slug>.password`, mounted read-only at `/run/addon/secrets` into `email-indexer` and `email-outbox` only.
- Commit messages and docs must not name Mail's metadata database (the hook blocks them).
- Add-on data layout from plan 1, as merged: `data/addons/email/config/settings.env`, `instances/<slug>.json` and `secrets/<slug>.<name>`. A service's `mounts` give it `/run/addon/config` (the directory, not a single file), `/run/addon/instances` and `/run/addon/secrets`. Containers parse `settings.env` with `shlex.split` per line, as `docs/extending.md` describes. Never use compose `env_file` or `docker --env-file` for it.
- Settings keys in `settings.env` are `ADDON_EMAIL_<NAME>`, with `-` mapped to `_`. Bools are written as `1`/`0`, lists as JSON and dates as `YYYY-MM-DD`.
- Files under `data/addons/email/` are created by `addon.sh` as the host user (`id -u`). The four `email` services run as `HERMES_UID:HERMES_GID`. On Linux the two must match, which is the rule `docs/hermes.md` already sets for other bind mounts. On Docker Desktop for Mac the bind mounts are readable either way.
- `email_index` and `email_drafts` are named volumes. The `email-imap` image creates `/data/index` and `/data/drafts` with mode `1777`, and each volume is first mounted by an `email-imap` container, so it starts with that mode instead of root-owned `755`.
- `configure` writes a gate flag only while the add-on is enabled. A gate change takes effect at the next `enable`, which Boundary calls after `configure`.
- `list --json` already carries the settings `schema`. `missing` reports `instances:accounts` while no account exists.

## Review Focus

- An instance removed with `configure --remove` must disappear from the next index pass's config, and its messages must stop showing in search.
- A password the server refuses must log `mail-index:` with the account id and exit code 4 in the pass result, never the password. The loop must go on to the next pass and the other accounts.
- `drafts` turned off while drafts are queued: `email-outbox` stops, and the queue stays on disk for when it's turned back on.
- No instances configured yet but `EMAIL_ENABLED=1` (a hand-edited `.env`): `email-indexer` writes no index and logs one line per pass. `email-mcp` reports an empty `mail_status` instead of crashing.
- An instance with `security = "plain"` and a public host must be refused at render time with the instance slug named, for any host except `host.docker.internal`, `localhost`, `127.0.0.1` and the test server name `greenmail`.
- `enable` on a fresh data root, before any `configure`, must not let Docker create a bind source. Docker would create it root-owned, and every later `configure` would fail.

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
- Modify: `scripts/sync-profiles.sh` (drop the two `MAIL_*` rows from `SERVICES_CORE`. Plan 1's contract test fails on a flag listed twice, so the manifest rows can't coexist with the static ones.)
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
- Consumes: `mail_index.load_config`, and the instance file and `settings.env` formats in `scripts/lib/addon_store.py` (see Global Constraints). `email_addon.py` doesn't import `addon_store`; it parses the files itself. The tests build their fixtures with `addon_store.apply_settings` and `apply_instance`, so the two formats can't drift apart.
- Produces:
  - `render(config_dir: Path, instances_dir: Path, *, index_path: str, drafts_dir: str) -> str` returns TOML in `mail-index.toml` format. It reads `config_dir / "settings.env"`; a missing file means every setting takes its default.
    - Top level: `source = "imap"`, `index`, `since`, `bodies`, `attachments`, `max_body_chars`.
    - One `[[accounts]]` per instance file, sorted by slug, with `id = slug` and the instance's `host`, `port`, `security`, `user`, `address`, `addresses`, `mailboxes` and `drafts_mailbox`.
    - When the instance has a `tls_ca` secret, `tls_ca = "/run/addon/secrets/<slug>.tls_ca"`.
    - Never a password.
    - Raises `ValueError` naming the slug for `plain` on a host outside `{"host.docker.internal", "localhost", "127.0.0.1", "greenmail"}`.
  - `email_addon.py index` renders to `/tmp/config.toml` before each pass, then runs `mail-index.py run --config /tmp/config.toml` every `ADDON_EMAIL_INTERVAL` seconds (default 900). With no instances it logs `email-indexer: no accounts configured` and sleeps.
  - `email_addon.py mailboxes --account SLUG` renders, then execs `mail-index.py mailboxes --account SLUG`.
  - `email_addon.py outbox` renders, then execs `mail-drafts.py watch --no-mail-app --config /tmp/config.toml --dir /data/drafts`.
  - All three read `/run/addon/config` and `/run/addon/instances`, and set `MAIL_CREDENTIALS_DIR=/run/addon/secrets`.
  - Image tag: `${IMAGE_PREFIX:-assistant}-email-imap:local`. Base `python:3.11-slim`. Copies only `scripts/mail-index.py`, `scripts/mail-drafts.py` and the `scripts/lib/` modules they import. Runs `python -I -B`. Creates `/data/index` and `/data/drafts` with mode `1777`, so a new named volume mounted there starts writable for `HERMES_UID`.

- [ ] **Step 1: Write the failing tests** in `scripts/test-email-addon.py`:
  - Build the fixture with `addon_store`: `apply_settings` with `since` and `bodies`, then `apply_instance` for `work` and `home` with a password. `render(<sid dir>/config, <sid dir>/instances, …)` gives TOML that `tomllib.loads` parses to two accounts in slug order. The password string appears nowhere in the output.
  - A list setting with a value containing a quote and a space (`mailboxes: ["INBOX", "Saved 'old'"]`) round-trips into the TOML unchanged.
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
Expected: all `ok`; the build succeeds. `docker run --rm assistant-email-imap:local python -I -B /app/scripts/mail-index.py --help` exits 0, and `docker run --rm assistant-email-imap:local stat -c %a /data/index /data/drafts` prints `1777` twice.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/email_addon.py compose/email-imap scripts/test-email-addon.py
git commit -m "Add the email-imap image and its config renderer"
```

### Task 5: `addon.sh` prepares mount sources, stops gated-off services and owns its volumes

Plan 1 left four gaps that the `email` add-on is the first to hit. This task closes them in the generic code, before any `email` service exists.

**Files:**
- Modify: `scripts/lib/addon_store.py` (new public `prepare`), `scripts/lib/addon_cli.py` (`plan-enable`), `scripts/addon.sh` (`enable`)
- Modify: `scripts/test-addon-manifests.py` (volume ownership cross-check), `scripts/test-addon-store.py`, `scripts/test-addon-cli.sh`
- Modify: `docs/extending.md` (the `enable` paragraph)

**Interfaces:**
- Consumes: plan 1's `addon_store._mkdir_addon`, `cmd_plan_enable`, the `name@gate` service list it prints, and `addon.sh enable`.
- Produces:
  - `addon_store.prepare(manifest: dict, data_root: Path) -> None` creates `config/`, `instances/` and `secrets/` under `addon_dir` at `0700`, without touching existing files. It is called only when some service declares `mounts`. `plan-enable` calls it after the missing-settings check. `_stored` and `_slugs` become public as `stored` and `slugs`, since `addon_cli` uses them.
  - `plan-enable` prints a `stop` list beside `services`: the add-on's gated services whose gate flag ends at `0`. `addon.sh enable` runs `docker compose stop` on that list after `up`, so turning `drafts` off and calling `enable` stops `email-outbox` and `email-drafts-mcp`. A non-empty `stop` failing is a `die` (exit 1), like `up`.
  - `addon.sh enable` refuses (exit 1, `error: HERMES_UID <n> is not your user id <m>; set HERMES_UID and HERMES_GID in .env`) when `uname -s` is `Linux`, some service declares `mounts`, and `HERMES_UID` from `.env` (default `1000`) differs from `id -u`. The same check applies to `HERMES_GID` against `id -g`.
  - `test-addon-manifests.py`: each name in any `services.<key>.volumes` is mounted in `docker-compose.yml` only by that add-on's compose services. Otherwise `--purge` could remove a volume another service uses.

- [ ] **Step 1: Write the failing tests.**
  - `test-addon-store.py`: `prepare` on an empty data root creates the three directories at `0700`. A second call leaves an existing `secrets/x` file and its mode alone.
  - `test-addon-cli.sh`: with a fixture whose service declares `mounts` and has no required settings, `enable` before any `configure` leaves `config/`, `instances/` and `secrets/` in place before the stubbed `docker compose up` runs. Check that the stub logs `up` and the directories exist at that moment by having the stub record `ls` of the data dir. After `configure fixture {"extra": false}` on the enabled fixture, `enable` logs `stop` with the gated service. `HERMES_UID=4242` in `.env`, with `uname` stubbed to print `Linux`, makes `enable` exit 1 with `error: HERMES_UID`.
  - `test-addon-manifests.py`: a fixture compose snippet where a core service also mounts the add-on's volume is reported as a problem.
- [ ] **Step 2: Run them and confirm they fail**

Run: `python3 scripts/test-addon-store.py; bash scripts/test-addon-cli.sh; python3 scripts/test-addon-manifests.py`
Expected: FAIL on the new checks only.

- [ ] **Step 3: Implement** the four changes above and update the `enable` paragraph in `docs/extending.md`.
- [ ] **Step 4: Run them and confirm they pass**

Run: `make addon-test && sh scripts/test-no-host-python.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/addon_store.py scripts/lib/addon_cli.py scripts/addon.sh scripts/test-addon-manifests.py scripts/test-addon-store.py scripts/test-addon-cli.sh docs/extending.md
git commit -m "Prepare add-on mount sources, stop gated-off services and check volume ownership"
```

### Task 6: `email` compose services, networks and manifest

**Files:**
- Modify: `docker-compose.yml`: the four services, networks `email` (`internal: true`) and `email-egress` (`{}`), volumes `email_index` and `email_drafts`, `hermes` joins `email`, and `EMAIL_ENABLED`/`EMAIL_DRAFTS_ENABLED` in both `hermes-*-bootstrap` environments next to `GPTR_MCP_ENABLED`
- Create: `addons/email/addon.json`
- Modify: `scripts/test-compose-env-allowlist.py` (the new profiles and env keys)
- Modify: `.env.example` (commented `EMAIL_ENABLED=0`, `EMAIL_DRAFTS_ENABLED=0`)
- Create: `scripts/test-email-registration.sh`

**Interfaces:**
- Produces, in compose:

| Service | Image | Profiles | Networks | Mounts | Command |
|---|---|---|---|---|---|
| `email-indexer` | `email-imap` | `email` | `email-egress` | `email_index:/data/index`; the three add-on mounts, ro | `python -I -B /app/scripts/lib/email_addon.py index` |
| `email-mcp` | `mail-mcp` image | `email` | `email` | `email_index:/data/mail:ro` | default; `MAIL_INDEX_PATH=/data/mail/index.sqlite` |
| `email-drafts-mcp` | `mail-drafts-mcp` image | `email-drafts` | `email` | `email_drafts:/data/mail-drafts` | default |
| `email-outbox` | `email-imap` | `email-drafts` | `email-egress` | `email_drafts:/data/drafts`; the three add-on mounts, ro | `python -I -B /app/scripts/lib/email_addon.py outbox` |

  "The three add-on mounts" are `${ASSISTANT_DATA_ROOT:-./data}/addons/email/secrets:/run/addon/secrets:ro`, `…/config:/run/addon/config:ro` and `…/instances:/run/addon/instances:ro`, in the exact form plan 1's mount cross-check requires. `email-indexer` and `email-outbox` get `extra_hosts: ["host.docker.internal:host-gateway"]` for Proton Bridge. `email-mcp` and `email-drafts-mcp` copy the healthchecks of `mail-mcp` and `mail-drafts-mcp`. `email-mcp` has `depends_on: {email-indexer: {condition: service_started}}`, and `email-drafts-mcp` has the same on `email-outbox`, so an `email-imap` container mounts each new volume first and its `1777` directory seeds the volume.
- Produces, in the manifest:
  - `builtin`: `{flag: EMAIL_ENABLED, profile: email, seed: false}`
  - `gates`: `[{flag: EMAIL_DRAFTS_ENABLED, profile: email-drafts, seed: false}]`
  - `services`:
    - `indexer`: `{compose: email-indexer, mounts: {secrets: ro, config: ro, instances: ro}, volumes: ["email_index"]}`
    - `mcp`: `{compose: email-mcp, port: 8000}`
    - `drafts-mcp`: `{compose: email-drafts-mcp, port: 8000, when}`
    - `outbox`: `{compose: email-outbox, mounts: {secrets: ro, config: ro, instances: ro}, volumes: ["email_drafts"], when}`. The drafts queue lives in this volume, so `disable` keeps it and `disable --purge` removes it with the index.
  - `mcp`: entries `email` (title `Email (read-only)`) and `email-drafts` (title `Email drafts (no sending)`, `when` gate). Tools, pins and writeTools as in `email-local`, profiles `{default: rw, browser: rw}`, no `env` block.
  - `settings`:
    - `since`: date, required
    - `bodies`: bool, default false
    - `attachments`: bool, default false
    - `max_body_chars`: int, default 100000
    - `interval`: int, default 900, min 300
    - `drafts`: bool, default false, flag `EMAIL_DRAFTS_ENABLED`
  - `instances`: `{name: "accounts", settings: …}` with the spec's account table: `host`; `port` (default 993); `security` (choice, `options: ["tls", "starttls", "plain"]`, default `tls`); `user`; `address`; `addresses` (list, optional); `mailboxes` (list, required); `tls_ca` (secret, optional); `drafts_mailbox` (optional); `password` (secret, required).
  - `commands`: `{"mailboxes": {service: indexer, instance: true, argv: ["python", "-I", "-B", "/app/scripts/lib/email_addon.py", "mailboxes"]}}`
  - `permissions`: `{reads: ["mail"], writes: ["external:mail-drafts"], sends: false, egress: ["internet"], exposesToAgent: false, cron: false}`
  - title `Email (IMAP)`.

- [ ] **Step 1: Write the failing tests.**
  - `scripts/test-email-registration.sh`: modelled on Task 3's rewritten tests. Off drops both URLs. On registers `email` on default and browser. `EMAIL_DRAFTS_ENABLED=1` adds `email-drafts`. api-server only ever drops.
  - `test-addon-manifests.py` (already generic) must pass for `addons/email`, including the plan 1 checks that mounts match compose and that services holding secrets aren't MCP services, and Task 5's volume ownership check.
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

### Task 7: `make email-test`, end to end on GreenMail

**Files:**
- Create: `scripts/test-email-e2e.py`
- Modify: `scripts/test-mail-imap.py` (expose `greenmail()`, `seed()`, `server_state()`, `rows()` for import; no behaviour change)
- Modify: `Makefile` (`email-test`, added to `.PHONY`)

**Interfaces:**
- Consumes: `scripts/addon.sh` (plan 1 and Task 5), Tasks 4 and 6, and the GreenMail helpers.
- Produces: `make email-test`. It runs a throwaway compose project (`COMPOSE_PROJECT_NAME=email-e2e`, `ASSISTANT_DATA_ROOT` set to a temp dir, a temp `.env` copied from `.env.example`). GreenMail runs as container `greenmail` on that project's `email-egress` network. The test skips with a message when Docker isn't running, and always runs `docker compose down -v` for its project.

- [ ] **Step 1: Write the test** `scripts/test-email-e2e.py`:
  - Seed GreenMail with the synthetic messages for two users.
  - On the fresh data root, `addon.sh enable email` exits 3, and its error names `since` and `instances:accounts`.
  - Configure two instances (`work`, `home`) through `addon.sh configure email --instance …` with `host: greenmail`, `port: 3143`, `security: plain`, the GreenMail test passwords on stdin, and `mailboxes`.
  - `addon.sh run email mailboxes --instance work` lists `INBOX` with a count.
  - Set `since` and `drafts: true`, `addon.sh enable email` (with `make hermes-reload` stubbed through `PATH`), and wait for the first pass in `docker compose logs email-indexer`.
  - Copy the index out of `email_index` with `docker compose cp`. Its rows match what `test-mail-imap.py` builds with the host IMAP source for the same accounts, by the same `rows()` comparison.
  - `server_state()` is unchanged after indexing.
  - Write a draft request and a reply request in the queue format `mail-drafts-mcp` writes (use `compose/mail-drafts-mcp/drafts.py` to build them), and `docker compose cp` them into `email-drafts-mcp:/data/mail-drafts`. Within 60 s both appear in each account's Drafts on GreenMail, and nothing appears in Sent.
  - Drafts off: `configure email` with `{"drafts": false}`, then `enable email`. `docker compose ps` shows `email-outbox` and `email-drafts-mcp` stopped, while `email-indexer` and `email-mcp` keep running. A request copied in before the stop is still in `email_drafts` (check with `docker compose run --rm --no-deps email-outbox ls /data/drafts`). After `{"drafts": true}` and `enable`, it reaches GreenMail.
  - Isolation:
    - `docker inspect` shows no mount at `/run/addon/secrets` on `email-mcp` or `email-drafts-mcp`.
    - From `email-mcp`, a TCP connect to `greenmail:3143` fails, and so does one to `1.1.1.1:443`.
    - From `email-indexer`, resolving `hermes` and `email-mcp` fails.
  - No password string appears in `docker compose logs` or in `docker inspect` output for any service.
  - `addon.sh configure email --instance home --remove`, then the next pass's `mail_status` (via `docker compose exec email-mcp`) lists only `work`.
  - `addon.sh disable email` stops all four services, even though `sync-profiles.sh` has already dropped their profiles. This is the live check plan 1 couldn't run against stubs. `email_index`, `email_drafts` and `data/addons/email/` remain.
  - `addon.sh disable email --purge` removes `data/addons/email/` and both volumes under the `email-e2e` project name.
- [ ] **Step 2: Run it**

Run: `make email-test`
Expected: every check `ok`. It may fail at first on the network isolation or remove checks; fix the compose file or `email_addon.py`, not the test.

- [ ] **Step 3: Commit**

```bash
git add scripts/test-email-e2e.py scripts/test-mail-imap.py Makefile
git commit -m "Test the email add-on end to end against GreenMail"
```

### Task 8: Docs, changelog and the real-account handoff

**Files:**
- Modify: `docs/email.md`
- Modify: `addons/README.md`, `CHANGELOG.md`, `docs/README.md` (index line)

- [ ] **Step 1: `docs/email.md`.**
  - Retitle it "Email". Open with the two add-ons: `email-local`, which is the existing content, unchanged apart from headings, and `email`.
  - Add an "IMAP add-on (`email`)" section covering the four services and the diagram from the spec, `addon.sh` configure/run/enable with the account settings table, and Proton through `host.docker.internal`.
  - State the password rule as the spec words it: never in a container Hermes can reach, `.env`, environment, arguments or `docker inspect`. Say that `email-local` with the Keychain keeps the stricter rule.
  - Give the order Boundary calls: `list --json` (the `schema` key drives the account form), `configure` for settings and each account, `run email mailboxes --instance <slug>` to fill the mailbox choice, then `enable`. Changing `drafts` later is `configure` followed by `enable`. On Linux, `HERMES_UID`/`HERMES_GID` must match the installing user, or `enable` refuses.
  - Point the "IMAP source" section's first lines at the `email` add-on for new IMAP users.
- [ ] **Step 2: CHANGELOG.** Under Unreleased:
  - `email-local` is a manifest only, with no install change.
  - New `email` add-on: services, the `email` and `email-egress` networks (hermes joins `email`), the `email_index` and `email_drafts` volumes, `EMAIL_ENABLED` and `EMAIL_DRAFTS_ENABLED`.
  - `addon.sh enable` (Task 5) now creates the add-on's mount directories, stops gated services whose gate is off, and, on Linux, refuses when `HERMES_UID`/`HERMES_GID` differ from the user running it.
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
