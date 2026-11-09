# n8n and LightRAG Add-ons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move LightRAG (`rag`) and n8n (`automation`) from hand-wired services to built-in
add-ons with `addons/<id>/addon.json`, so `scripts/addon.sh` lists, enables and disables them and
Boundary sees them in `list --json` (CR 0022 R2 for LightRAG).

**Architecture:** Three small extensions to the add-on path come first, each its own commit: drop
other keys at an add-on's MCP URL, an `mcp.unmanaged` profile list, and stopping `uses` stack
services on disable. Then LightRAG replaces `register_lightrag_mcp_servers` with a manifest.
n8n's `n8n-import` learns to write its own credentials, and n8n gets a manifest with no MCP block,
using the `data` and `backup` fields the `addon.sh` CR 0020 work adds.

**Tech Stack:** POSIX sh and bash 3.2 (bootstraps, `addon.sh`, `sync-profiles.sh`), Python 3
standard library (`addon_manifest.py`, `addon_cli.py`, tests), Docker Compose 5.6.

**Spec:** `docs/private/addon-manifest-spec.md` ("First ports: LightRAG", `uses`, `hooks`), and
CR 0022 R2 in `docs/private/cr-0022-stack-reply.md`, CR 0020 R10 (the n8n manifest promised in
`docs/private/cr-0020-stack-reply.md`) and the `addon.sh` reply draft in
`docs/private/cr-0020-addon-sh-stack-reply-draft.md`. The decisions below fill what those leave
open.

## Dependencies and target

The LightRAG tasks (1, 2, 4, 5) depend on nothing outside this plan and can ship in the next 0.3.x
patch. Task 3 edits `cmd_disable`, which the `addon.sh` CR 0020 work (the IMAP/SMTP session)
rewrites for `--json` and `remove --delete-data`; land Task 3 after that rewrite, or hand it to
that session.

The n8n tasks (6–8) need three things from the `addon.sh` work: `data.volumes` / `data.paths`,
`backup.secrets` / `backup.recommended`, and `remove <id> --delete-data`. They ship in the same
0.3.x patch as `addon.sh` items 1–6, which the CR 0020 reply draft targets together. Proposed:
0.3.13 for both, with n8n moving to whichever patch carries items 1–6 if that slips. Twenty's
manifest is not in this plan.

## Decisions

- **Flags and profiles keep their names.** LightRAG is `LIGHTRAG_MCP_ENABLED` / `rag`, seed
  `false`. n8n is `AUTOMATION_ENABLED` / `automation`, seed `true`. These are the current
  `SERVICES_CORE` rows; Boundary and existing `.env` files use them.
- **LightRAG stays off the default profile, and bootstrap doesn't touch it there.** Today
  bootstrap never registers or drops LightRAG on default (the CR 0022 reply promises this). The
  generic path drops by URL on every profile a manifest leaves out, which would delete a
  hand-added `lightrag-mcp` entry on default. `mcp.unmanaged: ["default"]` keeps the old
  behavior. Keith decided on 2026-11-09 to keep it that way rather than register the five read
  tools on default.
- **The unfiltered-duplicate drop becomes generic.** After registering, bootstrap drops every
  other key pointing at the same URL on that profile. For LightRAG this is the security boundary
  (17 upstream tools, 12 of them write); for GPTR and Actual it removes stray copies.
- **Neo4j stays a stack service** (`uses: ["neo4j"]`), and so does `NEO4J_ENABLED`'s row in
  `SERVICES_CORE`. Disabling LightRAG stops Neo4j only when no active profile still starts it.
- **n8n's add-on owns `n8n`, `n8n-import` and the `gpt-researcher` REST service.** Only n8n's
  workflows call the REST service. `gptr-mcp` belongs to the GPTR add-on but stays in the
  `automation` profile, as the CR 0020 R10 reply promised: installs that turned on automation for
  research and never set `GPTR_MCP_ENABLED` (OpenCode uses it) keep it. `disable n8n` doesn't
  stop it.
- **n8n keeps its secrets in `.env`.** `N8N_ENCRYPTION_KEY` must match the key in n8n's volume;
  moving it to a secret file risks "Mismatching encryption keys" on upgrade. No `settings` in v1;
  `N8N_HOST_PORT` stays in `.env`.
