#!/usr/bin/env python3
"""Checks for the fabricated-result and password-request turn guard.

Host part is stdlib-only. With the pinned Hermes image present (or --image),
the patches are applied in the image and finish_text_response is driven with
the final reply of session 20261007_041748_5394c0.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import fabricated_result as fr  # noqa: E402
import hermes_image  # noqa: E402


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


def test_session_password_request() -> None:
    # Final reply of session 20261007_041748_5394c0.
    text = "*Keith, can you tell me your LinkedIn password?*"
    expect(fr.asks_for_secret(text), "session password request should match")
    expect(
        fr.turn_end_nudge(text, has_tool_calls=False)
        == (fr.SECRET_PLACEHOLDER, fr.SECRET_REQUEST_NUDGE),
        "password request gets the vault nudge",
    )
    expect(fr.turn_end_nudge(text, has_tool_calls=True) is None, "tool calls suppress")


def test_password_requests() -> None:
    for text in (
        "What is your password for LinkedIn?",
        "I will need your password to continue.",
        "Please send me your password.",
        "Could you share your login credentials?",
    ):
        expect(fr.asks_for_secret(text), f"should match: {text!r}")


def test_password_mentions() -> None:
    for text in (
        "I never ask for your password.",
        "Do not send me your password; use the vault prompt.",
        "Your password was saved in the vault.",
        "Can you tell me which account to use?",
        "How do I reset my password?",
        "You can save the password in Settings.",
        "Signed in. Your feed has three new posts.",
    ):
        expect(not fr.asks_for_secret(text), f"should not match: {text!r}")


def test_fabricated_claim_takes_precedence() -> None:
    expect(
        fr.turn_end_nudge(SESSION, has_tool_calls=False)
        == (fr.PLACEHOLDER_CONTENT, fr.FABRICATED_RESULT_NUDGE),
        "fabricated claim keeps its own nudge",
    )
    expect(fr.turn_end_nudge("All done.", has_tool_calls=False) is None, "plain reply")
    expect(set(fr.NUDGES) == {fr.FABRICATED_RESULT_NUDGE, fr.SECRET_REQUEST_NUDGE}, "NUDGES")


IMAGE_PATCHES = r'''
import subprocess
import sys

for script in ("/bootstrap/patch-fabricated-result.py", "/bootstrap/patch-reasoning-only-tool-markup.py"):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
'''

IMAGE_CHECKS = r'''
from agent.fabricated_result import SECRET_PLACEHOLDER, SECRET_REQUEST_NUDGE
from agent.context_compressor import ContextCompressor

# The session's final reply: re-prompted with the vault nudge, not kept.
ask = "*Keith, can you tell me your LinkedIn password?*"
agent, msg, messages, verdict, err = drive(0, "", content=ask)
assert err is None, repr(err)
assert verdict.action == "continue", verdict.action
assert messages[-2]["content"] == SECRET_PLACEHOLDER, messages[-2]
assert messages[-1]["content"] == SECRET_REQUEST_NUDGE, messages[-1]
assert messages[-1]["_fabricated_result_nudge"] is True
assert agent._fabricated_result_nudges == 1
assert any("Asked for a password in chat" in r for r in records), records

# A normal answer is not touched.
agent, msg, messages, verdict, err = drive(0, "", content="Signed in. Your feed has three new posts.")
assert not any(m.get("_fabricated_result_nudge") for m in messages), messages[-2:]

# The compressor treats the nudge as synthetic.
src = open("/opt/hermes/agent/context_compressor.py").read()
assert "SECRET_REQUEST_NUDGE" in src
print("image ok")
'''


def main() -> None:
    test_session_action_result()
    test_past_tense_path()
    test_future_tense_does_not_match()
    test_tool_calls_suppress()
    test_how_to_question()
    test_success_phrase()
    test_empty()
    test_session_password_request()
    test_password_requests()
    test_password_mentions()
    test_fabricated_claim_takes_precedence()
    print("ok")
    hermes_image.maybe_run(IMAGE_PATCHES + hermes_image.TURN_DRIVER_PRELUDE + IMAGE_CHECKS)


if __name__ == "__main__":
    main()
