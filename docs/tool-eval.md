# Tool-calling eval

Pin stack tools in the browser schema, capture JSONL turn traces, and score a
small prompt pack so we can see whether mitigations improve real WebUI behavior.

## Prerequisites

1. Hermes `core` stack up (`make up`), model server reachable.
2. Re-apply bootstraps after pulling these changes so eager-tool pins land
   (`always_include` plus `tools.tool_search.defer` without `session_search`):

```bash
docker compose run --rm hermes-api-bootstrap
docker compose run --rm hermes-browser-bootstrap
docker compose up -d --force-recreate hermes
```

3. Confirm pins: `make doctor` should report SearXNG + `session_search` on
   browser `always_include`, `defer` omitting `session_search`, LightRAG
   `query_document` + `get_documents` when `rag` is on, coding_* when
   `coding` is on, and **runtime always_include keeps SearXNG eager**. Doctor
   parses those lists without PyYAML — a missing host package must not print
   OK. Recreate `hermes` after pulling so the always_include overlay and
   tracer unwrap land.

## Cases

Tracked prompts live in [eval/tool-calling/cases.yaml](../eval/tool-calling/cases.yaml).
Every prompt embeds `[eval:<id>]` so WebUI paste and the scripted harness share
the same id in the trace file.

| id | Gate | Expect |
|---|---|---|
| `web-fact` | core | `searxng_web_search` (or native web rewrite); not `terminal`; `max_chars` 4000 / `max_seconds` 180 |
| `known-url` | core | `web_url_read`, not search fan-out |
| `list-projects` | core | `search_files` / `terminal` / … (any listing tool) |
| `remember` | core | `memory` or write under `/opt/memory` |
| `session-search` | core | `session_search` (not `tool_search` then stop) |
| `no-tool` | core | no tools (negative control) |
| `kb-inventory` | rag | any LightRAG read tool (`get_documents`, `query_document`, …); not web/terminal |
| `kb-query` | rag | LightRAG `query_document` (harness inserts/deletes a temporary `aurora-kb-eval` doc) |
| `coding-locate` | coding | `coding_list_roots` (use `make opencode-smoke-fixture` first); fail if text-only |
| `coding-delegate` | coding | `coding_start_task` (use `make opencode-smoke-fixture` first) |

`kb-inventory` accepts any LightRAG read tool — catalog prompts against an empty
KB are not a `query_document` miss. `kb-query` is the content gate. The scripted
harness inserts one text document (`POST /documents/text`, file source
`aurora-kb-eval.md`), runs the case, then deletes it
(`DELETE /documents/delete_document`). Nothing is scanned into `inputs/` and
the leftover file from an older fixture copy is removed if present.

WebUI paste needs the same wrap by hand:

```bash
make kb-eval-fixture          # insert + wait until processed
# paste [eval:kb-query] … in a new chat
make kb-eval-fixture-remove   # delete the doc + graph/vector rows
```

`CASE=all` installs only around `kb-query`, so `kb-inventory` still sees the
real corpus. Recreate `hermes` after pulling so `HERMES_ENVIRONMENT_HINT`
picks up the inventory-vs-query routing line.

## WebUI path (primary)

```bash
make tool-eval-reset
```

1. Open a **new** chat at http://localhost:8787.
2. Paste one case prompt from `cases.yaml` unchanged (keep the `[eval:…]` tag).
3. Wait until the turn settles (tool card or final text).
4. Repeat for the next case (new chat each time keeps the transcript clean).
5. Score:

```bash
make tool-eval-score
```

Traces append to `data/hermes/eval/traces.jsonl` (gitignored under `data/hermes/`).

## Scripted twin (same browser profile)

Port 8644 is not published; the harness `docker compose exec`s into `hermes` and
POSTs `/v1/chat/completions` with `compose/hermes/browser.env`’s key.

```bash
make tool-eval-reset
make tool-eval-run CASE=web-fact    # or CASE=all
make tool-eval-score
```

`data/hermes/eval/last-run.json` records HTTP status and tool names from the API
payload. Score merges that with the JSONL tracer when both exist. Outcome is
session-level: a final prose answer after tools is `tools_dispatched`, not
`text_only`. The tracer unwraps `tool_call({calls: [{name, …}]})` so a
successful SearXNG dispatch via the bridge still matches `pass_any`.

Cases with no JSONL event and no last-run entry are **SKIP** (`not_run`), so
scoring after `CASE=web-fact` (or a single WebUI paste) can exit 0. `curl`
waits 2400s by default; override with `TOOL_EVAL_TIMEOUT` if a final answer is
still longer.

Optional per-case `max_chars` / `max_seconds` fail a correct tool that then
dumps a huge or slow final answer (`too_long` / `too_slow`). The tracer
records `content_len` after the text phase. Browser `model.max_tokens` is
1024 (bootstrap) so WebUI cannot emit a 250k narration after one search.

```bash
make tool-eval-list
```

## Reading failures

Score prints a one-line reason when a case fails:

| Reason | Meaning |
|---|---|
| `text_only` | Session never dispatched tools (a final prose turn after tools is not this) |
| `bridge_no_dispatch` | Used `tool_search` / `tool_describe` / `tool_call` without the real tool |
| `wrong_tool` | Fired a tool that the case forbids (`fail_if_tools`) |
| `missed_expected` | Used some other tool but never hit `pass_any` |
| `no_trace` | Attempted (last-run present) but no JSONL tool events |
| `not_run` | SKIP — no JSONL and no last-run (case was not attempted) |
| `too_long` | An assistant turn exceeded the case `max_chars` |
| `too_slow` | Session span (first–last trace `ts`, else last-run `duration_s`) exceeded `max_seconds` |
| `recovered` / `rewritten` | Pass notes — text recovery or native-web rewrite helped |

Iterate one mitigation at a time after you have traces. Phase 2 levers (only if
needed) are listed in the tool-call eval plan: widen recovery patterns, consider
eager-loading by disabling tool_search on browser, or a same-model plan-then-act
skill — not a second LLM on the LM Studio slot.

## Related

- [Hermes WebUI](hermes-webui.md) — browser profile, intent-ack
- [Hermes Agent](hermes.md#model-suitability-agentic-tool-use) — model suitability and overlays