- **n8n has no MCP entry in this plan.** Hermes reaching n8n (n8n's MCP Server Trigger or
  instance MCP) is a separate feature with its own permissions questions.
- **n8n needs no setup hook.** Its demo credentials have to exist before `n8n-import` first
  creates the database, so the `init` the `addon.sh` work adds (run once after the services are
  healthy) is too late. `n8n-import` instead runs `generate-n8n-credentials.js` itself, in the n8n
  image, into a scratch directory, when the database doesn't exist yet. An empty key fails the
  one-shot, so `n8n` never starts and `enable --json` reports the error with `logTail`.
  `make ensure-local` keeps calling `sync-n8n-credentials.sh` for existing installs and `make
  doctor`.
- **Manifest fields follow the `addon.sh` work.** n8n's volumes go in `data.volumes`, not
  `services.<name>.volumes`, and its keys in `backup` (`secrets: ["N8N_ENCRYPTION_KEY"]`,
  `recommended: ["N8N_USER_MANAGEMENT_JWT_SECRET"]`), as the R10 reply promised. If that work
  lands different names, use its names. LightRAG declares no `data`: `rag_storage/` and
  `inputs/` are the user's corpora, and the Neo4j graph belongs to a shared stack service.

## Global Constraints

- Upgrading installs see no change in what Hermes gets: same MCP key `lightrag`, same five tools,
  same pins, same profiles (api-server, browser), nothing on default.
- LightRAG's tool list is exactly `query_document get_documents get_pipeline_status
  get_graph_labels check_lightrag_health`. No write tool reaches any profile.
- `LIGHTRAG_MCP_URL`, `LIGHTRAG_MCP_TIMEOUT`, `LIGHTRAG_MCP_CONNECT_TIMEOUT` keep overriding the
  URL and timeouts through the manifest's `mcp.env`.
- `scripts/test-gptr-mcp-registration.sh`, `test-actual-registration.sh` and
  `test-addon-mcp-access.sh` keep passing; only the new dedupe call may be added to their expected
  call lists.
- Manifests stay canonical: `json.dumps(manifest, indent=2, ensure_ascii=False)` plus a newline.
  `sync-profiles.sh` still runs with no host `python3` (`scripts/test-no-host-python.sh`).
- `remove lightrag --delete-data` (and `disable --purge`) deletes no user data: `rag_storage/`, `inputs/` and the
  `neo4j_data` volume stay.
- Each core extension (Tasks 1–3) is its own commit, separate from the LightRAG and n8n commits.
- Branch `feat/n8n-lightrag-addons`, worktree `../assistant-n8n-lightrag`. Commit as Keith, no
  Claude trailers, no push. CHANGELOG under Unreleased; the release is a 0.3.x patch.
- Run only the touched test scripts while iterating; run the CI script list once before each
  merge-ready commit (through `~/.claude/bin/testq` if wrapped in `bash -c`).
- Prose follows `~/.claude/rules/writing-style.md`.

## Review Focus

- **A user's hand-added `lightrag-mcp` entry on the default profile:** stays, with LightRAG on or
  off. Test in Task 4.
- **LightRAG disabled while `NEO4J_ENABLED=1`:** Neo4j keeps running and the graph is untouched.
  With `neo4j` off too, Neo4j stops. Test in Task 3.
- **`addon.sh enable n8n` with `N8N_ENCRYPTION_KEY` or `LIGHTRAG_API_KEY` empty:** `n8n-import`
  exits non-zero naming the key, and `n8n` never starts. Test in Task 6.
- **`addon.sh remove n8n --delete-data`:** removes `n8n_storage` and `gptr_outputs`, including
  after `n8n-import` exited (a stopped container still holds the volume). `gptr-mcp`, Neo4j and
  LightRAG data stay. Test in Task 7.
- **A profile cloned from default that carries the unfiltered `lightrag-mcp` entry:** loses it on
  the next bootstrap, so all 17 tools never reach api-server or browser. Tests in Tasks 1 and 4.

---

### Task 1: Drop other keys at an add-on's MCP URL

**Files:**
- Modify: `compose/hermes/bootstrap-api-profile.sh` (`register_addon_mcp_servers`)
- Test: `scripts/test-addon-mcp-access.sh`, expected calls in `scripts/test-gptr-mcp-registration.sh` and `scripts/test-actual-registration.sh`

**Interfaces:**
- Consumes: `drop_mcp_entries_for_url <profile> <keep_key> <url>` (existing; empty `keep_key` drops all).
- Produces: after `register_mcp_server`, every add-on registration calls `drop_mcp_entries_for_url "$profile" "$name" "$url"`.

- [ ] **Step 1:** In `test-addon-mcp-access.sh`, add a case: plan line for `name=demo`, on, access `rw`, URL `http://demo:8000/mcp`; assert the recorded calls contain `drop:browser:demo:http://demo:8000/mcp;` after `register:browser:demo:`.
- [ ] **Step 2:** Run `sh scripts/test-addon-mcp-access.sh`. Expected: FAIL on the new case.
- [ ] **Step 3:** Add the call in `register_addon_mcp_servers` right after `register_mcp_server`, before the `sse` transport set.
- [ ] **Step 4:** Run the three scripts above; update the GPTR and Actual expected call strings only by inserting the new `drop:` call. Expected: all PASS.
- [ ] **Step 5:** Commit: `Drop other MCP keys at an add-on's URL after registering`.

