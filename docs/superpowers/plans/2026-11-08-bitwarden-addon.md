# Bitwarden add-on Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hermes signs in to websites with Bitwarden logins, and stack services take secrets from Bitwarden, without any Bitwarden secret reaching the gateway, a tool result or a file agent code can read.

**Architecture:** A `bitwarden-broker` sidecar in the `hermes` network namespace holds every account's `bw` state and fills pages over CDP itself. A Hermes login-backend plugin (`bwbroker:`) and two overlays route fills to it and scrub filled values from model output by digest. One-shot services (`bitwarden-setup`, `bitwarden-secrets-sync`) do the work that needs the user's unlock or writes secret files. `scripts/vault.sh` and `make vault-*` drive it all, and an optional local Vaultwarden gives a from-scratch mode.

**Tech Stack:** Python 3 (stdlib plus Debian's `python3-websockets`), `bw` 2026.10.1 native build, Vaultwarden 1.37.4, nginx 1.30.6-alpine, Bash 3.2-compatible host scripts, Docker Compose, Hermes v0.22.7 overlays.

**Spec:** `docs/superpowers/specs/2026-11-08-bitwarden-addon-design.md`. Read it before each task; this plan names decisions, the spec gives the reasons.

## Global Constraints

- Hermes image `nousresearch/hermes-agent:v0.22.7`. Overlays follow the existing pattern: `compose/hermes/patch-<name>.py` with `MARKER`, `ANCHOR`, `NEEDED`, an `already applied` second run, applied from `compose/hermes/05-patch-tool-calling.sh` via `run_patch`, mounted read-only under `/bootstrap/` in `docker-compose.yml`.
- `bw` version `2026.10.1`, build argument `BW_CLI_VERSION`, with the per-architecture SHA-256 taken from GitHub's asset digest (`gh api repos/bitwarden/clients/releases/tags/cli-v2026.10.1`) and written into the Dockerfile.
- Vaultwarden `vaultwarden/server:1.37.4@sha256:efb3cde962015fcc036b2ea625242248611943b27212ebf4392841fb30fad055`. TLS sidecar `nginx:1.30.6-alpine` (the `VIKUNJA_PROXY_IMAGE` default). Broker base `debian:bookworm-slim`, pinned by digest at implementation time.
- Every new flag defaults off: `BITWARDEN_ENABLED=0`, `BITWARDEN_LOCAL_ENABLED=0`. With both off, `docker compose config` and every bootstrap output are byte-identical to main.
- Broker listens on `127.0.0.1:8099` only. Vaultwarden host port `127.0.0.1:${VAULTWARDEN_PORT:-8222}`.
- Data under `data/addons/bitwarden/` (gitignored): `bw/<slug>/`, `secrets/<slug>/` (mode 600 files `client_secret`, `master_password`), `secrets/local/tls/`, `broker/requests.json`, `broker/denylist.json`, `vaultwarden/`. Account files `compose/bitwarden/accounts/<slug>.env`, mode 600, gitignored.
- No value, session key or page content in any broker response, log line, error text or test output. Tests prove this by searching all captured output for the synthetic secrets.
- Synthetic data only: `example.com` sites, made-up names, generated passwords. No real Bitwarden account is touched by any test or by the implementer.
- Host scripts run under macOS `/bin/bash` 3.2 (`scripts/test-bash32.sh`) and need no host `python3` on first run (`scripts/test-no-host-python.sh`).
- Writing rules in `~/.claude/rules/writing-style.md` apply to docs, comments, commit messages and CHANGELOG.
- While iterating, run only the task's own test file. Run the full suite once before each commit through `~/.claude/bin/testq`, in the background.
- Branch names never contain "claude". Commit as the configured git identity with no co-author trailer. Never push.

## Review Focus

1. **A login item with mixed URIs** (`androidapp://…`, an `https://` URI with match type Never, a bare host with no scheme, and one good `https://` URI). It should be listed with exactly the good origin, and a fill on any other origin refused. Test in Task 3 (`test_mixed_uris`).
2. **A hand-edited account file**: CRLF line endings, `BW_MODE="cloud"` in double quotes, trailing spaces, a comment line. A person expects it to load. An unknown value (`BW_UNLOCK=auto`) or unknown key must still refuse the account with a message that names the file and key. Test in Task 3 (`test_account_file_tolerance`).
3. **A password with non-ASCII characters, `+`, `/`, `%`, or an embedded newline.** Its base64, percent-encoded and reversed forms, and the CR/LF-stripped form core registers, must all be scrubbed. Test in Task 2 (`test_unicode_and_newline_values`).
4. **The broker restarting while `hermes` stays up, or `hermes` being recreated under it.** `stored` accounts unlock again with no user action. Pending requests and denylist entries survive. A fill during the restart window gets the "Bitwarden is unavailable" result, not a stack trace. Tests in Task 5 (`test_restart_keeps_requests_and_denylist`) and Task 8 (`test_broker_down_result`).
5. **The page navigating, or the tab closing, between core's origin pre-check and the broker's write.** The broker re-reads the URL and refuses with fixed text; a closed target returns fixed text. Test in Task 4 (`test_target_navigated`, `test_target_closed`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/lib/addon_manifest.py` | add `credentials` class, `vaultBackend` validation, `vault-plan` subcommand |
| `compose/bitwarden-broker/broker/digest_redaction.py` | shared: build redaction entries (broker) and scan text (gateway) |
| `compose/hermes/patch-digest-redaction.py` | overlay: extend `agent/redact.py` with digest entries |
| `compose/bitwarden-broker/Dockerfile` | broker image: Debian slim, `bw` native, python3, `python3-websockets` |
| `compose/bitwarden-broker/broker/accounts.py` | account file parsing, fail-closed validation |
| `compose/bitwarden-broker/broker/bwcli.py` | the only code that runs `bw`; per-account lock, 429 backoff |
| `compose/bitwarden-broker/broker/vault.py` | item listing, scope filtering, origins, handles, denylist |
| `compose/bitwarden-broker/broker/cdp.py` | target lookup, origin check, fill write over CDP |
| `compose/bitwarden-broker/broker/requests_store.py` | access requests: validation, cap, expiry, persistence |
| `compose/bitwarden-broker/broker/server.py` | HTTP API on loopback, sync loop, secrets socket, `main()` |
| `compose/bitwarden-broker/setup/setup.py` | `bitwarden-setup`: borrowed unlock, create, seed, grant, role change |
| `compose/bitwarden-broker/setup/vw_register.py` | Vaultwarden client-side registration and org creation |
| `compose/bitwarden-broker/secrets_sync.py` | `bitwarden-secrets-sync`: item-name mapping, file writes |
| `compose/bitwarden-tls/default.conf.template` | TLS proxy config for `vaultwarden.vault.internal` |
| `compose/hermes/plugins/bwbroker/plugin.yaml`, `__init__.py` | Hermes plugin: registers backend and the two tools |
| `compose/hermes/bitwarden_broker_backend.py` | `LoginBackend` subclass over the broker API |
| `compose/hermes/patch-bitwarden-broker-fill.py` | overlay: route `bwbroker:` fills, list hint, save refusal |
| `compose/hermes/bootstrap-api-profile.sh` | install plugin from `vault-plan`; `vault.bitwarden.enabled: false` |
| `scripts/vault.sh` | host commands: `init add unlock lock sync grant requests remove open secrets-sync` |
| `scripts/fixtures/fake-bw.py` | fake `bw` answering from a synthetic JSON vault |
| `addons/bitwarden/addon.json` | built-in manifest |
| `docker-compose.yml`, `docker-compose.hermes-terminal.yml` | services, `vault` network, mounts, env allowlist |
| `docs/bitwarden.md`, `SECURITY.md`, `docs/hermes.md`, `CHANGELOG.md`, `compose/hermes/skills/browser-login/SKILL.md` | documentation |

Deviation from the spec, recorded here and in the CHANGELOG: the plugin is a directory (`compose/hermes/plugins/bwbroker/`) because Hermes loads plugins from `$HERMES_HOME/plugins/<name>/plugin.yaml` plus `__init__.py` with `register(ctx)`. The spec's `compose/hermes/bitwarden_broker_backend.py` is the backend module that the plugin's `__init__.py` imports. The manifest's `vaultBackend.plugin` names the directory.

Second deviation: `POST /fill` also takes `port`, the CDP port from the task's `cdp_url`, because Chromium picks a random DevTools port (`--remote-debugging-port=0`). The broker connects only to `127.0.0.1:<port>`. A caller can point it at a CDP endpoint of its own, which the spec's last Documentation paragraph already covers.

Out of this plan: the Boundary dialog (a Boundary change request against `vault.sh requests --json` and `vault.sh grant`), and the bitwarden.com organization check, which needs an account Keith creates.

---

### Task 1: Manifest validator: `credentials` and `vaultBackend`

Check main first: the "Add-on manifest spec roadmap" session works in the same file. If `credentials` or `vaultBackend` already landed, rebase and do only what is missing.

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`READ_CLASSES` line 41, `validate()`, `main()`)
- Test: `scripts/test-addon-manifests.py`
- Modify: `docs/private/addon-manifest-spec.md` in the main checkout (gitignored, so edit it in place and don't commit it): add the class and the block

**Interfaces:**
- Produces: `validate()` accepts a top-level `vaultBackend` object. CLI `python3 addon_manifest.py vault-plan <addons-dir> <profile>` prints one line per add-on with a `vaultBackend`: `name|enabled|access|plugin|tools`, where `enabled` is `1`/`0` from the builtin flag, `access` is `rw`, `ro` or empty, `plugin` is the repo-relative directory and `tools` is space-separated. Same skip-with-warning rules as `mcp-plan`.

- [ ] **Step 1: Write the failing tests** in `test-addon-manifests.py`, using its existing case helpers:
  - `credentials_cron_refused`: `reads: ["credentials"]`, `cron: true` → error contains `needs permissions.cron false`.
  - `credentials_api_server_refused`: a `vaultBackend` with `profiles: {"api-server": "rw"}` and `reads: ["credentials"]` → error contains `api-server`.
  - `vault_backend_reserved_name`: `name` in `vault`, `op`, `bw` → error `vaultBackend.name is reserved`.
  - `vault_backend_community_refused`: source `community` → error `vaultBackend is refused unless the add-on is built-in or on the official feed`.
  - `vault_backend_shape`: missing `plugin`, a non-list `tools`, a profile value other than `rw`/`ro` → one error each.
  - `vault_plan_lines`: a temp addons dir with one valid manifest and `BITWARDEN_ENABLED=1` → `["bwbroker|1|rw|compose/hermes/plugins/bwbroker|bitwarden_request_access bitwarden_release_access"]` for profile `default`, access empty for `api-server`.
- [ ] **Step 2: Run** `python3 scripts/test-addon-manifests.py`. Expected: FAIL on the new cases.
- [ ] **Step 3: Implement.** Add `"credentials"` to `READ_CLASSES`. Add `_validate_vault_backend(vb, source, reads) -> list[str]` next to `_validate_mcp`, reusing its profile rule (with `reads`, no `api-server` key). `name` must match `[a-z][a-z0-9_]*`. Add `vault_plan(addons_dir, profile, environ=os.environ) -> list[str]` modelled on `mcp_plan`, and the `vault-plan` branch in `main()`.
- [ ] **Step 4: Run** the test file. Expected: `ok`.
- [ ] **Step 5: Commit** `Add credentials read class and vaultBackend to add-on manifests`.

### Task 2: Digest redaction

**Files:**
- Create: `compose/bitwarden-broker/broker/digest_redaction.py`
- Create: `compose/hermes/patch-digest-redaction.py`
- Modify: `compose/hermes/05-patch-tool-calling.sh`, `docker-compose.yml` (two read-only mounts in the `hermes` service, next to `blocked_fetch_hint.py`)
- Test: `scripts/test-digest-redaction.py`

**Interfaces:**
- Produces (module, stdlib only):
  - `FORMS = ("raw", "base64", "hex", "percent", "reversed")`
  - `build_entry(value: str) -> dict`, which returns `{"key": <b64 32 bytes>, "base": int, "forms": [{"len": int, "fp": int, "mac": <hex>}]}` with one form per `FORMS` item, built over the value and its CR/LF-stripped form. The broker calls it.
  - `scrub(text: str, entries: list[dict], marker: str) -> str`, which the gateway calls.
- Produces (overlay): `agent.redact.register_vault_redaction_digest(entry: dict) -> None`. Entries are stored per `_vault_scope()` under the same lock and per-profile cap as core's values. `redact_registered_vault_values` calls `scrub` after core's exact pass, and `clear_vault_redaction_values` clears digests too.

Encodings: UTF-8 bytes of the value. `base64.b64encode` (standard, padded). Lowercase hex. `urllib.parse.quote(value, safe="")`. Reversed is the reversed string's UTF-8 bytes. Fingerprints run over UTF-8 bytes with modulus `2**61 - 1`. The polynomial is `fp(w) = sum(w[i] * base**(n-1-i)) mod M`, and `base` is drawn per entry from `secrets.randbelow(M - 256) + 256`. Matches compare `hmac.compare_digest(hmac.new(key, window, sha256).hexdigest(), mac)`. A matched byte window is a whole UTF-8 encoding, so replacing it with the marker leaves valid UTF-8. Take the marker from core's exact pass (read it in `agent/redact.py` and name it in `NEEDED`).

- [ ] **Step 1: Write the failing tests** (host part, stdlib):
  - `test_each_form_scrubbed`: for a generated 24-char password, each of the five forms embedded at the start, middle and end of 1 kB and 200 kB random text is replaced by the marker; the text is otherwise unchanged.
  - `test_no_false_positives`: 200 kB of random printable text with ten unrelated entries → unchanged.
  - `test_unicode_and_newline_values` (Review Focus 3): values `"pässwörd+/%x"` and `"line1\nline2"` → every form scrubbed, including the CR/LF-stripped raw form.
  - `test_timing`: print the 200 kB, ten-entry scrub time in ms. It always passes; the number goes in the output for the record.
  - `test_overlay_is_wired`: `05-patch-tool-calling.sh` runs `patch-digest-redaction.py`; both files are mounted `:ro`.
  - `IMAGE_DRIVER`, through `hermes_image.maybe_run`: apply twice (second prints `already applied`); register an entry; `redact_registered_vault_values` scrubs base64 of the value; `clear_vault_redaction_values` empties it; the exact pass still works for a plain registered value.
- [ ] **Step 2: Run** `python3 scripts/test-digest-redaction.py`. Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** the module, then the overlay, which copies the module to `/opt/hermes/agent/digest_redaction.py` and anchors on `def redact_registered_vault_values` and `def clear_vault_redaction_values`.
- [ ] **Step 4: Run** the test file, with the image present. Expected: `ok` then `image ok`.
- [ ] **Step 5: Commit** `Scrub filled values from model output by digest`.

### Task 3: Broker accounts, `bw` wrapper and item listing

**Files:**
- Create: `compose/bitwarden-broker/broker/{__init__,accounts,bwcli,vault}.py`
- Create: `scripts/fixtures/fake-bw.py`
- Test: `scripts/test-bitwarden-broker.py`

**Interfaces:**
- `accounts.load_dir(path: Path) -> tuple[list[Account], list[str]]` returns the accounts and one error per refused file. `Account` is a frozen dataclass with `slug, mode, server, scope_kind ("folder"|"collection"|"all"), scope_value, unlock ("stored"|"manual"), client_id, secrets_item_scope (tuple|None), path`.
- `bwcli.Bw(account, data_root: Path, secrets_root: Path, bw_path="bw")` has these methods: `login() -> None`, `unlock(password: str | None = None) -> None`, `lock() -> None`, `sync() -> None`, `list_items() -> list[dict]`, `get_totp(item_id) -> str`, `get_password(item_id) -> str`, `state -> "unlocked"|"locked"|"offline"|"error"`, `error_class -> str`. Each account gets its own `BITWARDENCLI_APPDATA_DIR`. The session key is held in the instance only, and every call goes through one `threading.Lock` per account. A 429 sets `retry_at` from `Retry-After`, or 60 s when the header is absent. Failures raise `BwError(error_class)`, where `error_class` is one of `auth`, `locked`, `network`, `rate_limited`, `unknown` and never carries `bw`'s own text.
- `vault.Vault(bw_by_slug, denylist: Denylist)`, methods `items() -> list[ItemMeta]`, `get(handle) -> ItemMeta | None`. `ItemMeta` is `{handle, label, username, origins: tuple[str], has_totp, account, item_id}`. The handle is `bwbroker:<slug>/<item id>`. Denylisted handles are neither listed nor returned by `get`.
- `vault.Denylist(path: Path)` has `add(handle)`, `remove(handle)` and `__contains__`. It persists to `broker/denylist.json` atomically (temp file plus `os.replace`), mode 600.
- `fake-bw.py` reads `FAKE_BW_VAULT` (JSON: items, folders, collections, `master_password`, `rate_limit_after`) and `FAKE_BW_LOG` (where it appends argv with secrets masked). It implements `config server`, `login --apikey`, `unlock --passwordenv --raw`, `lock`, `logout`, `sync`, `list items|folders|org-collections`, `get item|password|totp`, `create folder|org-collection|item`, `edit item|item-collections|org-collection`, `move`, `confirm org-member`. Exit codes and stderr follow `bw`'s shape.

- [ ] **Step 1: Write the failing tests:**
  - `test_account_file_tolerance` (Review Focus 2): CRLF, double-quoted values, trailing spaces and comments load. `BW_UNLOCK=auto`, an unknown key, a slug with `_`, `BW_SERVER=http://…` and a missing `BW_CLIENTID` each refuse that file with the file name and key in the error, while the other accounts still load.
  - `test_scope_filtering`: for `folder:Agent`, only items in folder Agent; for `collection:<id>`, only items in that collection; `all` → everything.
  - `test_mixed_uris` (Review Focus 1).
  - `test_no_uri_items_hidden`: items with no http(s) URI are not listed.
  - `test_429_backoff`: the fake returns 429 with `Retry-After: 120`; the next `sync()` within 120 s does not run `bw` (check `FAKE_BW_LOG`), and items are still served from the last list.
  - `test_errors_carry_no_text`: the fake writes the synthetic password into its stderr on a failed unlock; `BwError` and its `str()` don't contain it.
  - `test_manual_unlock_and_lock`: a `manual` account starts `locked`; `unlock(pw)` → `unlocked`; `lock()` → `locked`, and its items vanish from `Vault.items()`.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-broker.py`. Expected: FAIL.
- [ ] **Step 3: Implement** the three modules and the fake. `bw` gets the password through `--passwordenv BW_PW` in a child environment built per call, never through argv. `stored` reads `secrets/<slug>/master_password` at unlock time and drops it after.
- [ ] **Step 4: Run** the test file. Expected: `ok`.
- [ ] **Step 5: Commit** `Add broker account loading, bw wrapper and item listing`.

### Task 4: Broker fill over CDP

**Files:**
- Create: `compose/bitwarden-broker/broker/cdp.py`
- Test: extend `scripts/test-bitwarden-broker.py` (a fake CDP server built on `websockets.serve` and `http.server`), plus an image check in Task 8

**Interfaces:**
- `cdp.fill(port: int, target_id: str, allowed_origins: tuple[str], value: str, timeout=10.0) -> FillResult`. `FillResult` is `{filled: bool, origin: str, error: str | None}`, and `error` is one of these fixed texts:
  - `target_not_found`
  - `origin_mismatch`
  - `no_field`
  - `page_error`
  - `cdp_unavailable`

  The function reads `/json/list` on `127.0.0.1:<port>`, finds the target, normalizes its origin the way core's `normalize_origin` does (copy the rules into a function, with a test against core's results in Task 8), and connects to `webSocketDebuggerUrl` from `/json/version`. It then attaches flattened and evaluates the fill expression. The value goes in through `Runtime.callFunctionOn` arguments, not string concatenation, so it never appears in a script. It detaches on every path.
- The fill expression copies core's deep probe (`agent/vault_login_classifier.build_fill_js` behaviour): find `input[type=password]` through shadow roots, set `value`, dispatch `input` and `change` with `bubbles: true`, and check `location.origin` again inside the page before writing (`origin_changed` → `origin_mismatch`).

- [ ] **Step 1: Write the failing tests** against the fake CDP server:
  - `test_fill_writes_value`: the fake records the `callFunctionOn` arguments; the value arrives as an argument and appears in no `expression`/`functionDeclaration` field.
  - `test_origin_refused`: target URL `https://evil.example.com/login`, allowed `https://login.example.com` → `origin_mismatch`, no `callFunctionOn` sent.
  - `test_target_navigated` (Review Focus 5): `/json/list` says the allowed origin but the in-page origin check reports a different one → `origin_mismatch`.
  - `test_target_closed` (Review Focus 5): the target is absent from `/json/list` → `target_not_found`.
  - `test_no_value_in_errors`: a page exception whose message contains the value → `page_error`, and the value is in no result or log line.
- [ ] **Step 2: Run** the test file. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement** `cdp.py`.
- [ ] **Step 4: Run** the test file. Expected: `ok`.
- [ ] **Step 5: Commit** `Fill logins from the broker over CDP`.

### Task 5: Broker HTTP API, access requests and release

**Files:**
- Create: `compose/bitwarden-broker/broker/{requests_store,server}.py`
- Test: `scripts/test-bitwarden-access.py`, and extend `scripts/test-bitwarden-broker.py` for the API

**Interfaces:**
- `server.main()` reads these environment variables:
  - `BITWARDEN_ACCOUNTS_DIR`
  - `BITWARDEN_DATA_DIR`
  - `BITWARDEN_SECRETS_DIR`
  - `BITWARDEN_SYNC_MINUTES` (default 10)
  - `BITWARDEN_IDLE_LOCK_MINUTES` (default 0)
  - `BITWARDEN_LISTEN` (default `127.0.0.1:8099`)
  - `BITWARDEN_SYNC_SOCKET` (default `/run/bitwarden-sync/sync.sock`)

  It serves the spec's API table exactly. `POST /fill {handle, target_id, port, field}` returns `{filled, origin, redaction}`, where `redaction` is `digest_redaction.build_entry(value)` (Task 2 module, already in the broker package). `port` comes from the caller and is used only to connect to `127.0.0.1`.
- `requests_store.Store(path)` has these methods:
  - `add(account, site, reason) -> str`: raises `RequestError` with `invalid_site`, `too_many` or `unknown_account`.
  - `pending() -> list`
  - `recent() -> list`
  - `mark_done(site)`
  - `expire(now)`

  The cap is 10 pending and expiry is 7 days. `reason` is cut to 200 characters after removing Unicode category `C*` characters. Writes are atomic (temp file plus `os.replace`) and the file is mode 600.
- `POST /release {handle}`: adds to the denylist first, then for `folder:` runs `bw edit item` with `folderId: null`, and for `collection:` runs `bw edit item-collections <id> []`. It returns `{released: true, returned: bool}`. If the edit fails, `returned` is false and the entry stays.
- Unix socket `GET /secrets` (HTTP over the socket): returns `[{name, value}]` for items in each account's `BW_SECRETS_ITEM_SCOPE` only. This is the one path that returns values, and it is never bound on TCP.

- [ ] **Step 1: Write the failing tests:**
  - `test_api_returns_no_values`: start `server.main` on a free port with the fake `bw`, call every TCP endpoint, then search every response body and the captured stderr for each synthetic password and TOTP seed. None found.
  - `test_request_validation`: `site="not a host!"` → 400 `invalid_site`; a 500-char reason with `\x1b[31m` → stored as 200 chars without control characters.
  - `test_request_cap_and_expiry`: the 11th pending request → 429-style JSON `too_many`; a request dated 8 days ago is gone after `expire`.
  - `test_request_reveals_nothing`: a request for a site that has an item outside scope, and one that has none, get byte-identical responses apart from `request_id`.
  - `test_release_before_move`: with the fake's `edit` made to block, `/items` and `/fill` refuse the handle while the move is still running.
  - `test_release_paths`: folder → `folderId` null in the fake vault; collection → empty `collectionIds`; the fake refusing → `returned: false`, still denylisted.
  - `test_regrant_clears_denylist`: after release, the fake moves the item back into scope and `/sync` lists it again.
  - `test_restart_keeps_requests_and_denylist` (Review Focus 4): stop and restart `server.main`; pending requests and the denylist are intact; a `stored` account is `unlocked` again.
  - `test_sync_socket_scope`: the socket serves only `BW_SECRETS_ITEM_SCOPE` items; TCP `/secrets` → 404.
- [ ] **Step 2: Run** both test files. Expected: FAIL.
- [ ] **Step 3: Implement.** Use `http.server.ThreadingHTTPServer` with a `BaseHTTPRequestHandler` whose `log_message` writes only method, path, status and handle. Run a sync thread every `BITWARDEN_SYNC_MINUTES`, plus an idle-lock check for `manual` accounts. `POST /sync` marks pending requests done for any host now in scope.
- [ ] **Step 4: Run** both test files. Expected: `ok`.
- [ ] **Step 5: Commit** `Serve the broker API with access requests and release`.

### Task 6: Broker image and compose services

**Files:**
- Create: `compose/bitwarden-broker/Dockerfile`, `compose/bitwarden-broker/entrypoint.sh`
- Modify: `docker-compose.yml` (services `bitwarden-broker`, `bitwarden-secrets-sync`, `bitwarden-setup`; volume `bitwarden_sync`; network `bitwarden-setup`), `.gitignore` (`compose/bitwarden/accounts/`, `data/addons/`)
- Modify: `.github/workflows/ci.yml` (add `core,bitwarden` to the profile loop)
- Test: `scripts/test-compose-env-allowlist.py` (extend), `scripts/test-bitwarden-compose-config.py` (new)

**Interfaces:**
- **`bitwarden-broker`**
  - Profile `bitwarden`, `network_mode: "service:hermes"`.
  - `user: "10001:10001"`, `read_only: true`, `tmpfs: /tmp`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
  - Mounts: accounts dir `:ro`, `data/addons/bitwarden/bw` rw, `secrets` `:ro`, `broker` rw, `bitwarden_sync:/run/bitwarden-sync`, and the local CA file `:ro`.
  - Environment: an allowlist of the `BITWARDEN_*` keys from Task 5, plus `NODE_EXTRA_CA_CERTS=/run/secrets/bitwarden-local-ca.crt`.
  - Healthcheck: `python3 -c` GET `/health`.
- **`bitwarden-secrets-sync`**
  - Profile `bitwarden-tools`, same image, entrypoint `python3 /app/secrets_sync.py`.
  - Mounts `bitwarden_sync` plus the target folders listed in Task 9.
- **`bitwarden-setup`**
  - Profile `bitwarden-tools`, same image, entrypoint `python3 /app/setup/setup.py`.
  - Networks `bitwarden-setup` plus `vault`; mounts `secrets` rw and the accounts dir rw; no `hermes` mounts.
- The Dockerfile installs `bw` from `https://github.com/bitwarden/clients/releases/download/cli-v${BW_CLI_VERSION}/bw-linux-${arch}-${BW_CLI_VERSION}.zip`, verifies it with `sha256sum -c` against the `ARG BW_SHA256_ARM64` and `ARG BW_SHA256_X64` defaults, and runs `bw --version` at build.

- [ ] **Step 1: Write the failing tests.** `test-bitwarden-compose-config.py`:
  - With `COMPOSE_PROFILES=core`, the output has none of the three services.
  - With `core,bitwarden`, `bitwarden-broker` has `network_mode: service:hermes`, no `ports`, `read_only: true` and `cap_drop: [ALL]`.
  - The `hermes` service mounts nothing under `data/addons/bitwarden` and no `bitwarden_sync`.
  - `bitwarden-setup` does not share a network with `hermes` except `vault`.

  Extend the allowlist test so the broker's environment keys are exactly the listed ones.
- [ ] **Step 2: Run** both tests. Expected: FAIL.
- [ ] **Step 3: Implement** the Dockerfile, entrypoint (exec `python3 -m broker.server`) and compose entries. Before choosing 8099, check that nothing in `hermes` already listens there: run `docker compose exec hermes python3 -c` with a socket bind test; that reads no data.
- [ ] **Step 4: Run** the tests, then `docker compose --profile bitwarden build bitwarden-broker`. Expected: tests `ok`, and the build prints `2026.10.1`.
- [ ] **Step 5: Commit** `Add the Bitwarden broker image and services`.

### Task 7: Hermes plugin and bootstrap registration

**Files:**
- Create: `compose/hermes/plugins/bwbroker/plugin.yaml`, `compose/hermes/plugins/bwbroker/__init__.py`, `compose/hermes/bitwarden_broker_backend.py`
- Modify: `compose/hermes/bootstrap-api-profile.sh` (next to the mcp-plan code at line 1358), `docker-compose.yml` (mount plugin dir and backend `:ro` under `/bootstrap/`)
- Test: `scripts/test-bitwarden-registration.sh`

**Interfaces:**
- `BrokerLoginBackend(LoginBackend)` sets `name = "bwbroker"`, `prefix = "bwbroker:"`, `display_name = "Bitwarden"`, and `needs_unlock = False` as plain class attributes, because the registry rejects descriptors.
  - `list_items()` builds `VaultItemMeta(kind="login", allowed_origins=…, has_otp=…, identifier=username, identifier_type="username")` from `GET /items`.
  - `get_meta(handle)` returns the same metadata for one handle.
  - `resolve_password` and `resolve_otp` raise `RuntimeError("bwbroker never returns values")`.
  - `fill_context()` returns `contextlib.nullcontext()`.
  - The broker URL is `http://127.0.0.1:8099`, with a 5 s timeout.
- Tools, registered with toolset `browser`:
  - `bitwarden_request_access(site: str, reason: str, account: str = "")` calls `POST /requests` and returns `{pending: true, request_id, approve: "make vault-grant SITE=<site>, or move the login into the agent's folder or collection in Bitwarden"}`.
  - `bitwarden_release_access(handle: str)` calls `POST /release`.
- Bootstrap, per profile, from `vault-plan` (Task 1):
  - When a line's `enabled` is `1` and `access` is non-empty, it copies the plugin dir to `$HERMES_HOME/plugins/bwbroker/`, adds `bwbroker` to `plugins.enabled` and sets `vault.bwbroker.enabled: true`.
  - Otherwise it removes them.
  - Whenever any line is enabled, it writes `vault.bitwarden.enabled: false` on every profile.
  - It uses the existing YAML helpers in `scripts/lib/hermes-yaml.sh`.

- [ ] **Step 1: Write the failing test** (shell, following `scripts/test-gptr-mcp-registration.sh`):
  - The flag off leaves every profile config byte-identical to the baseline.
  - The flag on installs the plugin on `default` and `browser` only.
  - `api-server` gets no plugin and no tools, while `vault.bitwarden.enabled: false` is set on all three.
  - No cron toolset lists either tool.
  - Turning the flag off again removes the plugin.
- [ ] **Step 2: Run** `sh scripts/test-bitwarden-registration.sh`. Expected: FAIL.
- [ ] **Step 3: Implement** the plugin, backend and bootstrap section.
- [ ] **Step 4: Run** the test. Expected: `ok`.
- [ ] **Step 5: Commit** `Register the Bitwarden broker backend on default and browser`.

### Task 8: Fill overlay

**Files:**
- Create: `compose/hermes/patch-bitwarden-broker-fill.py`
- Modify: `compose/hermes/05-patch-tool-calling.sh`, `docker-compose.yml` (mount)
- Test: `scripts/test-bitwarden-fill.py`

**Interfaces:**
- **Fill.** In `tools/browser_vault_tool.py`, at the `return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id)` call (line 512), a handle starting with `bwbroker:` goes to `_bwbroker_fill(handle, meta, page_origin, effective_task_id, field="password")` instead. Inserted helper:
  1. `supervisor = _ensure_supervisor(task_id)`, then `supervisor.focus_page(page_origin)`.
  2. Get the target id by sending `Target.getTargetInfo` on `supervisor._page_session_id`, scheduled on the supervisor loop the way `evaluate_runtime` does, and the port from the session's `cdp_url`.
  3. `POST /fill`.
  4. `register_vault_redaction_digest(result["redaction"])`.
  5. Return JSON shaped like core's success result, `{"success": true, "filled": ["password"], "origin": …}`, or core-shaped errors (`origin_mismatch` and the rest).
- **Codes.** `browser_vault_enter_code` routes `bwbroker:` handles with `field="totp"` when `meta.has_otp`, else falls through to core.
- **Save.** `browser_vault_save_login` returns `{"success": false, "error": "Bitwarden logins are added in Bitwarden. Ask the user to add this login to the agent's folder or collection, or to approve a request: bitwarden_request_access."}` when every enabled backend is `bwbroker`.
- **List.** `browser_vault_list` adds `"bitwarden": {"locked": [slug…], "unlock": "make vault-unlock SLUG=<slug>"}` from `GET /accounts`, or `{"unavailable": true}` when the broker does not answer.

- [ ] **Step 1: Write the failing tests:**
  - Host: `test_overlay_is_wired`.
  - `IMAGE_DRIVER`:
    - The patch applies twice.
    - A stub broker (`http.server` on 8099 inside the container) and a real headless Chromium launched as in spike 1 show that a `bwbroker:` fill reaches the stub with the right `target_id` and `port`, and that `PluginLoginBackend.resolve_password` is never called (patched to raise and record).
    - The redaction digest is registered.
    - A `vault_` handle still takes core's path.
  - `test_broker_down_result` (Review Focus 4): with the stub stopped, fill returns `{"success": false, "error": "Bitwarden is unavailable right now. …"}` and list carries `unavailable`.
  - `test_totp_fallback`: `has_otp` false → core's ask-the-user path.
  - `test_save_refused`.
  - `test_origin_normalization_matches_core`: run the broker's `normalize_origin` copy and core's over a table of URLs (ports, IDN, trailing dots, `http` vs `https`, userinfo); results equal.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-fill.py`. Expected: FAIL.
- [ ] **Step 3: Implement** the overlay, with `NEEDED` naming `_ensure_supervisor`, `focus_page`, `_page_session_id` and the line-512 call.
- [ ] **Step 4: Run** the test with the image. Expected: `ok`, `image ok`.
- [ ] **Step 5: Commit** `Route Bitwarden fills to the broker`.

### Task 9: Stack secrets sync

**Files:**
- Create: `compose/bitwarden-broker/secrets_sync.py`
- Modify: `docker-compose.yml` (`bitwarden-secrets-sync` mounts: `compose/twenty/workspaces`, the Twenty `*_FILE` directory, `compose/postgres/services`, `compose/caldav-mcp/accounts`, `llm-proxy` key file directory, all rw)
- Test: `scripts/test-bitwarden-secrets-sync.sh`

**Interfaces:**
- `secrets_sync.plan(items: list[{name, value}], roots: dict[str, Path]) -> tuple[list[Write], list[str]]`. `Write` is `(path, key | None, value)`, and the second element holds the problems.
- `main() -> int` gets items from the sync socket, applies the writes, and prints one line per changed file (`changed compose/twenty/workspaces/main.env`), never values. It exits 0, or 3 when any Hermes-side file changed; `vault.sh secrets-sync` runs `make hermes-reload` on 3.
- The spec's mapping table, with the `*_API_KEY_FILE` path per `llm-proxy` route taken from the route's env in `docker-compose.yml`.

- [ ] **Step 1: Write the failing test.** It runs the service image with a fake socket server serving synthetic items into a temp tree, and checks:
  - Every row of the mapping table.
  - An unknown name is reported and skipped.
  - A name mapping to two files is refused.
  - Mode 600 is kept.
  - An unchanged value leaves the mtime unchanged.
  - The output contains no value.
- [ ] **Step 2: Run** it. Expected: FAIL.
- [ ] **Step 3: Implement.** `KEY=value` files: edit only the matching line, keep all other lines and order, then write atomically.
- [ ] **Step 4: Run** it. Expected: `ok`.
- [ ] **Step 5: Commit** `Sync mapped Bitwarden items into stack secret files`.

### Task 10: `bitwarden-setup`

**Files:**
- Create: `compose/bitwarden-broker/setup/setup.py`, `compose/bitwarden-broker/setup/vw_register.py` (`token()` only)
- Test: `scripts/test-bitwarden-setup.sh`

**Interfaces:**
- CLI, run with `docker compose run --rm -it bitwarden-setup <command>`:
  - `create --slug S`: creates the folder, or the collection with the agent member's access.
  - `seed --slug S`: interactive site prompts.
  - `grant --slug S [--site H] [--item ID]`
  - `role --slug local`: Local mode step 6.
  - `list-unassigned --slug S`
- Each command reads the user's email and server from flags written by `vault.sh`, takes the master password through `getpass` (three tries, then exit 2 with nothing changed), unlocks into `tempfile.mkdtemp(dir="/tmp")`, and runs `bw lock` then `bw logout` in a `finally` that also runs on `SIGINT`/`SIGTERM` (install handlers that raise `KeyboardInterrupt`).
- `match_items(items, host) -> list` matches when the URI host equals `host` or ends with `.` + `host`. `vault-grant` uses the same function.
- Org API calls (invite, member edit) use `urllib`. They authenticate with a password-grant token (`POST /identity/connect/token`, master hash as in spike 2, via `vw_register.token(base, email, password) -> str`, which this task creates in `compose/bitwarden-broker/setup/vw_register.py` and Task 11 extends) held in memory only. If the server refuses the grant (bitwarden.com may ask for new-device verification), setup prints the web vault steps for that action and carries on with the rest. The calls are `POST /api/organizations/<id>/users/invite` and `PUT /api/organizations/<id>/users/<member>`, with payloads as in spike 2: invite `{emails, groups: [], type, collections: [], permissions: {}}`; edit `{type: 2, groups: [], permissions: {}, collections: [{id, readOnly, hidePasswords, manage}]}`.
- Before the first `bw move` in a run it prints this text: `Moving an item into an organization collection transfers it from your personal vault to the organization. Members with access to the collection can see it.`

- [ ] **Step 1: Write the failing test** with the fake `bw` (Task 3) and a fake org API server. Cases:
  - Per-mode creation.
  - Seeding by host, with `linkedin.com` matching `www.linkedin.com` but not `notlinkedin.com`.
  - The transfer notice printed once, before the first move.
  - Three wrong passwords → exit 2 and no `create`, `edit` or `move` in `FAKE_BW_LOG`.
  - `lock` and `logout` in `FAKE_BW_LOG` on success, on failure, and after `SIGINT` mid-run.
  - The temporary directory removed.
- [ ] **Step 2: Run** `sh scripts/test-bitwarden-setup.sh`. Expected: FAIL.
- [ ] **Step 3: Implement** `setup.py`.
- [ ] **Step 4: Run** it. Expected: `ok`.
- [ ] **Step 5: Commit** `Add bitwarden-setup for creating, seeding and granting`.

### Task 11: Local mode

**Files:**
- Modify: `compose/bitwarden-broker/setup/vw_register.py`
- Create: `compose/bitwarden-tls/default.conf.template`
- Modify: `docker-compose.yml` (services `vaultwarden` and `vaultwarden-tls`, profile `vault`; network `vault: {internal: true}`), `docker-compose.hermes-terminal.yml` (hermes joins `vault`), `scripts/sync-profiles.sh` (`BITWARDEN_LOCAL_ENABLED vault 0` in `SERVICES_CORE`)
- Test: `scripts/test-vw-register.py`

**Interfaces:**
- Consumes: `vw_register.token` from Task 10.
- `vw_register.register(base: str, email: str, password: str, ca: Path) -> Keys`, `create_org(base, token, keys, name, first_collection) -> (org_id, collection_id)`, `api_key(base, token, email, password) -> (client_id, client_secret)`, `admin_invite(base, admin_token, email)`.
  - Crypto as in spike 2: PBKDF2-SHA256 600,000 iterations over the lower-cased email; master hash is one PBKDF2 round of the key over the password; HKDF-Expand SHA-256 `enc`/`mac`; type-2 EncStrings; RSA-2048 SPKI/PKCS#8; org key wrapped `4.` RSA-OAEP-SHA1.
  - Use Python's `cryptography` package from Debian (`python3-cryptography`) in the broker image.
- **`vaultwarden`**: no `ports` except `127.0.0.1:${VAULTWARDEN_PORT:-8222}:80`. Environment: `SIGNUPS_ALLOWED=false`, `INVITATIONS_ALLOWED=true`, `DOMAIN=http://localhost:${VAULTWARDEN_PORT:-8222}`, and `ADMIN_TOKEN` from a file through the entrypoint, Argon2-hashed by `vault.sh init`. Data at `data/addons/bitwarden/vaultwarden`. Networks: `vault` plus the default network for the host port.
- **`vaultwarden-tls`**: listens on 443 on `vault` with alias `vaultwarden.vault.internal`, proxies to `http://vaultwarden:80`, and uses the cert and key from `data/addons/bitwarden/secrets/local/tls/`.

- [ ] **Step 1: Write the failing test.** It starts a scratch Vaultwarden and TLS sidecar with a generated CA on a throwaway network and repeats spike 2's sequence with `vw_register` and the real `bw` in the broker image:
  - Uninvited registration is refused.
  - Admin invite, registration, `bw login --apikey`, `bw unlock`.
  - Org and collections.
  - User invite and registration.
  - Confirm, demote.
  - The agent lists only "Agent".
  - Release by empty `item-collections` succeeds.
  - The owner lists the unassigned item through `bw list items --organizationid`. This closes the spec's open check.

  Skip with a message when Docker is unavailable.
- [ ] **Step 2: Run** `python3 scripts/test-vw-register.py`. Expected: FAIL.
- [ ] **Step 3: Implement** `vw_register.py`, the TLS template and the compose entries.
- [ ] **Step 4: Run** it. Expected: `ok`, with each step named.
- [ ] **Step 5: Commit** `Add the local Vaultwarden mode with a TLS sidecar`.

### Task 12: `scripts/vault.sh` and make targets

**Files:**
- Create: `scripts/vault.sh`
- Modify: `Makefile` (targets next to `crm-*`, with a usage comment block in the same style), `scripts/doctor.sh`
- Test: `scripts/test-vault-sh.sh`

**Interfaces:**
- **Subcommands.**
  - `init [--owner EMAIL]` runs Local mode steps 1–6 using `vw_register` through `docker compose run bitwarden-setup` and is re-runnable. It reads state from `secrets/local/state`, which records the step reached.
  - `add --slug S --mode cloud|selfhosted [--region eu] [--url U] [--scope …] [--unlock stored|manual]`
  - `unlock --slug S`
  - `lock --slug S`
  - `sync`
  - `grant [--site H]`
  - `requests [--json]`
  - `remove --slug S`
  - `open`
  - `secrets-sync`
- **Make targets**: `vault-init`, `vault-add`, `vault-unlock`, `vault-lock`, `vault-sync`, `vault-grant`, `vault-requests`, `vault-remove`, `vault-open` and `secrets-sync` map onto these, passing `SLUG`, `MODE`, `REGION`, `URL`, `SCOPE`, `UNLOCK`, `SITE`.
- **`add`.** Prompts use `read -rs`. `add` refuses `--scope all` unless `SCOPE=all` was given literally. Before writing a `stored` password for a `folder:` scope it prints the warning from the spec's Unlock section.
- **Passwords never in argv.** `unlock` passes the password to `docker compose exec -T bitwarden-broker python3 -m broker.unlock <slug>` on stdin.
- **`doctor.sh`.** Checks the broker's health and lists each account's state and `error_class`. It warns while any account uses `all`. When `BITWARDEN_LOCAL_ENABLED=0` and a `local` account file exists, it notes that the local account is offline.
- **`make up`.** After the stack starts, when any account file sets `BW_SECRETS_ITEM_SCOPE`, `make up` runs `vault.sh secrets-sync` if the broker reports that account `unlocked`. Otherwise it prints `Bitwarden secrets sync skipped: <slug> is locked`.
- **`add` and setup.** When the folder or collection in `--scope` does not exist, `add` offers to run `bitwarden-setup create` and then `seed`.
- **Backup hooks.** `hooks.backup` (`sqlite3 .backup` of `db.sqlite3` into the backup dir, plus `attachments/`, `rsa_key*` and `secrets/`) is a `vault.sh backup <dir>`; `restore` mirrors it.

- [ ] **Step 1: Write the failing test.** Bash 3.2-compatible, with `docker` replaced by a recording stub on `PATH`, covering:
  - The argument validation messages.
  - `add` writes a mode 600 account file and secret files, and argv in the stub log never contains the password.
  - `--scope all` refused without `SCOPE=all`.
  - `requests --json` is valid JSON from the stub.
  - `init` resumes from the recorded step.
- [ ] **Step 2: Run** `bash scripts/test-vault-sh.sh` and `sh scripts/test-bash32.sh`. Expected: FAIL.
- [ ] **Step 3: Implement** `vault.sh`, the make targets and the doctor checks. Add `vault.sh` to `test-bash32.sh` and `test-no-host-python.sh` coverage as `crm.sh` is.
- [ ] **Step 4: Run** both. Expected: `ok`.
- [ ] **Step 5: Commit** `Add make vault-* commands`.

### Task 13: Manifest, docs, skill and tool-evals

**Files:**
- Create: `addons/bitwarden/addon.json`, `docs/bitwarden.md`
- Modify: `SECURITY.md`, `docs/hermes.md` (patch table, `bwbroker` next to "Saving logins"), `compose/hermes/skills/browser-login/SKILL.md`, `CHANGELOG.md` (Unreleased), `README.md` (docs index line), `eval/` (three cases), `Makefile` (`bitwarden-test` target running every test from Tasks 2–12), `.github/workflows/ci.yml` (the host-only tests)
- Test: `scripts/test-addon-manifests.py` (the canonical check covers the new file)

**Interfaces:**
- `addon.json`, copied from the spec's "What validates today":
  - `id`: `io.github.kaxline.stack.bitwarden`.
  - `minStackVersion`: the version this releases in.
  - `vaultBackend` exactly as the spec's Changes item 2, with `plugin: "compose/hermes/plugins/bwbroker"`.
  - Written with `json.dumps(indent=2, ensure_ascii=False) + "\n"`.
- Tool-eval cases are added in the format of the existing cases in `eval/`: `browser-login-bitwarden`, `bitwarden-request-not-move` and `bitwarden-injected-request`, with the expectations from the spec's Testing section.
- `docs/bitwarden.md` covers every item in the spec's Documentation section, including the final paragraph on what agent code can read, stated as the spec states it.

- [ ] **Step 1: Run** `python3 scripts/test-addon-manifests.py` with the new manifest in place before writing it canonically. Expected: FAIL on canonical layout.
- [ ] **Step 2: Write** the manifest canonically and run the test again. Expected: `ok`. Run `sh scripts/test-sync-profiles.sh` to confirm the `BITWARDEN_ENABLED bitwarden 0` row is read.
- [ ] **Step 3: Write** the docs, skill text, CHANGELOG entry (new services and flags, `vault.bitwarden.enabled: false` from bootstrap, and the plugin-directory deviation) and eval cases. Check every claim against the code from Tasks 1–12.
- [ ] **Step 4: Run** `make bitwarden-test` through testq in the background, then the full suite once. Expected: all pass.
- [ ] **Step 5: Commit** `Document the Bitwarden add-on and add its manifest`.

### Task 14: Live check on a throwaway local Vaultwarden

No real accounts. This is the spec's one live check, and its result goes in the CHANGELOG.

- [ ] **Step 1:** In a scratch copy of the stack (`CONTAINER_PREFIX=bwcheck-`, a temp `DATA_ROOT`), set `BITWARDEN_ENABLED=1 BITWARDEN_LOCAL_ENABLED=1` and run `make up`. Then run `make vault-init` with `VAULT_OWNER_EMAIL=owner@example.com`, registering the owner in the web vault at `http://localhost:8222` in the built-in browser.
- [ ] **Step 2:** As the owner, add a synthetic login for `http://127.0.0.1:8765`, served from a local test page in `hermes`, into "Agent", and a synthetic `llm-proxy/test` item into "Stack secrets". Run `make vault-sync`.
- [ ] **Step 3:** In a Hermes TUI turn on `default`, ask the agent to sign in to the test page. Expected: `browser_vault_list` shows the `bwbroker:` handle, the fill succeeds, and the tool results contain no password (check with `grep` for the synthetic password in the gateway log only, not in session content).
- [ ] **Step 4:** Run `make secrets-sync` against a scratch target file. Expected: `changed …`, and the file holds the value with mode 600.
- [ ] **Step 5:** Ask the agent to release the login. Expected: the item becomes unassigned, and `make vault-grant` lists it.
- [ ] **Step 6:** Tear down the scratch stack by its `CONTAINER_PREFIX` containers and temp `DATA_ROOT`, by name, not by pattern. Record the outcome in `CHANGELOG.md` and commit `Record the Bitwarden live check`.
