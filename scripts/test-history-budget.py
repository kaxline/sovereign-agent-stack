#!/usr/bin/env python3
"""Checks for the per-session history window.

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the patch is applied in the image and the gateway wire contract is checked:
session.create / session.resume must accept history_budget (v2026.9 answered
4000), and the history_* reply fields must validate.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import history_budget as hb  # noqa: E402
import hermes_image  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def estimate(messages) -> int:
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += len(content)
        if message.get("tool_calls"):
            total += 1
    return total


def row(row_id: int, role: str, content: str, **extra) -> dict:
    message = {"role": role, "content": content, "_row_id": row_id}
    message.update(extra)
    return message


def ids(messages) -> list:
    return [hb.message_row_id(message) for message in messages]


def test_coerce() -> None:
    expect(hb.coerce_history_budget(None) is None, "null clears the budget")
    expect(hb.coerce_history_budget(8192) == 8192, "positive int is accepted")
    for bad in (0, -1, True, False, 1.5, "8192"):
        try:
            hb.coerce_history_budget(bad)
        except ValueError:
            continue
        fail(f"expected ValueError for {bad!r}")


def test_no_budget_is_unchanged() -> None:
    messages = [row(1, "user", "hello"), row(2, "assistant", "hi")]
    window = hb.apply_history_budget(messages, None, estimate=estimate)
    expect(window.messages is messages, "no budget returns the same list")
    expect(window.report == hb.empty_report(None), "no budget report is empty")
    expect(window.needs_refresh is False, "no budget does not ask for a summary")


def test_under_budget_sends_every_row() -> None:
    messages = [row(1, "user", "aa"), row(2, "assistant", "bb"), row(3, "user", "c")]
    window = hb.apply_history_budget(messages, 100, estimate=estimate)
    expect(ids(window.messages) == [1, 2, 3], "a short thread is sent whole")
    expect(window.report["history_first_row_id"] == 1, "first verbatim row is the oldest")
    expect(window.report["history_summarised"] is False, "nothing was summarised")
    expect(window.report["history_tokens"] <= 100, "under budget stays within the ceiling")
    expect(window.report["history_tokens"] == estimate(messages), "tokens match the estimator")
    expect(window.excluded == [], "nothing is excluded")


def test_over_budget_keeps_a_tail() -> None:
    messages = [
        row(1, "user", "xxxx"),
        row(2, "assistant", "yyyy"),
        row(3, "user", "z"),
    ]
    window = hb.apply_history_budget(messages, 5, estimate=estimate)
    expect(ids(window.messages) == [2, 3], "older rows drop off the front")
    expect(window.report["history_first_row_id"] == 2, "cut is the first kept row")
    expect(window.report["history_tokens"] <= 5, "tail stays within the budget")
    expect(ids(window.excluded) == [1], "the dropped row is the excluded prefix")
    expect(window.needs_refresh is True, "a dropped prefix wants a summary")
    expect(all(hb.message_row_id(message) != 1 for message in window.messages), "excluded row is not sent")


def test_newest_user_message_may_exceed_budget() -> None:
    messages = [row(1, "user", "a"), row(2, "user", "x" * 20)]
    window = hb.apply_history_budget(messages, 5, estimate=estimate)
    expect(ids(window.messages) == [2], "the current user message is kept")
    expect(window.report["history_tokens"] > 5, "an oversized current message is reported over budget")
    expect(window.report["history_first_row_id"] == 2, "the cut is that user message")


def test_tool_pair_stays_together() -> None:
    messages = [
        row(1, "user", "xxxx"),
        row(2, "assistant", "bb", tool_calls=[{"id": "call-1"}]),
        row(3, "tool", "cccc"),
        row(4, "user", "z"),
    ]
    dropped = hb.apply_history_budget(messages, 4, estimate=estimate)
    expect(ids(dropped.messages) == [4], "a tool pair that does not fit is dropped whole")
    expect(2 not in ids(dropped.messages) and 3 not in ids(dropped.messages), "neither half of the pair is sent")

    kept = hb.apply_history_budget(messages, 10, estimate=estimate)
    expect(ids(kept.messages) == [2, 3, 4], "a tool pair that fits is kept whole")
    expect(kept.report["history_first_row_id"] == 2, "the cut is the assistant tool call")
    expect(kept.report["history_tokens"] <= 10, "the kept pair stays within the budget")


def test_summary_counts_toward_budget() -> None:
    messages = [
        row(1, "user", "xxxxxx"),
        row(2, "assistant", "yyyyyy"),
        row(3, "user", "z"),
    ]
    # Without a summary, budget 8 keeps rows 2 and 3 (7 tokens).
    bare = hb.apply_history_budget(messages, 8, estimate=estimate)
    expect(ids(bare.messages) == [2, 3], "sanity: eight tokens keeps the last two rows")

    window = hb.apply_history_budget(
        messages,
        8,
        estimate=estimate,
        summary_text="SUMMARY-TEXT",
        summary_covers_through=2,
    )
    expect(window.report["history_summarised"] is True, "a covering summary is marked")
    expect(window.messages[0].get("_row_id") is None, "the summary is not a transcript row")
    expect(window.report["history_first_row_id"] == 3, "verbatim history starts after the summary")
    expect(ids(window.messages[1:]) == [3], "the summary crowds an older verbatim row out")
    expect(window.report["history_tokens"] <= 6, "summary plus tail stays within the budget")
    expect(all(message.get("content") != "yyyy" for message in window.messages), "row 2 was not sent verbatim")


def test_stale_summary_is_not_injected() -> None:
    messages = [row(1, "user", "xxxx"), row(2, "user", "z")]
    window = hb.apply_history_budget(
        messages,
        1,
        estimate=estimate,
        summary_text="old",
        summary_covers_through=1,
    )
    # Row 2 is 1 token and is the mandatory tail. Row 1 is excluded.
    # A one-token budget cannot also hold the summary, so the tighter cut
    # still starts at row 2. Coverage through 1 matches that prefix.
    expect(window.report["history_first_row_id"] == 2, "cut stays on the current user row")
    expect(window.needs_refresh is False, "cache already covers the excluded prefix")
    expect(window.report["history_summarised"] is True, "the covering summary is sent")

    stale = hb.apply_history_budget(
        messages,
        1,
        estimate=estimate,
        summary_text="old",
        summary_covers_through=0,
    )
    expect(stale.report["history_summarised"] is False, "a stale summary is not injected")
    expect(stale.needs_refresh is True, "the caller is asked to refresh")
    expect(ids(stale.messages) == [2], "the verbatim tail is still sent")


def test_fit_tokens_tightens_the_ceiling() -> None:
    messages = [row(1, "user", "xxxx"), row(2, "assistant", "yy"), row(3, "user", "z")]
    window = hb.apply_history_budget(messages, 100, estimate=estimate, fit_tokens=3)
    expect(ids(window.messages) == [2, 3], "the model window can cut deeper than the budget")
    expect(window.report["history_budget"] == 100, "the reported budget stays the session budget")
    expect(window.report["history_tokens"] <= 3, "the sent tail fits the tighter ceiling")


IMAGE_DRIVER = r'''
import subprocess
import sys

out = subprocess.run([sys.executable, "/bootstrap/patch-history-budget.py"], capture_output=True, text=True)
if out.returncode != 0:
    raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
for label in ("session.create", "session.resume", "session.context_breakdown", "message.complete"):
    assert f"patched {label} contract" in out.stdout, out.stdout

sys.path.insert(0, "/opt/hermes")
from tui_gateway.contracts import events, registry, sessions

params, problem = registry.validate_params(registry.METHODS["session.create"], {"history_budget": 20000})
assert problem is None, problem
params, problem = registry.validate_params(
    registry.METHODS["session.resume"], {"session_id": "x", "history_budget": None})
assert problem is None, problem
params, problem = registry.validate_params(registry.METHODS["session.create"], {"history_budgett": 1})
assert problem is not None, "unknown keys are still refused"

fields = dict(history_budget=20000, history_tokens=1234, history_first_row_id=42, history_summarised=True)
events.MessageCompletePayload.model_validate({"text": "hi", **fields})
sessions.SessionContextBreakdownResult.model_validate(dict(
    categories=[], context_max=1, context_percent=0, context_used=0, estimated_total=0,
    context_estimated=False, context_source="x", model="m", **fields))
print("image ok")
'''


def main() -> None:
    test_coerce()
    test_no_budget_is_unchanged()
    test_under_budget_sends_every_row()
    test_over_budget_keeps_a_tail()
    test_newest_user_message_may_exceed_budget()
    test_tool_pair_stays_together()
    test_summary_counts_toward_budget()
    test_stale_summary_is_not_injected()
    test_fit_tokens_tightens_the_ceiling()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