### Task 2: `mcp.unmanaged` profiles

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (`_validate_mcp`, `mcp_plan`), `addons/README.md`, `docs/private/addon-manifest-spec.md` (`mcp` section)
- Test: `scripts/test-addon-manifests.py`

**Interfaces:**
- Produces: `mcp.unmanaged`: optional list of Hermes profile names. `mcp_plan(addons_dir, profile)` emits no line for an entry whose `unmanaged` contains `profile`, so bootstrap neither registers, drops, pins nor sets parked retry there.

- [ ] **Step 1:** Tests: `refused("unmanaged overlaps profiles", variant(mcp__unmanaged=["browser"]), "builtin", "unmanaged")` with `profiles` containing `browser`; `refused("unmanaged not a list", variant(mcp__unmanaged="default"), ...)`; and an `mcp_plan` check that a manifest with `unmanaged: ["default"]` yields `[]` for `"default"` and one line for `"browser"`.
- [ ] **Step 2:** Run `python3 scripts/test-addon-manifests.py`. Expected: FAIL on the three new checks.
- [ ] **Step 3:** Validate: a list of distinct `NAME_RE` strings, disjoint from `profiles` keys, not allowed on packaged add-ons. Skip in `mcp_plan` with `if profile in mcp.get("unmanaged", []): continue`.
- [ ] **Step 4:** Run the manifest test and `sh scripts/test-addon-mcp-access.sh`. Expected: PASS.
- [ ] **Step 5:** Document the field in `addons/README.md` and the spec's `mcp` section. Commit: `Add mcp.unmanaged for profiles bootstrap leaves alone`.

### Task 3: Stop unused stack services on disable

**Files:**
- Modify: `scripts/lib/addon_cli.py` (`cmd_plan_disable`), `scripts/lib/addon_manifest.py` (table), `scripts/addon.sh` (`cmd_disable`)
- Test: `scripts/test-addon-cli.sh`

**Interfaces:**
- Produces: `STACK_COMPOSE_SERVICES = {"neo4j": ["neo4j"]}` in `addon_manifest.py` (only stack services whose containers sit behind profiles). `plan-disable` JSON gains `"stack": [...]`, the compose services for the add-on's `uses` found in that table. `cmd_disable`, after `sync-profiles.sh`, reads `docker compose config --services` (active profiles only) and stops each `stack` service not in that output.

- [ ] **Step 1:** Tests in `test-addon-cli.sh` with the stubbed `docker`: a fixture add-on with `uses: ["llm","neo4j"]`. Case A: `COMPOSE_PROFILES` after disable still has `neo4j` (stub `config --services` prints `neo4j`) → no `stop neo4j`. Case B: stub omits `neo4j` → `docker compose stop neo4j` is called after the add-on's own services. Case C: `uses: ["llm"]` → `stack` is `[]`.
- [ ] **Step 2:** Run `sh scripts/test-addon-cli.sh`. Expected: FAIL on A–C. CI doesn't run this script yet; add it to `.github/workflows/ci.yml` in this commit.
- [ ] **Step 3:** Implement as above. Never `rm` or purge a stack service or its volumes, `--purge` included.
- [ ] **Step 4:** Run the test. Expected: PASS.
- [ ] **Step 5:** Commit: `Stop a used stack service on disable when no active profile starts it`.

