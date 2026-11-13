# Bitwarden broker-side sign-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent signs in to a site with a Bitwarden login through a private Chromium in a new `bitwarden-signin` container, and only the site's session reaches the agent's browser.

**Architecture:** A dumb signer (Chromium plus a Unix-socket HTTP server) exposes page primitives. The broker owns every rule (origins, field kinds, the automatic sequence, the handover) and is the only process that decrypts items. A Hermes module and a small overlay add seven `browser_vault` actions that call the broker's new `/signin/*` routes.

**Tech Stack:** Python 3 stdlib plus `websockets` (signer: the Hermes image's venv; broker: as today), Chromium from the Hermes image, Docker Compose, bash (`vault.sh`, `doctor.sh`).

**Spec:** `docs/superpowers/specs/2026-11-13-bitwarden-signin-design.md`. Read it before any task; it holds the reasons this plan doesn't repeat.

## Global Constraints

- Targets stack 0.3.16. Nothing lands on `main` until 0.3.15 is tagged; `land` runs only on Keith's approval.
- No broker or signer response, log line or error text ever contains a password, TOTP code, page text or script. Errors are fixed codes.
- Fixed codes (spec, Failure codes): `busy`, `no_session`, `stale_ref`, `origin_mismatch`, `not_a_secret_field`, `no_totp`, `handover_refused`, `page_error`, `timeout`, `signer_unavailable`, `sandbox_unavailable`, plus `/fill`'s existing `account_locked`, `unknown_handle`, `unavailable`. New in this plan: `sign_in_only` (the `/fill` refusal) and `no_page` (`sign_in` with no tab of the task on the item's first origin).
- Custom field names: `agent-in-page-fill` (boolean), `agent-signin-idp` (comma-separated origins).
- Socket: `/run/bitwarden-signin/signin.sock`, mode 0600, volume `bitwarden_signin`. Network: `signin`. Service: `bitwarden-signin`, profile `bitwarden`, `user: "${HERMES_UID:-1000}:${HERMES_GID:-1000}"`, `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, tmpfs `/tmp` 512 MB, `mem_limit: 768m`.
- Idle timeout `BITWARDEN_SIGNIN_IDLE_MINUTES`, default 5. Per-action bound 30 s.
- Snapshot scrub encodings are exactly `digest_redaction._encode`'s five forms (raw, base64, hex, percent, reversed), each also with CR and LF removed; marker `«redacted-vault-secret»`.
- Chromium never runs with `--no-sandbox`. If the sandbox can't start: health `sandbox_unavailable`, sessions refused. If the seccomp profile fails on Docker Desktop or Linux, stop and ask Keith.
- Overlays follow the fork cap rule: upstream lines stay under about 50 and in `tools/browser_vault_tool.py` only; stack code lives in copied modules. Stop and ask if a task needs more.
- New test scripts go in `scripts/tests.manifest`; `make bitwarden-test` picks up `scripts/test-bitwarden-*` by prefix.
- Writing style rules apply to docs, comments and commit messages. Commits use Keith's git identity, no Claude trailers.

## Review Focus

1. **The site redirects mid-session to an origin that is neither an item nor an IdP origin** (consent page, marketing site). Actions refuse with `origin_mismatch`, `signin_cancel` still ends the session, and the agent isn't stuck until the idle timeout. Test in Task 5.
2. **The page shows the password after the fill**: a show-password toggle turns the input into `type=text`, or an error message echoes what was typed. The snapshot still contains no trace of it. Test in Task 3.
3. **The agent's tab isn't on the item's origin at `signin_finish`.** `handover_refused`, the signer session stays open so the agent can open the page and call `signin_finish` again, and the idle timeout still applies. Test in Task 5.
4. **A session left open by a crashed or abandoned turn.** Another `sign_in` gets `busy`; `signin_cancel` with no handle ends it from any caller; a broker restart ends it. Test in Task 5.
5. **Cookies on a parent domain** (`.example.com` for `app.example.com`) are handed over, and cookies on a sibling host (`other.example.com`) or an IdP are not. Test in Task 2.

---

### Task 1: Item metadata, `sign_in_only` and the `/fill` gate

**Files:**
- Modify: `compose/bitwarden-broker/broker/bwcli.py` (`_sanitize`)
- Modify: `compose/bitwarden-broker/broker/vault.py` (`ItemMeta`, `Vault._account_items`)
- Modify: `compose/bitwarden-broker/broker/server.py` (`get_items`, `post_fill`)
- Test: `scripts/test-bitwarden-broker.py`

**Interfaces:**
- Produces: `_sanitize(item)` also returns `in_page_fill: bool` (true only when a field named `agent-in-page-fill` has type 2 and value `"true"`) and `idp_origins: list[str]` (the `agent-signin-idp` value split on commas, each through `normalize_origin`, non-http(s) dropped). No other field name or value is kept.
- Produces: `ItemMeta.in_page_fill: bool`, `ItemMeta.idp_origins: tuple[str, ...]`.
- Produces: `/items` entries gain `sign_in_only: bool` (`not in_page_fill`). `/fill` on an item with `in_page_fill` false returns 409 `{"error": "sign_in_only"}` before `bw` runs.

- [ ] **Step 1: Write the failing tests** `test_sanitize_keeps_only_signin_fields` (an item with `agent-in-page-fill`, `agent-signin-idp: "https://sso.example.com, ftp://x, junk"`, and a third custom field holding `"CANARY"` → `in_page_fill is True`, `idp_origins == ["https://sso.example.com"]`, `"CANARY" not in json.dumps(result)`), `test_items_sign_in_only_flag`, `test_fill_refuses_sign_in_only` (fake `bw` records no `get password` call; reply 409 `sign_in_only`).
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-broker.py`. Expected: the three new tests fail.
- [ ] **Step 3: Implement** the produces above.
- [ ] **Step 4: Run** `python3 scripts/test-bitwarden-broker.py`. Expected: pass.
- [ ] **Step 5: Commit** "Broker: agent-in-page-fill and agent-signin-idp fields; /fill refuses sign-in-only items".

