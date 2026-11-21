#!/usr/bin/env python3
"""Checks how tool_eval_trace names tool calls for scoring (stdlib only, no Hermes import).

With the image present (or --image), patch-tool-eval-trace.py:

- exits with "update this patch" when the recovery patch has not run;
- after the recovery patch, lands the trace after the v9 phase call, applies
  once, and the patched block writes a recovered_then_dispatched trace line;
- still applies when the recovery patch's inserted comments change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402
import hermes_image  # noqa: E402
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))

import tool_eval_trace as tt  # noqa: E402

IMAGE_DRIVER = r'''
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

LOOP = Path("/opt/hermes/agent/conversation_loop.py")
PRISTINE = LOOP.read_text()
RECOVERY = "# assistant-stack: text-mimicked tool-call recovery"
TRACE = "# assistant-stack: tool-eval turn trace"
PHASE = "            _v = _run_phase(\n"

def run(script, ok=True):
    out = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if ok and out.returncode != 0:
        raise SystemExit(f"FAIL {script}: {out.stdout}{out.stderr}")
    return out

# Trace patch alone: fails loudly, leaves the loop as shipped.
out = run("/bootstrap/patch-tool-eval-trace.py", ok=False)
assert out.returncode != 0 and "update this patch" in out.stdout + out.stderr, out.stdout + out.stderr
assert LOOP.read_text() == PRISTINE, "loop untouched without recovery"

# Patch order from 05-patch-tool-calling.sh.
run("/bootstrap/patch-text-tool-call-recovery.py")
run("/bootstrap/patch-block-native-web-tools.py")
out = run("/bootstrap/patch-tool-eval-trace.py").stdout
assert "patched loop" in out and "patched web rewrite log" in out, out
assert "already applied (loop " in run("/bootstrap/patch-tool-eval-trace.py").stdout
text = LOOP.read_text()
assert text.count(TRACE) == 1, text.count(TRACE)
assert text.index(RECOVERY) < text.index(PHASE) < text.index(TRACE), "recovery, phase, trace"

# Run the patched block (recovery, phase, trace) on a text-mimicked call.
lines = text.splitlines(keepends=True)
start = next(i for i, ln in enumerate(lines) if RECOVERY in ln)
end = next(i for i, ln in enumerate(lines) if "tool-eval turn trace failed" in ln)
block = textwrap.dedent("".join(lines[start:end + 1]))

os.environ["HERMES_EVAL_TRACE_PATH"] = "/tmp/eval/traces.jsonl"
sys.path.insert(0, "/opt/hermes")
import logging
agent = SimpleNamespace(valid_tool_names={"search_files"}, _current_streamed_assistant_text="",
                        session_id="trace-test", _emit_status=lambda *a, **k: None)
msg = SimpleNamespace(content='search_files(pattern="todo")', tool_calls=None,
                      reasoning=None, reasoning_content=None)
s = SimpleNamespace(assistant_message=msg,
                    messages=[{"role": "user", "content": "[eval:trace-recover] find my todos"}])
phases = []
def _run_phase(fn, agent, s):
    phases.append(fn)
    return SimpleNamespace(action=None, result=None)
ns = dict(agent=agent, s=s, logger=logging.getLogger("trace-test"), _run_phase=_run_phase,
          run_tool_round="run_tool_round", finish_text_response="finish_text_response")
exec(block, ns)
assert phases == ["run_tool_round"], phases
rows = [json.loads(ln) for ln in Path("/tmp/eval/traces.jsonl").read_text().splitlines()]
assert len(rows) == 1, rows
row = rows[0]
assert row["kind"] == "assistant_turn" and row["eval_id"] == "trace-recover", row
assert row["outcome"] == "recovered_then_dispatched", row

# A comment edit in the recovery patch's inserted block does not break the trace patch.
LOOP.write_text(PRISTINE)
src = Path("/bootstrap/patch-text-tool-call-recovery.py").read_text()
marker_line = '            ' + RECOVERY + '\n'
assert src.count(marker_line) == 1, "recovery patch inserts its marker once"
edited = src.replace(marker_line, marker_line + "            # an edited comment\n")
Path("/tmp/recovery-edited.py").write_text(edited)
Path("/tmp/overlay_patch.py").write_text(Path("/bootstrap/overlay_patch.py").read_text())
run("/tmp/recovery-edited.py")
assert "# an edited comment" in LOOP.read_text()
assert "patched loop" in run("/bootstrap/patch-tool-eval-trace.py").stdout
text = LOOP.read_text()
assert text.index("# an edited comment") < text.index(PHASE) < text.index(TRACE), "edited recovery, phase, trace"

os.write(1, b"image ok\n")
'''

checks = Checks()


def call(name: str, args) -> dict:
    return {"function": {"name": name, "arguments": json.dumps(args) if isinstance(args, dict) else args}}


# browser_vault records with its action, from a JSON string or a dict.
names = tt.tool_call_names([call("browser_exec", {"code": "print(1)"}),
                            call("browser_vault", {"action": "save_login", "label": "x"})])
checks.expect(names == ["browser_exec", "browser_vault.save_login"], f"string args: {names}")
names = tt.tool_call_names([{"name": "browser_vault", "arguments": {"action": "fill", "handle": "vault_1"}}])
checks.expect(names == ["browser_vault.fill"], f"dict args: {names}")

# Attribute-style tool calls (the assistant message objects Hermes passes).
obj = SimpleNamespace(function=SimpleNamespace(name="browser_vault", arguments='{"action": "list"}'))
checks.expect(tt.tool_call_names([obj]) == ["browser_vault.list"], "attribute tool call")

# No action, empty action or unparsable args: the bare name.
for args in ({}, {"action": ""}, "not json"):
    names = tt.tool_call_names([call("browser_vault", args)])
    checks.expect(names == ["browser_vault"], f"no action {args!r}: {names}")

# Other tools keep their name even when they take an action argument.
names = tt.tool_call_names([call("memory", {"action": "add", "content": "x"})])
checks.expect(names == ["memory"], f"memory: {names}")

# Through the tool_call bridge, the inner call is qualified too.
bridged = call("tool_call", {"calls": [{"name": "browser_vault", "arguments": {"action": "enter_code"}},
                                       {"name": "web_search", "arguments": {"query": "q"}}]})
names = tt.tool_call_names([bridged])
checks.expect(names == ["tool_call", "browser_vault.enter_code", "mcp__searxng__searxng_web_search"], f"bridge: {names}")

checks.finish("tool_eval_trace: ok")
hermes_image.maybe_run(IMAGE_DRIVER)