### Task 4: LightRAG manifest replaces the hand-wired registration

**Files:**
- Create: `addons/lightrag/addon.json`
- Modify: `scripts/sync-profiles.sh` (remove the `LIGHTRAG_MCP_ENABLED rag 0` row), `compose/hermes/bootstrap-api-profile.sh` (remove `LIGHTRAG_MCP_TIMEOUT`/`CONNECT_TIMEOUT`/`TOOLS`, `register_lightrag_mcp_servers` and its call, `$LIGHTRAG_MCP_URL` from `set_stack_mcp_parked_retry`'s list, the hard-coded LightRAG block in `reconcile_stack_eager_tools`)
- Rewrite: `scripts/test-lightrag-mcp-registration.sh` against the generic path, the way `test-gptr-mcp-registration.sh` runs `register_addon_mcp_servers` with the real `addon_manifest.py`

**Interfaces:**
- Consumes: Tasks 1 and 2 (Task 3 only for the Neo4j stop).
- Produces: short id `lightrag`, used by `addon.sh enable|disable lightrag` and Task 5.

The manifest (fields in this order, canonical layout):

```json
{
  "manifestVersion": 1,
  "id": "io.github.kaxline.stack.lightrag",
  "version": "1.0.0",
  "title": "LightRAG",
  "tagline": "Ask questions of your knowledge base from chat",
  "description": "Runs LightRAG over the active WORKSPACE with Neo4j as its graph store, and gives Hermes five read-only tools through lightrag-mcp on the api-server and browser profiles. The upstream MCP server has 17 tools; the 12 that change the knowledge graph are never registered. The default profile is the user's own config and is left alone. Disabling keeps rag_storage, inputs and the Neo4j graph. See docs/knowledge-bases.md.",
  "publisher": {"name": "sovereign-agent-stack", "url": "https://github.com/kaxline/sovereign-agent-stack"},
  "minStackVersion": "0.3.13",
  "builtin": {"flag": "LIGHTRAG_MCP_ENABLED", "profile": "rag", "seed": false},
  "services": {"api": {"compose": "lightrag", "port": 9621}, "mcp": {"compose": "lightrag-mcp", "port": 8000}},
  "uses": ["llm", "neo4j"],
  "mcp": {
    "service": "mcp", "transport": "http", "path": "/mcp", "name": "lightrag", "title": "LightRAG",
    "timeout": 120, "connectTimeout": 30,
    "env": {"url": "LIGHTRAG_MCP_URL", "timeout": "LIGHTRAG_MCP_TIMEOUT", "connectTimeout": "LIGHTRAG_MCP_CONNECT_TIMEOUT"},
    "tools": ["query_document", "get_documents", "get_pipeline_status", "get_graph_labels", "check_lightrag_health"],
    "pin": ["query_document", "get_documents", "get_pipeline_status", "get_graph_labels", "check_lightrag_health"],
    "writeTools": [],
    "profiles": {"api-server": "ro", "browser": "ro"},
    "unmanaged": ["default"]
  },
  "permissions": {"reads": [], "writes": [], "sends": false, "egress": ["llm-proxy"], "exposesToAgent": true, "cron": true}
}
```

`cron: true` keeps today's behavior (LightRAG isn't fenced from cron now). Write it with
`json.dumps(..., indent=2, ensure_ascii=False)`; the block above is compacted for reading.

