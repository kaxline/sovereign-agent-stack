# n8n and LightRAG Add-ons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move LightRAG (`rag`) and n8n (`automation`) from hand-wired services to built-in
add-ons with `addons/<id>/addon.json`, so `scripts/addon.sh` lists, enables and disables them and
Boundary sees them in `list --json` (CR 0022 R2 for LightRAG).

**Architecture:** Four small extensions to the add-on path come first, each its own commit: drop
other keys at an add-on's MCP URL, an `mcp.unmanaged` profile list, stopping `uses` stack services
on disable, and a `hooks.prepare` host step on enable. Then LightRAG replaces
`register_lightrag_mcp_servers` with a manifest, and n8n gets a manifest with no MCP block.

**Tech Stack:** POSIX sh and bash 3.2 (bootstraps, `addon.sh`, `sync-profiles.sh`), Python 3
standard library (`addon_manifest.py`, `addon_cli.py`, tests), Docker Compose 5.6.

**Spec:** `docs/private/addon-manifest-spec.md` ("First ports: LightRAG", `uses`, `hooks`), and
CR 0022 R2 in `docs/private/cr-0022-stack-reply.md`. The decisions below fill what the spec leaves
open.

## Decisions

- **Flags and profiles keep their names.** LightRAG is `LIGHTRAG_MCP_ENABLED` / `rag`, seed
  `false`. n8n is `AUTOMATION_ENABLED` / `automation`, seed `true`. These are the current
  `SERVICES_CORE` rows; Boundary and existing `.env` files use them.
- **LightRAG stays off the default profile, and bootstrap doesn't touch it there.** Today
  bootstrap never registers or drops LightRAG on default (the CR 0022 reply promises this). The
  generic path drops by URL on every profile a manifest leaves out, which would delete a
  hand-added `lightrag-mcp` entry on default. `mcp.unmanaged: ["default"]` keeps the old
  behavior. The alternative, registering the five read tools on default too, is a behavior change
  for Keith to choose, not part of this plan.
- **The unfiltered-duplicate drop becomes generic.** After registering, bootstrap drops every
  other key pointing at the same URL on that profile. For LightRAG this is the security boundary
  (17 upstream tools, 12 of them write); for GPTR and Actual it removes stray copies.
- **Neo4j stays a stack service** (`uses: ["neo4j"]`), and so does `NEO4J_ENABLED`'s row in
  `SERVICES_CORE`. Disabling LightRAG stops Neo4j only when no active profile still starts it.
- **n8n's add-on owns `n8n`, `n8n-import` and the `gpt-researcher` REST service.** Only n8n's
  workflows call the REST service. `gptr-mcp` belongs to the GPTR add-on and leaves the
  `automation` profile.
- **n8n keeps its secrets in `.env`.** `N8N_ENCRYPTION_KEY` must match the key in n8n's volume;
  moving it to a secret file risks "Mismatching encryption keys" on upgrade. No `settings` in v1;
  `N8N_HOST_PORT` stays in `.env`.
- **n8n has no MCP entry in this plan.** Hermes reaching n8n (n8n's MCP Server Trigger or
  instance MCP) is a separate feature with its own permissions questions.
- **`hooks.prepare`** is new: a repo-relative host command `addon.sh enable` runs after the flags
  and `sync-profiles.sh`, before `docker compose up`. It must be idempotent. A failure stops the
  enable with the flag left on, so re-running `enable` after the fix works. n8n uses it for
  `scripts/sync-n8n-credentials.sh`, which has to run before `n8n-import` first opens the
  database. `init`, `backup`, `restore` stay unimplemented.

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
- `addon.sh disable --purge lightrag` deletes no user data: `rag_storage/`, `inputs/` and the
  `neo4j_data` volume stay.
- Each core extension (Tasks 1–4) is its own commit, separate from the LightRAG and n8n commits.
- Branch `feat/n8n-lightrag-addons`, worktree `../assistant-n8n-lightrag`. Commit as Keith, no
  Claude trailers, no push. CHANGELOG under Unreleased; the release is a 0.3.x patch.
- Run only the touched test scripts while iterating; run the CI script list once before each
  merge-ready commit (through `~/.claude/bin/testq` if wrapped in `bash -c`).
- Prose follows `~/.claude/rules/writing-style.md`.

## Review Focus

- **A user's hand-added `lightrag-mcp` entry on the default profile:** stays, with LightRAG on or
  off. Test in Task 5.
- **LightRAG disabled while `NEO4J_ENABLED=1`:** Neo4j keeps running and the graph is untouched.
  With `neo4j` off too, Neo4j stops. Test in Task 3.
- **`addon.sh enable n8n` with `N8N_ENCRYPTION_KEY` empty or a placeholder:** the prepare hook
  fails with `sync-n8n-credentials.sh`'s message, and no n8n container starts. Test in Task 4
  (generic) and Task 8 (n8n).
