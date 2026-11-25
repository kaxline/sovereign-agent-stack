# Upstream proposal: client context and a profile pin on the dashboard gateway

**Status: draft. Not filed.** Nothing here has been posted to GitHub. Filing the issue or opening a PR needs explicit approval in chat.

This proposes upstreaming the stack's client-context overlay, which is on `main` (`compose/hermes/client_context.py` and `patch-client-context.py`, landed 2026-10-12 in 04bb66b and 334f8a0). Once upstream ships an equivalent, the overlay can be deleted (see [Removing the overlay](#removing-the-overlay-after-upstream-merges)).

- Patch: [client-context.patch](client-context.patch), in upstream layout (`tui_gateway/…`, `tests/tui_gateway/…`).
- A second, separate candidate is the history-budget overlay: [history-budget.md](history-budget.md).

## Before filing

Every fact below was checked on 2026-10-12, against upstream `main` `7cc3fb8` and the `latest` image of
that day (`session.info` release 2026.9.24). Re-check these first:

- `git apply --check` the patch against current upstream `main`.
- The five gateway files the patch touches, for changes since `7cc3fb8`.
- Error codes 4080-4083, for whether they are still unused upstream.
- The anchors row below. The stack has since moved to Hermes v0.23.1 with `patch-client-context.py`
  unchanged, so the broken `session.resume` anchor it reports is not what we run today.
- Boundary's source. The note under [Notes for us](#notes-for-us-not-part-of-the-issue) is from its
  `main` `4d9d655`.

## What upstream has today (checked 2026-10-12)

The check covered `nousresearch/hermes-agent:latest` (`sha256:444b4259…`, built 2026-10-12 03:23 UTC; `session.info` reports release date 2026.9.24) and upstream `main` at `7cc3fb8` (2026-10-12). The five gateway files the patch touches are byte-identical in the two.

| Question | Answer |
|---|---|
| `context` / `instructions` (or similar) on `session.create`, `session.resume` or `prompt.submit`? | **No.** The new `PromptSubmitParams` fields are `voice_turn`, `title_preview` and `confirm_deep_truncate`. `voice_context` already existed and only applies on the `voice-live` surface (capped at 6,000 chars). `surface` only takes `hud` / `voice-live`. |
| Profile validation on `prompt.submit`? | **No.** `params.profile` is still ignored for the turn. It is used only as a fallback name in the legacy group-room fence. |
| Upstream issues about either? | None found (`gh search issues`, four queries). |
| Error codes 4080–4083 in use upstream? | **No.** Nearby codes in use: 4070–4072 and 4090–4094. |
| Overlay anchors on `latest` | **Broken.** The `session.resume` contract anchor no longer matches, because `inline_images` now follows `close_on_disconnect` in `SessionResumeParams`. The next `make hermes-upgrade` past v2026.9.14 needs a patch fix or this upstream change. |

So there is nothing to migrate to yet, and this is a new proposal.

## Verification of the patch

- `git apply --check` is clean against upstream `main` `7cc3fb8`.
- Tests ran in a throwaway container: `latest` image plus `pytest==9.1.1` / `pytest-asyncio==1.3.0` from upstream's `dev` extra, with upstream's `tests/conftest.py` and `_fixtures`.
  - `tests/tui_gateway/test_client_context.py` (36 tests) passes, as do the existing `test_hud_surface_note.py` and `test_voice_live_delegation.py` (64 passed in total).
  - With the handlers unpatched (helper module only), 18 of the 36 new tests fail, so they exercise the wiring and not just the helpers.
- `ruff check` (0.15.10, upstream `ruff.toml`) is clean on every touched file.
- The full upstream suite was **not** run. Only the gateway tests above were.

## Differences from the overlay

The patch keeps the overlay's wire behaviour: same fields, same codes, same headings, caution line, cap and clear semantics. Only the mechanics change:

- **Handlers, not wrappers.** The checks sit inside `prompt.submit`, `_create_session` and `session.resume`. The turn context travels as an argument (`_run_after_agent_ready` → `_run_prompt_submit` → `_TurnRun.client_context`) instead of a session key that a refused turn could leave behind. This removes the overlay's "drop the staged context if the answer isn't `streaming`" cleanup.
- **The turn note is staged last** in `_prepare_turn_input`, after the @-reference refusal point, so a refused turn leaves no note for the next one.
- **The profile pin compares names first.** It calls `_profile_home()` only on a mismatch, so the common matching case never runs `activate_multi_profile_hosting()` as a side effect.
- `session.resume`'s body moves to `_resume_session()`. The registered handler validates, delegates, then stamps the record. `session.branch_stored` shares `_create_session` but does not declare `context`, so the contract still rejects the field there.

## Notes for us (not part of the issue)

- **Error codes.** 4080–4083 were picked from a range that was unused on the v2026.9.14 gateway. Maintainers may renumber them. **Boundary (`../boundary-desktop`) is the client that depends on them.** As of `main` `4d9d655` and `feat/channel-wake-header` `06b0b46`, no Boundary source references 4080–4083, `context` or `session_profile` yet. Boundary vendors `gateway-contract.generated.ts`, so the new fields would arrive through a regenerate. Agreeing on numbers before Boundary ships against them avoids a client-side migration.
- **Isolation (4083).** Upstream may prefer forwarding context over the compute-host turn protocol to refusing it. That would be a follow-up, and the refusal keeps the first PR small.
- **Behaviour change.** A mismatched `profile` on `prompt.submit` is a new error where it used to be silently ignored. Call this out in the PR as a breaking change for clients that send the wrong profile.

## Issue text (draft)