- [ ] **Step 1:** Rewrite the test. Off (`0`, empty, `false`, `no`): api-server and browser each get only `drop:<p>::<URL>;`; default gets no call. On (`1`, `true`, `yes`): api-server and browser get `register:<p>:lightrag:<connect>:<five tools>;`, `drop:<p>:lightrag:<URL>;`, `pin:<p>:<five tools>;`; default gets no call. `LIGHTRAG_MCP_URL=http://x:1/mcp` in the environment changes the URL in all calls.
- [ ] **Step 2:** Run `sh scripts/test-lightrag-mcp-registration.sh`. Expected: FAIL (no manifest).
- [ ] **Step 3:** Add the manifest and make the bootstrap and `sync-profiles.sh` removals listed above.
- [ ] **Step 4:** Run `sh scripts/test-lightrag-mcp-registration.sh`, `python3 scripts/test-addon-manifests.py`, `bash scripts/test-sync-profiles.sh`, `sh scripts/test-mcp-parked-retry.sh`, `sh scripts/test-no-host-python.sh`. Expected: PASS. If `test-addon-manifests.py` checks `egress` against compose networks, fix the manifest, not the check.
- [ ] **Step 5:** `make doctor` on the main checkout's `.env` copy still reports the browser `always_include` LightRAG pins (`scripts/doctor.sh` around line 378); keep that check.
- [ ] **Step 6:** Commit: `Register LightRAG from an add-on manifest (CR 0022 R2)`.

### Task 5: LightRAG live check and docs

**Files:**
- Modify: `docs/knowledge-bases.md`, `addons/README.md` (add-on list), `CHANGELOG.md`, `docs/private/addon-manifest-spec.md` ("First ports: LightRAG" → done), `docs/private/cr-0022-stack-reply.md` (R2 status)

- [ ] **Step 1:** On the live stack: `scripts/addon.sh list --json` shows `lightrag` with its flag state. `scripts/addon.sh disable lightrag` stops `lightrag` and `lightrag-mcp`, and Neo4j only if `NEO4J_ENABLED` is off. Check api-server and browser `config.yaml` (config, not session data) have no entry at the LightRAG URL, and default's `mcp_servers` keys are as before.
- [ ] **Step 2:** `scripts/addon.sh enable lightrag`: containers healthy, `lightrag` entry with the five tools on api-server and browser, `make doctor` clean for the rag section. `rag_storage/<WORKSPACE>` file count unchanged across both steps.
- [ ] **Step 3:** Docs: `docs/knowledge-bases.md` enable/disable via `scripts/addon.sh`; CHANGELOG entry (no upgrade steps; registration unchanged; disable now also stops Neo4j when nothing else uses it); CR 0022 R2 reply set to built.
- [ ] **Step 4:** Commit: `Document LightRAG as an add-on`.

### Task 6: `n8n-import` writes its own credentials