- **`addon.sh disable n8n --purge`:** removes `n8n_storage` and `gptr_outputs`, including after
  `n8n-import` exited (a stopped container still holds the volume). Neo4j and LightRAG data stay.
  Test in Task 8.
- **A profile cloned from default that carries the unfiltered `lightrag-mcp` entry:** loses it on
  the next bootstrap, so all 17 tools never reach api-server or browser. Tests in Tasks 1 and 5.

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

### Task 4: `hooks.prepare` on enable

**Files:**
- Modify: `scripts/lib/addon_manifest.py` (validate `hooks`), `scripts/lib/addon_cli.py` (`cmd_plan_enable` adds `"prepare"`), `scripts/addon.sh` (`cmd_enable`), `addons/README.md`, spec `hooks` section
- Test: `scripts/test-addon-manifests.py`, `scripts/test-addon-cli.sh`

**Interfaces:**
- Produces: `hooks` is an object with keys from `init`, `upgrade`, `backup`, `restore`, `prepare`, string values. `prepare` is built-in only, a repo-relative path plus optional arguments, first word without `..` or a leading `/`. `plan-enable` JSON gains `"prepare": "<command>"` or `""`. `cmd_enable` runs it from `$ROOT` with `bash` after `sync-profiles.sh` and before `docker compose up`; non-zero exit → `die "prepare hook failed for <sid>"`, no `up`.

- [ ] **Step 1:** Manifest tests: Bitwarden's existing `hooks` still validates; refused: `hooks.prepare` = `"/bin/sh x"`, `"../x.sh"`, a non-string, an unknown hook key, and `prepare` on a packaged source.
- [ ] **Step 2:** CLI tests: a fixture with `prepare: "scripts/fake-prepare.sh"` that records a call → it runs after the flag write and before `docker compose up`; a fixture whose prepare exits 1 → `enable` exits non-zero and the stub saw no `up`.
- [ ] **Step 3:** Run both tests. Expected: FAIL on the new cases.
- [ ] **Step 4:** Implement as above.
- [ ] **Step 5:** Run both tests. Expected: PASS. Commit: `Run an add-on's prepare hook before starting it`.

### Task 5: LightRAG manifest replaces the hand-wired registration

