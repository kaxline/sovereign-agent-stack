#!/usr/bin/env python3
"""Stdlib checks for the fabricated-result detector (no Hermes import)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))

import fabricated_result as fr  # noqa: E402


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


SESSION = """<env_check>
- Current working directory: /opt/projects/Cloudless_Substack
- User wants a test file created in this project
- Content should be "testing"
</env_check>

<action>
Write file to /opt/projects/Cloudless_Substack/test.txt with content "testing"
</action>
<result>
File written successfully
</result>
"""


def test_session_action_result() -> None:
    expect(
        fr.claims_unexecuted_result(SESSION, has_tool_calls=False),
        "session <action>/<result> text should match",
    )


def test_past_tense_path() -> None:
    expect(
        fr.claims_unexecuted_result(
            "I created /opt/projects/Cloudless_Substack/test.txt with the content testing.",
            has_tool_calls=False,
        ),
        "past-tense path claim should match",
    )


def test_future_tense_does_not_match() -> None:
    expect(
        not fr.claims_unexecuted_result(
            "I'll create the file at /tmp/assistant-tool-eval-write.txt.",
            has_tool_calls=False,
        ),
        "future tense belongs to intent-ack, not this detector",
    )


def test_tool_calls_suppress() -> None:
    expect(
        not fr.claims_unexecuted_result(SESSION, has_tool_calls=True),
        "a real tool call is not an unexecuted claim",
    )


def test_how_to_question() -> None:
    expect(
        not fr.claims_unexecuted_result(
            "How would I write a file?",
            has_tool_calls=False,
        ),
        "a how-to question should not match",
    )


def test_success_phrase() -> None:
    expect(
        fr.claims_unexecuted_result(
            "Done. File written successfully.",
            has_tool_calls=False,
        ),
        "success phrase should match",
    )


def test_empty() -> None:
    expect(
        not fr.claims_unexecuted_result("", has_tool_calls=False),
        "empty text should not match",
    )
    expect(
        not fr.claims_unexecuted_result("   ", has_tool_calls=False),
        "blank text should not match",
    )


def main() -> None:
    test_session_action_result()
    test_past_tense_path()
    test_future_tense_does_not_match()
    test_tool_calls_suppress()
    test_how_to_question()
    test_success_phrase()
    test_empty()
    print("ok")


if __name__ == "__main__":
    main()
