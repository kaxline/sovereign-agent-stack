#!/usr/bin/env python3
"""Checks for the compaction tool-facts overlay.

Host part is stdlib-only and uses a stand-in compressor module. With the
pinned Hermes image present (or --image), the patch is applied in the image
(twice, for idempotency) and the real ContextCompressor is driven: the
pruned window must still hold stubs, while the summarizer input built from
it must carry the original tool bodies, and the summary template must ask
for a Findings section.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect, fail  # noqa: E402

import compaction_tool_facts as ctf  # noqa: E402
import hermes_image  # noqa: E402

PLACEHOLDER = "[Old tool output cleared to save context space]"
FAKE = SimpleNamespace(
    _PRUNED_TOOL_PLACEHOLDER=PLACEHOLDER,
    # Upstream v0.22.7's check, which misses the "(N chars result)" stubs its own pruner writes.
    _is_summary_stub=lambda s: s.startswith("[") and " chars)" in s and len(s) < 400,
)


def tool(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def stub(call_id: str, n: int) -> dict:
    return tool(call_id, f"[web_url_read] url=https://example.com/{call_id} ({n:,} chars result)")


def test_record_skips_stubs_and_duplicates() -> None:
    bodies = ctf.record_bodies(FAKE, [
        {"role": "user", "content": "q"},
        tool("a", "Founded in 2019 by Pat Example."),
        stub("b", 900),
        tool("c", "[Duplicate tool output — same content as a more recent call]"),
        tool("d", PLACEHOLDER),
        tool("a", "short"),
    ])
    expect(bodies == {"a": "Founded in 2019 by Pat Example."}, f"only full bodies are kept: {bodies}")


def test_restore_swaps_stubs_without_mutating() -> None:
    turns = [stub("a", 31), tool("b", "live result stays"), stub("z", 10)]
    out, n = ctf.restore_turns(FAKE, turns, {"a": "Founded in 2019 by Pat Example.", "b": "other"})
    expect(n == 1, f"one stub restored, got {n}")
    expect(out[0]["content"] == "Founded in 2019 by Pat Example.", "stub replaced by body")
    expect(out[1]["content"] == "live result stays", "non-stub left alone")
    expect(out[2]["content"].startswith("[web_url_read]"), "stub with no recorded body stays")
    expect(turns[0]["content"].startswith("[web_url_read]"), "input turns are not mutated")


def test_restore_budget_prefers_newest() -> None:
    turns = [stub(str(i), 9000) for i in range(5)]
    bodies = {str(i): f"page {i} " + "x" * 9000 for i in range(5)}
    out, n = ctf.restore_turns(FAKE, turns, bodies, budget=10_000, per_result=4_000)
    expect(n == 3, f"budget of 10K at 4K each restores three, got {n}")
    expect(out[0]["content"].startswith("[web_url_read]"), "oldest stays a stub")
    expect(out[4]["content"].startswith("page 4"), "newest is restored")
    expect(all(len(m["content"]) <= 4_000 for m in out), "per-result cap holds")
    restored = sum(len(m["content"]) for m in out if not m["content"].startswith("[web_url_read]"))
    expect(restored <= 10_000, f"total budget holds: {restored}")
    expect("chars omitted" in out[4]["content"], "truncation is marked")
    expect(out[4]["content"].rstrip().endswith("x"), "tail of the body is kept")


def test_findings_section() -> None:
    sections = "## Completed Actions\n[...]\n\n## Active State\n[...]"
    once = ctf.add_findings_section(sections)
    expect(once.index("## Findings") < once.index("## Active State"), "Findings precedes Active State")
    expect(ctf.add_findings_section(once) == once, "adding twice is a no-op")
    expect(ctf.add_findings_section("no anchor") == "no anchor", "missing anchor leaves text alone")


IMAGE_DRIVER = r'''
import copy, os, subprocess, sys
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-compaction-tool-facts.py"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
assert "already applied" in out.stdout, out.stdout
src = open("/opt/hermes/agent/context_compressor.py").read()
assert src.count("# assistant-stack: compaction tool facts") == 1, "one install block"

sys.path.insert(0, "/opt/hermes")
from agent.context_compressor import ContextCompressor
import agent.compaction_tool_facts as ctf
assert getattr(ContextCompressor, ctf.INSTALLED_ATTR, False), "installed"

cc = ContextCompressor("qwen/qwen3.6-35b-a3b", quiet_mode=True, config_context_length=32768,
                       protect_first_n=1, protect_last_n=2)
fact = "BCX Example LLC was registered on 2019-03-14; manager Pat Example."
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Look the company up."}]
for i in range(12):
    cid = f"call_{i}"
    messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
        "function": {"name": "web_url_read", "arguments": '{"url": "https://example.com/p%d"}' % i}}]})
    body = (fact if i == 1 else f"page {i}") + " " + ("filler " * 1500)
    messages.append({"role": "tool", "tool_call_id": cid, "content": body})
messages.append({"role": "assistant", "content": "Still looking."})
messages.append({"role": "user", "content": "Any luck?"})

working, pruned, start, end = cc._plan_compaction_window(messages)
assert pruned > 0 and start < end, (pruned, start, end)
window = working[start:end]
stubs = [m for m in window if m.get("role") == "tool" and " chars" in m["content"] and len(m["content"]) < 400]
assert stubs, "window holds prune stubs"
assert all(fact not in m.get("content", "") for m in window), "fact is gone from the pruned window"

text = cc._serialize_for_summary(window)
assert fact in text, "summarizer input carries the original body"
assert len(text) < ctf.RESTORE_BUDGET_CHARS + 20000, len(text)
assert all(fact not in m.get("content", "") for m in window), "window is not mutated"
assert messages[5]["tool_call_id"] == "call_1" and messages[5]["content"].startswith(fact), "transcript is not mutated"

worker = copy.copy(cc)
assert fact in worker._serialize_for_summary(window), "prepared-compaction worker sees the bodies"
cc._plan_compaction_window([{"role": "user", "content": "new"}])
assert fact in worker._serialize_for_summary(window), "replanning does not clear the worker's bodies"

sections = ContextCompressor._summary_template_sections(
    {"historical_task": "h", "goal": "g", "constraints": "c", "resolved_questions": "r"}, 800, "")
assert "## Findings" in sections and sections.index("## Findings") < sections.index("## Active State")
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_record_skips_stubs_and_duplicates()
    test_restore_swaps_stubs_without_mutating()
    test_restore_budget_prefers_newest()
    test_findings_section()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