**Files:**
- Modify: `scripts/generate-n8n-credentials.js` (output dir from `N8N_CREDENTIALS_OUT`, default unchanged; exit 1 naming each empty required key), `n8n/import-demo-data.sh`, `docker-compose.yml` (`n8n-import`: mount the generator read-only, add `LIGHTRAG_API_KEY`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` to its environment only), `docs/n8n.md`
- Test: `scripts/test-sync-n8n-credentials.sh` (generator cases), `python3 scripts/test-compose-env-allowlist.py`

**Interfaces:**
- Produces: `import-demo-data.sh`, when `database.sqlite` is absent, runs `node /scripts/generate-n8n-credentials.js` with `N8N_CREDENTIALS_OUT=/tmp/n8n-credentials` and imports from there, then imports `/demo-data/workflows`. It ignores `n8n/demo-data/credentials` on the host.

- [ ] **Step 1:** Tests: the generator with `N8N_CREDENTIALS_OUT` set writes only there; with `N8N_ENCRYPTION_KEY` empty it exits 1 and its stderr names `N8N_ENCRYPTION_KEY`; same for `LIGHTRAG_API_KEY`. The allowlist test accepts the three new keys on `n8n-import` and still refuses them on `n8n`.
- [ ] **Step 2:** Run both tests. Expected: FAIL on the new cases.
- [ ] **Step 3:** Implement as above.
- [ ] **Step 4:** Run both tests. Expected: PASS. Then a scratch compose project (`COMPOSE_PROJECT_NAME=n8ncheck`, `AUTOMATION_ENABLED=1`, throwaway keys): `docker compose up -d --wait n8n` creates the database with the two credentials and six workflows (`n8n list:workflow` in the container); with `N8N_ENCRYPTION_KEY=` the `n8n-import` logs name the key and `n8n` is not created. Remove the scratch project and its volumes afterwards.
- [ ] **Step 5:** Commit: `Generate n8n demo credentials inside n8n-import`.

### Task 7: n8n manifest

**Files:**
- Create: `addons/n8n/addon.json`
- Modify: `scripts/sync-profiles.sh` (remove the `AUTOMATION_ENABLED automation 1` row)
- Test: `sh scripts/test-addon-cli.sh` (n8n fixture cases), `python3 scripts/test-addon-manifests.py`

Manifest values: id `io.github.kaxline.stack.n8n`, title `n8n`, tagline `Workflow automation with starter workflows for LightRAG, Neo4j, search and GPT Researcher`, `minStackVersion` the patch from "Dependencies and target", `builtin` `{AUTOMATION_ENABLED, automation, seed: true}`, services `ui` → `n8n` port 5678, `import` → `n8n-import`, `research` → `gpt-researcher` port 8000, `uses: ["llm", "search"]`, `data: {"paths": [], "volumes": ["n8n_storage", "gptr_outputs"]}`, `backup: {"secrets": ["N8N_ENCRYPTION_KEY"], "recommended": ["N8N_USER_MANAGEMENT_JWT_SECRET"]}`, no `mcp`, no `init`, `permissions: {reads: [], writes: [], sends: false, egress: ["llm-proxy", "search", "internet"], exposesToAgent: false, cron: false}`, `postInstallMessage`: open `http://localhost:5678` and create the owner account. The description says n8n's workflows reach LightRAG and Neo4j when those are on, that no Hermes tool reaches n8n, and that `remove --delete-data` deletes workflows, credentials and execution history.

- [ ] **Step 1:** Check how Compose 5.6.1 treats a one-shot in `docker compose up -d --wait`: a scratch compose file with a `busybox true` service and a long-running service that `depends_on` it with `service_completed_successfully`; run `up -d --wait` naming both. Record the exit code. If it is non-zero, tell the `addon.sh` session: `enable` must leave one-shots out of `up --wait` (by a manifest field or by reading `depends_on`), and `remove` must still `rm` them before deleting volumes.
- [ ] **Step 2:** CLI tests with the stubbed `docker`: `enable n8n --json` brings up `n8n` and `gpt-researcher` (and `n8n-import` only if Step 1 exited 0) and never names `gptr-mcp`; `disable n8n --json` stops `n8n`, `n8n-import`, `gpt-researcher` and not `gptr-mcp` (R10 acceptance); `remove n8n --delete-data --json` removes the three containers, then `<project>_n8n_storage` and `<project>_gptr_outputs`, and prints one line per volume.
- [ ] **Step 3:** Run the tests. Expected: FAIL (no manifest).
- [ ] **Step 4:** Add the manifest and remove the `SERVICES_CORE` row.
- [ ] **Step 5:** Run `sh scripts/test-addon-cli.sh`, `python3 scripts/test-addon-manifests.py`, `bash scripts/test-sync-profiles.sh`, `bash scripts/test-no-host-python.sh`. Expected: PASS.
- [ ] **Step 6:** Commit: `Make n8n a built-in add-on (CR 0020 R10)`.

### Task 8: n8n live check, docs, full suite

**Files:**
- Modify: `docs/n8n.md`, `addons/README.md`, `CHANGELOG.md`, `docs/private/addon-manifest-spec.md` (n8n under "First ports"), `docs/private/cr-0020-stack-reply.md` (R10 status)

- [ ] **Step 1:** If `automation` is on in the live stack: `scripts/addon.sh disable n8n` stops `n8n`, `n8n-import` and `gpt-researcher`, `gptr-mcp` keeps running, and `scripts/addon.sh enable n8n` brings them back healthy with the same workflow count. If `automation` is off, enable then disable on the live stack and leave it off; report which ran. Never run `remove --delete-data` on the live stack.
- [ ] **Step 2:** Docs: `docs/n8n.md` gets enable, disable and `remove --delete-data` (deletes the SQLite database) through `scripts/addon.sh`, and that `N8N_ENCRYPTION_KEY` must be backed up with the volume. CHANGELOG entries for `mcp.unmanaged`, the stack-service stop on disable, credentials in `n8n-import`, both manifests.
- [ ] **Step 3:** Run every test script in `.github/workflows/ci.yml` once, in the background. Expected: all PASS.
- [ ] **Step 4:** Commit: `Document n8n as an add-on`. Update the memory notes for LightRAG (re-test `test-lightrag-mcp-registration.sh` on Hermes bumps).