> **Title:** Dashboard gateway: let clients pass session/turn context, and refuse a mismatched `profile` on `prompt.submit`
>
> **Problem**
>
> A dashboard client often knows things the model should hear but the user did not type: the room or channel a conversation is in, who its members are, which member is being addressed, the document that's open. Today the only place to put that is the user's `text`, which pollutes the transcript, the session title and preview, rewinds and edits, and search.
>
> Upstream already has the plumbing. `ephemeral_system_prompt` is added at API time and never stored, and `_gateway_turn_context_notes` carries per-turn model input that stays out of the visible transcript and replays from the `api_content` sidecar. Clients just can't reach either. `voice_context` is the same idea scoped to `voice-live`.
>
> Separately, `prompt.submit` accepts `profile` (via `SessionParams`) and ignores it. The turn runs on the session's profile. A client that sends a different profile expecting to talk to another agent gets the wrong agent, silently.
>
> **Proposal**
>
> 1. `context: str | null` on `session.create` and `session.resume`, as **session context**. It is appended to the ephemeral system prompt under a `## Client Session Context` heading, so it stays byte-stable across turns and costs one cache miss only when the client changes it. On resume, omitting it keeps the live value and `null` or blank clears it. It is not persisted.
> 2. `context: str` on `prompt.submit`, as **turn context**. It is staged on `_gateway_turn_context_notes` under `## Client Turn Context` for that turn only. It never appears in `session.history`.
> 3. Both blocks carry a line telling the model the text comes from the app and to treat names and labels in it as untrusted metadata, never instructions.
> 4. `prompt.submit` with a `profile` that resolves to a different home from the session's returns an error with `data.session_profile`. An omitted or matching `profile` behaves as today, an unknown one is still 4064, and internal hosted-room submits are exempt.
>
> Errors (nothing on the session changes):
>
> | Code | When |
> |---|---|
> | 4080 | `context` is not a string or null, or is over 16,000 chars |
> | 4081 | `prompt.submit` `profile` differs from the session's |
> | 4082 | turn context while a turn is running. Queue, steer and redirect would fold it into another turn, so the client resubmits after `message.complete` |
> | 4083 | context on a compute-host (`dashboard.turn_isolation`) session. The worker protocol doesn't carry it yet |
>
> The codes come from a range that's unused today. Happy to renumber them to fit your scheme.
>
> Clients that leave the field out see no change. Older gateways reject the key with 4000, so clients can feature-detect.
>
> **Implementation**
>
> There is a patch ready (about 90 lines of new module, about 90 lines of handler and contract changes, and 36 tests in `tests/tui_gateway/test_client_context.py`):
>
> - `tui_gateway/client_context.py`: pure helpers for coercion, blocks, composing onto `ephemeral_system_prompt` (it remembers its base, so a personality switch or `/prompt` becomes the new base and re-applying is byte-stable) and staging the turn note.
> - Contracts: the `context` field on `SessionCreateParams`, `SessionResumeParams` and `PromptSubmitParams`.
> - `methods_session.py`: validation in `_create_session` and `session.resume`, stored as `session["client_context"]`.
> - `methods_prompt.py`: the profile pin and context checks before the slot claim, the busy refusal in the claim loop, and context passed to the turn thread.
> - `prompt_turn.py`: `_TurnRun.client_context`. Session context is applied once the turn's agent is final (after any model switch), and the turn note is staged last.
>
> Happy to open it as a PR if this direction works for you.

## PR description (draft, for after the issue is accepted)

> Adds optional client context to the dashboard gateway and pins `prompt.submit` to the session's profile. Closes #<issue>.
>
> - `session.create` / `session.resume` `context` → appended to the ephemeral system prompt (not stored, byte-stable across turns).
> - `prompt.submit` `context` → this turn's `_gateway_turn_context_notes` (model input only, replayed from `api_content`).
> - `prompt.submit` `profile` naming a different profile → error with `data.session_profile` (**behaviour change**: it was silently ignored).
> - New errors: 4080 invalid, 4081 profile mismatch, 4082 busy, 4083 isolated.
>
> Tests: `tests/tui_gateway/test_client_context.py` covers the helpers, the contracts, every refusal (each leaves the session untouched), the profile pin, resume keep/clear semantics, and `_prepare_turn_input` wiring, including that a refused turn stages no note.

## Removing the overlay after upstream merges

1. Wait for a tagged `nousresearch/hermes-agent` release that contains the change, then confirm it in the image (`grep -n "context" /opt/hermes/tui_gateway/contracts/prompt_voice.py`).
2. If upstream renumbered the errors, update Boundary's handling first, then the docs.
3. In this repo, on the branch that bumps the pin (`make hermes-upgrade AGENT=… WEBUI=…`):
   - Delete `compose/hermes/client_context.py`, `compose/hermes/patch-client-context.py` and `scripts/test-client-context.py`.
   - Remove the two `client_context` bootstrap mounts from `docker-compose.yml`, the `patch-client-context.py` line from `compose/hermes/overlays.list`, and the CI step in `.github/workflows/ci.yml`.
   - In `docs/hermes.md`, change "Client context" from "overlay, not upstream" to upstream behaviour, keep the error table (with upstream codes), and drop the overlay wording around `profile`.
   - Add a `CHANGELOG.md` entry noting that the behaviour is unchanged for clients but now comes from upstream, plus the minimum agent tag. If codes changed, make that an upgrade step for Boundary.
4. Update the memory note `upstream-client-context-overlay` (gateway overlay pending upstreaming) to say the overlay is gone.

If upstream ships something **different** (for example `instructions` instead of `context`, or a single channel), keep the overlay only as a shim that maps the old field to the new one until Boundary moves over, then delete it.