### Task 2: Signer pure parts (scrub, snapshot, cookie selection)

**Files:**
- Create: `compose/bitwarden-signin/signer/__init__.py`, `signer/scrub.py`, `signer/snapshot.py`, `signer/cookies.py`
- Create: `scripts/test-bitwarden-signer.py` (tag `ci image` in `scripts/tests.manifest`; this task adds the host part only)

**Interfaces:**
- Produces: `scrub.Scrubber` with `add(value: str) -> None`, `clear() -> None`, `apply(text: str) -> str`. `apply` replaces every occurrence of each held value in each Global Constraints encoding with the marker.
- Produces: `snapshot.reduce(ax_nodes: list[dict]) -> tuple[list[dict], dict[int, int]]`: from `Accessibility.getFullAXTree` nodes, a list of `{"ref": int, "role": str, "name": str, "text": str}` for nodes that aren't ignored, numbered from 1 in tree order, plus `ref → backendDOMNodeId`. It drops every `value` property and the `name` of `textbox`/`searchbox` nodes whose name comes from their value (`nameFrom == "contents"` or `"value"`).
- Produces: `cookies.select(cookies: list[dict], item_origins: tuple[str, ...]) -> list[dict]`: keeps a cookie when its domain (leading dot stripped) equals an item origin's host, or is a parent of it and the cookie isn't host-only. Each kept cookie is reduced to `name, value, domain, path, expires, httpOnly, secure, sameSite`.