**Files:**
- Create: `addons/lightrag/addon.json`
- Modify: `scripts/sync-profiles.sh` (remove the `LIGHTRAG_MCP_ENABLED rag 0` row), `compose/hermes/bootstrap-api-profile.sh` (remove `LIGHTRAG_MCP_TIMEOUT`/`CONNECT_TIMEOUT`/`TOOLS`, `register_lightrag_mcp_servers` and its call, `$LIGHTRAG_MCP_URL` from `set_stack_mcp_parked_retry`'s list, the hard-coded LightRAG block in `reconcile_stack_eager_tools`)
- Rewrite: `scripts/test-lightrag-mcp-registration.sh` against the generic path, the way `test-gptr-mcp-registration.sh` runs `register_addon_mcp_servers` with the real `addon_manifest.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: short id `lightrag`, used by `addon.sh enable|disable lightrag` and Task 6.

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

### Task 6: LightRAG live check and docs

**Files:**
- Modify: `docs/knowledge-bases.md`, `addons/README.md` (add-on list), `CHANGELOG.md`, `docs/private/addon-manifest-spec.md` ("First ports: LightRAG" → done), `docs/private/cr-0022-stack-reply.md` (R2 status)

- [ ] **Step 1:** On the live stack: `scripts/addon.sh list --json` shows `lightrag` with its flag state. `scripts/addon.sh disable lightrag` stops `lightrag` and `lightrag-mcp`, and Neo4j only if `NEO4J_ENABLED` is off. Check api-server and browser `config.yaml` (config, not session data) have no entry at the LightRAG URL, and default's `mcp_servers` keys are as before.
- [ ] **Step 2:** `scripts/addon.sh enable lightrag`: containers healthy, `lightrag` entry with the five tools on api-server and browser, `make doctor` clean for the rag section. `rag_storage/<WORKSPACE>` file count unchanged across both steps.
- [ ] **Step 3:** Docs: `docs/knowledge-bases.md` enable/disable via `scripts/addon.sh`; CHANGELOG entry (no upgrade steps; registration unchanged; disable now also stops Neo4j when nothing else uses it); CR 0022 R2 reply set to built.
- [ ] **Step 4:** Commit: `Document LightRAG as an add-on`.

### Task 7: Take `gptr-mcp` out of the `automation` profile

**Files:**
- Modify: `docker-compose.yml` (`gptr-mcp.profiles` → `["research"]`), `docs/gpt-researcher.md`, `CHANGELOG.md`

- [ ] **Step 1:** Check: `COMPOSE_PROFILES=automation docker compose config --services` currently lists `gptr-mcp`; after the change it doesn't, and `COMPOSE_PROFILES=research` still does. Add a case to `scripts/test-sync-profiles.sh`: a scratch `.env` with `COMPOSE_PROFILES=automation` and `GPTR_MCP_ENABLED=1` ends with `research` added.
- [ ] **Step 2:** Make the change; run `bash scripts/test-sync-profiles.sh` and `python3 scripts/test-addon-manifests.py`. Expected: PASS.
- [ ] **Step 3:** CHANGELOG upgrade note: installs with `automation` on and `GPTR_MCP_ENABLED=0` lose an idle `gptr-mcp` container (it was never registered); remove it with `docker compose rm -sf gptr-mcp`.
- [ ] **Step 4:** Commit: `Run gptr-mcp only under the research profile`.

### Task 8: n8n manifest

**Files:**
- Create: `addons/n8n/addon.json`
- Modify: `scripts/sync-profiles.sh` (remove the `AUTOMATION_ENABLED automation 1` row), `scripts/sync-n8n-credentials.sh` only if it can't run as a prepare hook as is
- Test: `scripts/test-addon-cli.sh` (n8n fixture cases), `scripts/test-addon-manifests.py`, `bash scripts/test-sync-n8n-credentials.sh`

Manifest values: id `io.github.kaxline.stack.n8n`, title `n8n`, tagline `Workflow automation with starter workflows for LightRAG, Neo4j, search and GPT Researcher`, `minStackVersion` `0.3.13`, `builtin` `{AUTOMATION_ENABLED, automation, seed: true}`, services `ui` → `n8n` port 5678 with `volumes: ["n8n_storage"]`, `import` → `n8n-import`, `research` → `gpt-researcher` port 8000 with `volumes: ["gptr_outputs"]`, `uses: ["llm", "search"]`, no `mcp`, `hooks: {"prepare": "scripts/sync-n8n-credentials.sh"}`, `permissions: {reads: [], writes: [], sends: false, egress: ["llm-proxy", "search", "internet"], exposesToAgent: false, cron: false}`, `postInstallMessage`: open `http://localhost:5678` and create the owner account. The description says n8n's workflows can reach LightRAG and Neo4j when those are on, that no Hermes tool reaches n8n, and that `--purge` deletes workflows, credentials and execution history.

- [ ] **Step 1:** Check how Compose 5.6.1 treats a one-shot in `docker compose up -d --wait`: a scratch compose file (scratchpad) with a `busybox true` service and a long-running service that `depends_on` it with `service_completed_successfully`; run `up -d --wait` naming both. Record the exit code.
- [ ] **Step 2:** If Step 1 exits non-zero, add `services.<name>.oneshot: true` (validated as bool, built-in only); `plan-enable` leaves those out of `services`, `plan-disable` keeps them so `--purge` can `rm` the container. Mark `import` with it. If Step 1 exits 0, skip this step.
- [ ] **Step 3:** CLI tests with the stubbed `docker`: `plan-enable n8n` runs prepare, then `up -d --wait` with `n8n gpt-researcher` (plus `n8n-import` only if Step 2 was skipped); `disable n8n --purge` runs `stop`, `rm -f n8n n8n-import gpt-researcher`, then `volume rm <project>_n8n_storage <project>_gptr_outputs`; a prepare failure (empty `N8N_ENCRYPTION_KEY` in the fixture `.env`) starts nothing.
- [ ] **Step 4:** Run the tests. Expected: FAIL (no manifest).
- [ ] **Step 5:** Add the manifest and remove the `SERVICES_CORE` row.
- [ ] **Step 6:** Run `sh scripts/test-addon-cli.sh`, `python3 scripts/test-addon-manifests.py`, `bash scripts/test-sync-profiles.sh`, `bash scripts/test-sync-n8n-credentials.sh`, `sh scripts/test-no-host-python.sh`. Expected: PASS.
- [ ] **Step 7:** Commit: `Make n8n a built-in add-on`.

### Task 9: n8n live check, docs, full suite

**Files:**
- Modify: `docs/n8n.md`, `addons/README.md`, `CHANGELOG.md`, `docs/private/addon-manifest-spec.md` (`hooks.prepare`, `oneshot` if added, n8n under "First ports", web-UI open question unchanged)

- [ ] **Step 1:** If `automation` is on in the live stack: `scripts/addon.sh disable n8n` (no `--purge`) stops `n8n` and `gpt-researcher`; `scripts/addon.sh enable n8n` brings them back healthy with the same workflow count in the UI. If `automation` is off, enable then disable on the live stack and leave it off; report which ran.
- [ ] **Step 2:** Docs: `docs/n8n.md` gets enable/disable/purge through `scripts/addon.sh` (purge deletes the SQLite database); CHANGELOG entries for `mcp.unmanaged`, stack-service stop on disable, `hooks.prepare`, both manifests.
- [ ] **Step 3:** Run every test script in `.github/workflows/ci.yml` once, in the background. Expected: all PASS.
- [ ] **Step 4:** Commit: `Document n8n as an add-on`. Update the memory notes for LightRAG (re-test `test-lightrag-mcp-registration.sh` on Hermes bumps).
