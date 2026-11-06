#!/usr/bin/env python3
"""Checks for the blocked-fetch hint (patch-blocked-fetch-hint.py).

Host part is stdlib-only: the error strings are mcp-searxng's own
(error-handler.js). With the pinned Hermes image present (or --image), the
patch is applied in the image (twice, for idempotency), the call site in
_commit_tool_result is checked, and a note is queued and attached to a tool
message whose content went through upstream's untrusted wrapper: the note
must land after the wrapper's closing tag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import blocked_fetch_hint as bfh  # noqa: E402
import hermes_image  # noqa: E402

TOOL = "mcp__searxng__web_url_read"
URL = "https://registry.example.com/search?q=acme"
ARGS = {"url": URL}


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def err(text: str) -> str:
    return json.dumps({"error": text})  # ASCII-escaped emoji, as tool_error writes it


def res(text: str) -> str:
    return json.dumps({"result": text}, ensure_ascii=False)


def test_refusals() -> None:
    for status, word in (("403", "bot detection"), ("429", "rate limited"), ("503", "challenge")):
        reason = {"403": "Access blocked (bot detection or geo-restriction)", "429": "Rate limit exceeded",
                  "503": "Internal server error"}[status]
        found = bfh.hint_for(TOOL, ARGS, err(f"\U0001F6AB Website Error ({status}): {reason}"))
        expect(found is not None, f"{status} gives a note")
        host, note = found
        expect(host == "registry.example.com", host)
        expect(word in note and f"new_tab('{URL}')" in note, note)
        expect("CAPTCHA" in note and "don't try to get past it" in note, note)


def test_empty_and_challenge() -> None:
    empty = res(f"\U0001F4C4 Content Warning: Page fetched but appears empty after conversion ({URL}). "
                "May contain only media or require JavaScript.")
    found = bfh.hint_for(TOOL, ARGS, empty)
    expect(found is not None and "JavaScript" in found[1], "empty page gives a note")
    challenge = res("# Just a moment...\n\nEnable JavaScript and cookies to continue")
    found = bfh.hint_for(TOOL, json.dumps(ARGS), challenge)
    expect(found is not None and "bot-check page" in found[1], "challenge page gives a note (string args)")


def test_no_note() -> None:
    cases = [
        (TOOL, ARGS, err("\U0001F6AB Website Error (404): Page not found")),
        (TOOL, ARGS, err("\U0001F310 Network Error: fetch failed. Check if the website URL is accessible")),
        (TOOL, ARGS, res("# Acme Plumbing\n\nFounded 2019. " + "text " * 50)),
        (TOOL, ARGS, res("Just a moment... " + "long article body " * 400)),
        ("mcp__searxng__searxng_web_search", ARGS, err("\U0001F6AB Website Error (403): Access blocked")),
        ("browser_exec", ARGS, err("Website Error (403)")),
        (TOOL, {"url": "file:///etc/passwd"}, err("Website Error (403)")),
        (TOOL, {"url": "https://x.example.com/a'b"}, err("Website Error (403)")),
        (TOOL, {}, err("Website Error (403)")),
    ]
    for name, args, result in cases:
        expect(bfh.hint_for(name, args, result) is None, f"no note for {name} {args} {result[:60]}")


def test_overlay_is_wired() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    expect("run_patch /bootstrap/patch-blocked-fetch-hint.py" in runner, "cont-init runs the patch")
    compose = (ROOT / "docker-compose.yml").read_text()
    for name in ("blocked_fetch_hint.py", "patch-blocked-fetch-hint.py"):
        expect(f"/bootstrap/{name}:ro" in compose, f"{name} is mounted")


IMAGE_DRIVER = r'''
import json, os, subprocess, sys
from types import SimpleNamespace
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-blocked-fetch-hint.py"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
assert "already applied" in out.stdout, out.stdout
src = open("/opt/hermes/agent/tool_executor.py").read()
assert src.count("# assistant-stack: blocked fetch hint") == 1
assert src.index("# assistant-stack: blocked fetch hint") < src.index("plugin_live_notes.attach_to_tool_message(agent, tool_message)")

sys.path.insert(0, "/opt/hermes")
import agent.tool_executor  # the patched module still imports
from agent.blocked_fetch_hint import queue_hint
from agent import plugin_live_notes
from agent.tool_dispatch_helpers import _maybe_wrap_untrusted
from tools.registry import tool_error

name = "mcp__searxng__web_url_read"
url = "https://registry.example.com/search?q=acme"
raw = tool_error("\U0001F6AB Website Error (403): Access blocked (bot detection or geo-restriction)")
padded = raw + " " * 400  # the wrapper skips very short strings; the real result row is wrapped
wrapped = _maybe_wrap_untrusted(name, padded)
assert wrapped.startswith("<untrusted_tool_result"), "searxng results are wrapped as untrusted"

agent = SimpleNamespace()
assert queue_hint(agent, name, {"url": url}, raw, "call_1")
assert queue_hint(agent, name, {"url": url + "&page=2"}, raw, "call_2")
message = {"role": "tool", "tool_call_id": "call_2", "content": wrapped}
plugin_live_notes.attach_to_tool_message(agent, message)
content = message["content"]
close = content.index("</untrusted_tool_result>")
note_at = content.index("[Stack note]")
assert note_at > close, "note sits after the untrusted wrapper"
assert content.count("[Stack note]") == 1, "one note per host"
assert "page=2" in content[note_at:], "the newest URL wins"
assert not queue_hint(SimpleNamespace(), "mcp__searxng__searxng_web_search", {"url": url}, raw, "c")
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_refusals()
    test_empty_and_challenge()
    test_no_note()
    test_overlay_is_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
