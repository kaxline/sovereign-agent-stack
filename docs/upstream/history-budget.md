# Upstream candidate: per-session history budget

**Status: candidate only. No issue text or patch yet, and nothing filed.** This is kept separate from [client-context.md](client-context.md) on purpose: it is a bigger change in a different area (compaction and the request path, not the RPC surface), and it should be reviewed on its own.

## What the overlay does

`compose/hermes/history_budget.py`, `history_budget_hook.py` and `patch-history-budget.py` are about 1,070 lines, with tests in `scripts/test-history-budget.py`. The user-facing behaviour is documented in [docs/hermes.md](../hermes.md) (the "per-session history budget" paragraphs under Image pins):

- `history_budget` (a positive integer of tokens, or `null`) on `session.create` / `session.resume`. Resume keeps the stored value when the field is omitted.
- The budget windows the **prompt**, never the transcript. It sends a newest-first tail, may prepend a summary of the excluded prefix, and never splits a tool call from its results.
- `session.context_breakdown` and `message.complete` report `history_budget`, `history_tokens`, `history_first_row_id` and `history_summarised`.

## Upstream status (checked 2026-10-12, `latest` `sha256:444b4259…` / `main` `7cc3fb8`)

- **No equivalent.** No `history_budget` or comparable per-session prompt window exists in `tui_gateway` or `agent`. The only `*history_budget*` name is `tools/vision_tools_history_budget`, which is unrelated (image eviction).
- **The overlay's anchors are broken on `latest`.** The dry run stops at the `session.context_breakdown contract` block in `tui_gateway/contracts/sessions.py`. The overlay patches 19 anchors across `contracts/sessions.py`, `methods_session.py`, `session_workdir.py`, `server.py`, `prompt_turn.py`, `agent/conversation_compression.py`, `agent/micro_compaction.py` and `agent/context_compressor.py`, so upgrades cost more for it than for client context.
- **The error code clashes.** The overlay returns **4028** for a bad `history_budget`. Upstream already uses 4028 on `prompt.submit` for "truncation would erase the entire session transcript" (present in both v2026.9.14 and `latest`). The methods differ, so nothing is ambiguous today, but an upstream version needs its own code. Boundary references 4028 in two places, which matter if upstream picks a new code:
  - `src/main/services/assistant/hermes/hermesRewind.ts:68` treats it as "rewind unsupported", which is the upstream meaning.
  - `__tests__/hermesHistoryBudget.test.ts` uses it for the budget error.

## Before drafting

1. Decide the API shape upstream would accept. One option is the same `history_budget` param. Another is a profile or config default plus a per-session override (the overlay deliberately has no profile key, because personality profiles also run side chats).
2. Re-anchor or rewrite against the current compaction code. Upstream added `compaction_events.py`, `compression_marker.py`, `context_compressor_prellm.py` and `conversation_compression_reply_anchor.py` since the pin, and some of those may offer cleaner hook points.
3. Pick an unused error code, and plan the Boundary change with it.
