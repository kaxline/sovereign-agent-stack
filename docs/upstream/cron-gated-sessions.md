# Upstream proposal: gated cron fires leave empty sessions

**Status: draft. Not filed.** Nothing here has been posted to GitHub. Filing the issue needs explicit approval in chat.

The stack works around this with `compose/hermes/cron_gated_sessions.py` and `patch-cron-gated-sessions.py` (Boundary CR 0033). Once upstream fixes it, delete both, the two `run_patch`/mount lines and `scripts/test-cron-gated-sessions.py`.

## Before filing

Checked on 2026-11-22 against `nousresearch/hermes-agent:v0.23.2` only, not upstream `main`. Re-check the line numbers and whether `main` still commits the session before the gates. A search of upstream issues for "cron empty session" and "wakeAgent session" found nothing.

## Issue text

**Title:** Cron fires skipped by a gate leave untitled, never-ended empty sessions

An agent cron job runs through `cron/scheduler_authority.py` `run_canonical_job`, which calls the gateway's `submit` (`gateway/session_cron.py`). `submit` commits the fire's session (`commit_local_session`, `source: "cron"`) before `run_job` starts. `run_job` then calls `_prepare_job_prompt`, and every early return there (config refusal, empty payload, `_apply_monitor_gate` when the output is unchanged, a wake script answering `wakeAgent: false`, `CronPromptInjectionBlocked`, a script with no output) and `setup.blocked` comes before `_open_cron_session_db`. `_finalize_cron_session`, which titles and ends the session, only runs when that store was opened.

So each gated fire leaves a session with no title, no messages and no `ended_at`. A `monitor_url` job every 15 minutes adds about 96 a day, and the default `session.list` returns all of them. `startup_orphan_reap` ends them at the next restart but leaves them untitled.

To reproduce on v0.23.2: create a job with `monitor_url` pointing at a page that doesn't change, let it fire three times after the first, and call `session.list`: three empty `source: cron` rows with `title: null` and `ended_at: null`.

Two possible fixes:

1. Run the monitor, wake and payload gates before `submit` commits the session. This changes where the gates run (the firing process instead of the owner).
2. When `execute` returns for a fire that never opened the session store, title the session as `_finalize_cron_session` would, hide it (`set_session_hidden`) and end it with a reason such as `cron_skipped`. Deleting it instead would retire its receipt while the firer still polls the admission.

The stack runs option 2 as a runtime wrapper of `session_cron.execute`, keyed on whether `_open_cron_session_db` ran during the fire, so a fire whose agent outlived `run_job` (deferred teardown) is never touched.

## The read-only half (not proposed)

The overlay also makes `prompt.submit` refuse a resumed cron session with `gateway.errors.cron_session_read_only`, because the dashboard gateway's resume path builds an ordinary live session and never reaches `session_cron.execute`'s refusal. Upstream already intends cron sessions to be read-only, and [NousResearch/hermes-agent#151857](https://github.com/NousResearch/hermes-agent/issues/151857) asks to allow replies instead. Raise the gap as a comment there if that request is declined; if it is accepted, drop the stack's refusal.