- [ ] **Step 1: Write the failing tests**: `test_scrub_matches_digest_forms` (for a value with a CR and LF in it, the set of byte strings `Scrubber` matches equals `{digest_redaction._encode(v, f) for v in (value, value_without_crlf) for f in FORMS}`; import broker's module from `compose/bitwarden-broker`), `test_scrub_replaces_each_form`, `test_reduce_drops_values` (an AX fixture with a password textbox, a text input whose name came from its value, and a button; output has no `"CANARY"`), `test_reduce_refs_stable_order`, `test_select_parent_domain_and_excludes_sibling_and_idp` (Review Focus 5: `.example.com` kept for `https://app.example.com`, `other.example.com` and `sso.idp.test` dropped, a host-only `example.com` cookie dropped).
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-signer.py`. Expected: fail (no module).
- [ ] **Step 3: Implement** the three modules. `scrub.py` keeps its own copy of the five encodings; the first test keeps them equal to the broker's.
- [ ] **Step 4: Run** `python3 scripts/test-bitwarden-signer.py`. Expected: pass.
- [ ] **Step 5: Commit** "Signer: snapshot reduction, scrub and cookie selection".

### Task 3: Signer browser session and socket server

**Files:**
- Create: `compose/bitwarden-signin/signer/browser.py`, `signer/session.py`, `signer/server.py`, `signer/__main__.py`
- Test: `scripts/test-bitwarden-signer.py` (image part, run in the Hermes image through `hermes_image.maybe_run` with a local test site the driver starts)

**Interfaces:**
- Consumes: Task 2's `Scrubber`, `reduce`, `select`.
- Produces: `browser.Chromium.start(sandbox: bool = True) -> Chromium` (fresh profile under `/tmp`, CDP on loopback via `DevToolsActivePort`, `--headless=new --disable-dev-shm-usage --no-first-run`, never `--no-sandbox`), `.stop()` (kill and delete the profile), `.call(method, params, session=None)`.
- Produces: `session.Session` with methods, each taking `allowed: list[str]` (origins) and returning a dict that always has `origin`:
  - `open(url)`, `snapshot()`, `click(ref)`, `type(ref, text)`, `fill(ref, field, value)`, `export(item_origins)`, `end()`.
  - `fill` refuses with `not_a_secret_field` unless the node is `input[type=password]` (`field == "password"`) or an `autocomplete="one-time-code"` input, or the first of a run of `maxlength=1` inputs (`field == "totp"`). It adds `value` to the scrubber before writing and writes through `Runtime.callFunctionOn` with the value as an argument.
  - `type` refuses `input[type=password]` with `not_a_secret_field`.
  - Every method except `end` refuses with `origin_mismatch` when the page origin isn't in `allowed`.
  - `export` returns `{"origin", "cookies": select(...), "localStorage": [[k, v]...], "sessionStorage": [[k, v]...]}`; storage is empty unless the page origin is in `item_origins`.
  - Snapshot-returning methods return `{"origin", "nodes"}` after `Scrubber.apply` on the JSON text.
- Produces: socket server routes, JSON in and out, one session at a time:

| Route | Body | Reply |
|---|---|---|
| `GET /health` | | `{"status": "ready" \| "busy" \| "sandbox_unavailable"}` |
| `POST /session/open` | `{url, allowed}` | snapshot, or 409 `busy` |
| `POST /session/snapshot` | `{allowed}` | snapshot |
| `POST /session/click` | `{ref, allowed}` | snapshot |
| `POST /session/type` | `{ref, text, allowed}` | snapshot |
| `POST /session/fill` | `{ref, field, value, allowed}` | snapshot |
| `POST /session/export` | `{allowed, item_origins}` | export dict |
| `POST /session/end` | `{}` | `{"ended": bool}` |

  With no session, every route but `open`, `end` and `health` gets 409 `no_session`. An action over 30 s gets `timeout` and ends the session. `BITWARDEN_SIGNIN_IDLE_MINUTES` without an action ends it. The sandbox is checked at startup by starting and stopping Chromium once.

- [ ] **Step 1: Write the failing image tests**: `test_open_snapshot_has_no_values`, `test_fill_password_then_snapshot_scrubbed` (Review Focus 2: the test site's form has a show-password toggle that flips `type` to `text` and an error line echoing the input; after `fill` and clicking the toggle, the snapshot JSON has none of the canary's encodings), `test_fill_refuses_text_input`, `test_type_refuses_password_input`, `test_origin_mismatch_after_redirect`, `test_stale_ref`, `test_export_cookie_and_storage`, `test_busy_second_open`, `test_idle_end_wipes_profile` (idle set to 0.05 min via env; the profile directory is gone afterwards), `test_end_without_session`.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-signer.py --image`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the produces above. The driver in the image runs Chromium with the sandbox on and `seccomp=unconfined` only for this unit run; Task 4 replaces that with the profile.
- [ ] **Step 4: Run** `python3 scripts/test-bitwarden-signer.py --image`. Expected: pass.
- [ ] **Step 5: Commit** "Signer: per-session Chromium and the socket API".

### Task 4: Signer image, seccomp profile and compose service

**Files:**
- Create: `compose/bitwarden-signin/Dockerfile`, `compose/bitwarden-signin/.dockerignore`, `compose/bitwarden-signin/seccomp.json`
- Modify: `docker-compose.yml` (service `bitwarden-signin`, network `signin`, volume `bitwarden_signin`, the broker's mount of it)
- Modify: `scripts/test-compose-env-allowlist.py` (`SIGNIN_ENV`), `scripts/test-bitwarden-compose-config.py`
- Test: `scripts/test-bitwarden-signer.py` (`test_sandbox_with_profile`, tagged `docker`)

**Interfaces:**
- Produces: image `${IMAGE_PREFIX:-assistant}-bitwarden-signin:local`, `FROM nousresearch/hermes-agent:${HERMES_AGENT_IMAGE_TAG}` (build arg passed from compose with the same default as the `hermes` service), entrypoint `/opt/hermes/.venv/bin/python -m signer`.
- Produces: `seccomp.json` = Docker's default profile (moby `profiles/seccomp/default.json` at the Docker Engine version CI uses; record the version and commit in the file's first `"comment"` key) with `clone`, `clone3` and `unshare` allowed for user-namespace creation without `CAP_SYS_ADMIN`.
- Produces: environment allowlist for the service: `BITWARDEN_SIGNIN_SOCKET`, `BITWARDEN_SIGNIN_IDLE_MINUTES`, `BITWARDEN_SIGNIN_ACTION_SECONDS`. The broker gains `BITWARDEN_SIGNIN_SOCKET`.

- [ ] **Step 1: Write the failing tests**: compose config asserts the service settings in Global Constraints, a healthcheck that `GET`s `/health` over the socket, `networks: [signin]` only, no `ports`, the volume mounted by exactly `bitwarden-broker` and `bitwarden-signin`; the env allowlist; `test_sandbox_with_profile` builds the image, runs it with the profile and the hardening, and expects `/health` `ready`.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-compose-config.py && python3 scripts/test-compose-env-allowlist.py && python3 scripts/test-bitwarden-signer.py --docker`. Expected: fail.
- [ ] **Step 3: Implement** the files above.
- [ ] **Step 4: Run** the same commands. Expected: pass on Docker Desktop. Then run `test_sandbox_with_profile` on a Linux Docker host (CI's runner, or a Linux VM); if it fails on either, stop and ask Keith (Global Constraints).
- [ ] **Step 5: Commit** "bitwarden-signin service with a seccomp profile for Chromium's sandbox".

### Task 5: Broker sign-in orchestration and handover

**Files:**
- Create: `compose/bitwarden-broker/broker/signin_client.py`, `compose/bitwarden-broker/broker/signin.py`
- Modify: `compose/bitwarden-broker/broker/cdp.py` (add `handover`), `broker/server.py` (routes, health, startup end), `broker/cli.py` (`signer` command)
- Test: `scripts/test-bitwarden-broker.py` (host part with a fake signer on a temp socket; image part for `cdp.handover` against the image's Chromium)

**Interfaces:**
- Consumes: Task 1's `ItemMeta.in_page_fill`, `.idp_origins`; Task 3's socket routes.
- Produces: `signin_client.SignerClient(path).call(method, path, body) -> tuple[int, dict]`; an unreachable socket raises `SignerUnavailable`.
- Produces: `signin.SignIn(vault, bws, client)` holding at most one `(handle, item)` session:
  - `start(handle, target_id, port) -> dict`
  - `snapshot()`, `click(ref)`, `type(ref, text)`, `fill(ref, field)`, `finish(target_id, port)`, `cancel()`
  - `allowed` is `item.origins + item.idp_origins` for every call, and `item.origins` for `fill` and `export`.
  - `fill` fetches the value with `bw.get_password` or `bw.get_totp` (TOTP on an item without one is `no_totp`), sends it once, and drops it.
- Produces: the automatic sequence in `start`: open the item's first URI, then loop at most 4 rounds:
  - Find the username field by the spec's order (`autocomplete=username`, `type=email`, name or id containing `user`, `email` or `login`) and type the username if the item has one and the field is empty.
  - If a password field is present, `fill` it; else if a one-time-code field is present and the item has TOTP, `fill` it; else stop.
  - Click the form's submit control (the node with role `button` whose name matches `/sign in|log in|next|continue|submit/i`, else the first `button`).

  When a round leaves no password or one-time-code field and the origin is an item origin, it calls `finish(target_id, port)` and returns `{"done": true, "origin", "cookies"}`. Otherwise it returns the last snapshot.
- Produces: `cdp.handover(port, target_id, allowed_origins, cookies, local, session) -> HandoverResult(done: bool, origin: str, error: str | None)`:
  - It reads the tab's URL, normalizes it with `origins.normalize_origin`, and returns `handover_refused` unless that origin is in `allowed_origins`.
  - Then `Network.setCookies`, then one `Runtime.callFunctionOn` that writes both storages from arguments, then `Page.reload`.
  - It runs under the same 10 s deadline and `_Connection` as `fill_async`.
- Produces: routes `POST /signin/start {handle, target_id, port}`, `/signin/snapshot {}`, `/signin/click {ref}`, `/signin/type {ref, text}`, `/signin/fill {ref, field}`, `/signin/finish {target_id, port}`, `/signin/cancel {}`. They share `_fields` validation and the TCP checks.
  - `finish` with `handover_refused` keeps the signer session open. Every other outcome of `finish` ends it.
  - `/health` gains `signer` (`ready`, `busy`, `down` from `SignerUnavailable`, `sandbox_unavailable`).
  - At startup the broker sends `/session/end`.
  - `python3 -m broker.cli signer` prints the state.

- [ ] **Step 1: Write the failing tests** (host part, fake signer):
  - `test_signin_routes_keep_tcp_checks` (Host, `Origin`, content type)
  - `test_no_route_returns_canary`: drive every `/signin/*` route with a fake `bw` whose password and TOTP are canaries, and search every reply body for each canary encoding.
  - `test_fill_uses_item_origins_only`: the IdP origin is in `allowed` for click but not for fill.
  - `test_auto_sequence_two_step` (fake signer scripts a username page, then a password page, then an account page) → `done`.
  - `test_auto_sequence_returns_snapshot_on_unknown_page`
  - `test_redirect_off_origin_then_cancel` (Review Focus 1)
  - `test_finish_refused_keeps_session` (Review Focus 3)
  - `test_busy_and_cancel_from_any_caller` (Review Focus 4)
  - `test_startup_ends_stale_session` (Review Focus 4)
  - `test_health_signer_down`

  Image part: `test_handover_cookie_and_local_storage` (the spike's two flows, as a test) and `test_handover_refused_off_origin`.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-broker.py` then `--image`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the produces above.
- [ ] **Step 4: Run** both. Expected: pass.
- [ ] **Step 5: Commit** "Broker: /signin routes, automatic sequence and session handover".

### Task 6: Hermes actions, refusals and list flag

**Files:**
- Create: `compose/hermes/vault_bitwarden_signin.py` (copied to `/opt/hermes/tools/` by the overlay)
- Create: `compose/hermes/patch-bitwarden-signin.py`
- Modify: `compose/hermes/patch-bitwarden-broker-fill.py` (extract `_bwbroker_tab`, map `sign_in_only`, list flag)
- Modify: `compose/hermes/05-patch-tool-calling.sh` (run the new patch after `patch-bitwarden-broker-fill.py`), `docker-compose.yml` (mount both new files under `/bootstrap`)
- Test: `scripts/test-bitwarden-fill.py`

**Interfaces:**
- Consumes: Task 5's routes.
- Produces: in the fill overlay's helpers, `_bwbroker_tab(task_id: str, page_origin: str, accept_kind: str) -> tuple[str, int] | dict`. It is the supervisor, focus, target-id, port and Bot Desktop block that `_bwbroker_fill` runs today, returning `(target_id, port)` or the refusal dict. `_bwbroker_fill` calls it with no behavior change.
- Produces: `_BWBROKER_ERRORS["sign_in_only"]` = "This Bitwarden login is set up for sign_in. Call browser_vault action=sign_in with this handle."
- Produces: `_bwbroker_list_hint` sets `sign_in_only` on each `bwbroker:` item in the list output from `/items`.
- Produces: `vault_bitwarden_signin.ACTIONS = ("sign_in", "signin_snapshot", "signin_click", "signin_type", "signin_fill", "signin_finish", "signin_cancel")`, `extend_schema(schema)` (adds the actions, plus `ref` (integer) and `text` (string); reuses `field` if core has it, else adds it with enum `password`, `totp`), and `handle_action(action: str, args: dict, task_id: str) -> str` (JSON). `sign_in` and `signin_finish` get the tab with `_bwbroker_tab` for the origin the last snapshot reported (`sign_in`: the item's first origin).
- Produces: `patch-bitwarden-signin.py` inserts into `tools/browser_vault_tool.py` only: one schema hook and one dispatch entry per action, about 15 lines, idempotent, with ok / patched / warn reporting like `patch-vault-create-login.py`.

- [ ] **Step 1: Write the failing image tests**: `test_schema_has_signin_actions`, `test_dispatch_routes_each_action` (stub broker records route and body), `test_fill_login_sign_in_only_text`, `test_enter_code_sign_in_only_text`, `test_list_sign_in_only_flag`, `test_fill_behavior_unchanged_after_extract` (the existing fill tests pass unchanged), `test_signin_in_cron_turn_unknown_handle`, `test_patch_line_count` (the inserted upstream lines in `browser_vault_tool.py` from the new patch are at most 50).
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-fill.py --image`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the produces above.
- [ ] **Step 4: Run** `python3 scripts/test-bitwarden-fill.py --image`. Expected: pass.
- [ ] **Step 5: Commit** "Hermes: browser_vault sign-in actions for bwbroker: logins".

### Task 7: Status, doctor, skill, docs, CHANGELOG

**Files:**
- Modify: `scripts/vault.sh` (`cmd_status` adds top-level `signer` from `broker_cli signer`, `"down"` when the broker isn't running), `scripts/test-vault-sh.sh`
- Modify: `scripts/doctor.sh` (Bitwarden section: signer state; warn on `down` or `sandbox_unavailable`)
- Modify: `compose/hermes/skills/browser-login/SKILL.md`
- Modify: `docs/bitwarden.md` ("What the agent can and cannot see", a new "Sign-in" section, the two custom fields), `docs/contracts/credentials.md` (the `/signin` operations), `CHANGELOG.md` (Unreleased, for 0.3.16)

**Interfaces:**
- Consumes: Task 5's `broker.cli signer`.

- [ ] **Step 1: Write the failing test**: `test-vault-sh.sh` case: `status --json` has `signer` equal to the stubbed `broker_cli signer` output, and `"down"` with no broker.
- [ ] **Step 2: Run** `bash scripts/test-vault-sh.sh`. Expected: fail.
- [ ] **Step 3: Implement** `vault.sh` and `doctor.sh`. Write the skill:
  - Open the site in the browser first.
  - Call `sign_in`.
  - On a snapshot, continue with click, type and fill; `signin_fill` is the only way to enter a password or code.
  - At a CAPTCHA, an emailed link or a refused handover, call `signin_cancel` and tell the user.

  Write the docs and the CHANGELOG entry. The CHANGELOG states the behavior change (in-page fill now needs `agent-in-page-fill`), the new service, volume and network, and the `signer` field.
- [ ] **Step 4: Run** `bash scripts/test-vault-sh.sh` and `bash scripts/test-doctor*.sh` if one covers the Bitwarden section. Expected: pass.
- [ ] **Step 5: Commit** "Bitwarden sign-in: status, doctor, skill and docs".

### Task 8: End-to-end canary test

**Files:**
- Create: `scripts/test-bitwarden-signin-e2e.py` (tag `docker image`, not `ci`)
- Modify: `scripts/tests.manifest`
- If `scripts/test-vw-register.py`'s Vaultwarden start-up isn't importable: Create `scripts/lib/vw_testkit.py` with it and have both tests use it.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the test.**
  - Containers on a throwaway network: a throwaway Vaultwarden with one account and one login item whose password and TOTP seed are canaries, the broker, the signer, a Hermes-image container running browser-harness's Chromium in the broker's namespace, and a local login site.
  - The site serves a one-page form, a two-step form, a TOTP step, a `localStorage` token flow and a redirect through an IdP origin listed in `agent-signin-idp`.
  - Drive `sign_in` for each flow through the Hermes overlay entry points, as `test-bitwarden-fill.py` does.
  - Then search for every canary encoding in: the agent Chromium's DOM, storage and profile directory; `/proc/<pid>/mem` of the gateway-stand-in process, read as the `hermes` user; and `docker logs` of every container.
  - Expected: each flow ends signed in, and there are zero canary hits.
- [ ] **Step 2: Run** `python3 scripts/test-bitwarden-signin-e2e.py`. Expected: pass. A failure here goes back to the task that owns the code.
- [ ] **Step 3: Run the full suite once**: `make bitwarden-test` (it goes through `testq`, run in the background). Expected: pass.
- [ ] **Step 4: Commit** "Bitwarden sign-in end-to-end canary test".

### Task 9: Live check, contract, land (after 0.3.15 is tagged)

- [ ] **Step 1:** Confirm 0.3.15 is tagged (`git tag -l v0.3.15`). If not, stop here.
- [ ] **Step 2:** Merge `main` into the branch, symlink main's `.env` and local configs into the worktree (memory: worktree land env), then build and bring the stack up with `--profile core --profile bitwarden`.
- [ ] **Step 3:** Live check 1: one model-driven `sign_in` turn against the local Vaultwarden and a public practice login page. Record pass or fail and error codes only.
- [ ] **Step 4:** Live check 2: Keith picks a site and an item in his real Bitwarden account; he runs the turn or approves it. Record pass or fail only.
- [ ] **Step 5:** On Keith's approval, run `~/.claude/bin/land` from the worktree in the background, then remove the worktree and delete the branch.
- [ ] **Step 6:** Send the contract (spec section "Boundary"), naming 0.3.16, to the Boundary sessions "Pin 0.3.14 in labs.11" and "Bitwarden integration for agent login". Tell the release-planning session the checklist row's new state.
