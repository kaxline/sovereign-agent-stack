#!/usr/bin/env python3
"""Checks for arguments in MCP trust-gate approvals (patch-mcp-trust-details.py).

Host part is stdlib-only: redaction and caps in mcp_trust_details.py. With the
pinned Hermes image present (or --image), the patch is applied twice
(idempotent), then a write on an untrusted server goes through the real gateway
approval queue: the notify payload, the approval.pending replay and the
client-facing request all carry the redacted arguments and the tool call id
without displacing upstream's keys or choices, two different calls are not
coalesced, a session answer is remembered, and reads still never ask.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect, fail  # noqa: E402

import hermes_image  # noqa: E402
import mcp_trust_details as mtd  # noqa: E402


def mask(text: str) -> str:
    return text.replace("sk-synthetic", "***")


def test_redaction() -> None:
    args = {
        "summary": "Call with Jordan",
        "start": "2026-10-23T11:00:00",
        "attendees": ["jordan@example.com"],
        "description": "key sk-synthetic in text",
        "password": "hunter2",
        "X-Api-Key": "abc",
        "auth": "basic",
        "author": "Sam",
        "nested": {"token_hint": "x", "deep": {"a": {"b": {"c": 1}}}},
    }
    out = mtd.redact_arguments(args, mask)
    expect(out["summary"] == "Call with Jordan" and out["attendees"] == ["jordan@example.com"], "plain values kept")
    expect(out["description"] == "key *** in text", "strings go through the redactor")
    for key in ("password", "X-Api-Key", "auth"):
        expect(out[key] == mtd.REDACTED, f"{key} is masked by name")
    expect(out["author"] == "Sam", "author is not a credential")
    expect(out["nested"]["token_hint"] == mtd.REDACTED, "nested credential keys are masked")
    expect(out["nested"]["deep"]["a"]["b"] == "…", "depth is capped")

    big = mtd.redact_arguments({"s": "x" * 2000, "l": list(range(80))}, mask)
    expect(len(big["s"]) == mtd.STRING_CAP and big["s"].endswith("…"), "strings are capped")
    expect(len(big["l"]) == mtd.ITEMS_CAP + 1 and big["l"][-1] == "… 30 more", "lists are capped")

    fields = mtd.approval_details("calendar-personal", "caldav_create_event", args, "call-1", mask)
    expect(fields == {"mcp": {"server": "calendar-personal", "tool": "caldav_create_event",
                              "arguments": out, "tool_call_id": "call-1"}}, fields)
    expect(mtd.approval_details("s", "t", None, "", mask)["mcp"]["arguments"] == {}, "no args is {}")
    expect(mtd.approval_details("s", "t", {}, "", mask)["mcp"]["tool_call_id"] is None, "missing id is null")


IMAGE_DRIVER = r'''
import os
import subprocess
import sys
import threading
import time

for want in ("patched", "already applied"):
    out = subprocess.run([sys.executable, "/bootstrap/patch-mcp-trust-details.py"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"FAIL patch: {out.stdout}{out.stderr}")
    assert out.stdout.count(want) == 2, out.stdout

os.environ["HERMES_HOME"] = "/tmp/hermes-home"
os.makedirs("/tmp/hermes-home", exist_ok=True)
os.environ["HERMES_GATEWAY_SESSION"] = "1"
sys.path.insert(0, "/opt/hermes")
from tools import approval
from tools import mcp_tool_handlers as h
from tools.approval_context import set_current_observability_context, set_current_session_key
from tools.mcp_tool_scope import _server_key
from tui_gateway import server
from tui_gateway.contracts import registry
from tui_gateway.contracts.common import PendingApproval

SK = "trust-test"
set_current_session_key(SK)
core = h._core
core._server_trust_levels[_server_key("calendar-test")] = core._TRUST_UNTRUSTED
core._tool_read_only_hints[_server_key("calendar-test")] = {"caldav_list_calendars": True}

seen, replayed = [], []
answers = []
def notify(data):
    seen.append(dict(data))
    def answer():
        time.sleep(0.2)
        replayed.append(approval.list_gateway_approvals(SK))
        approval.resolve_gateway_approval(SK, answers.pop(0), resolve_all=True)
    threading.Thread(target=answer, daemon=True).start()
approval.register_gateway_notify(SK, notify)

def gate(tool, args, call_id):
    set_current_session_key(SK)  # contextvars: a new thread starts without it
    set_current_observability_context(tool_call_id=call_id)
    return h._trust_gate_check("calendar-test", tool, args=args)

ARGS = {"summary": "Call with Jordan", "start": "2026-10-23T11:00:00", "end": "2026-10-23T11:30:00",
        "calendar_name": "Work", "password": "synthetic-pass"}

# A write asks, carrying the arguments; approve once.
answers.append("once")
assert gate("caldav_create_event", ARGS, "call-1") is None, "approved write proceeds"
data = seen[-1]
mcp = data["mcp"]
assert mcp["server"] == "calendar-test" and mcp["tool"] == "caldav_create_event", mcp
assert mcp["tool_call_id"] == "call-1", mcp
assert mcp["arguments"]["summary"] == "Call with Jordan" and mcp["arguments"]["calendar_name"] == "Work", mcp
assert mcp["arguments"]["password"] == "[REDACTED]", mcp
assert "synthetic-pass" not in repr(data), "no secret anywhere in the entry"
assert "Call with Jordan" in data["command"], data["command"]
assert data["pattern_key"] == "mcp_elicitation", "details never replace the entry's own keys"
assert data["allow_session"] is True and data["allow_permanent"] is True, "upstream's scope keys win"

# approval.pending replays the stored entry, arguments included.
pending = replayed[-1]
assert len(pending) == 1 and pending[0]["mcp"] == mcp, pending
PendingApproval.model_validate(pending[0])

# The client-facing request keeps upstream's choices and is valid under the contract.
payload = server._approval_request_payload(data)
assert payload["choices"] == ["once", "session", "always", "deny"], payload["choices"]
assert payload["mcp"] == mcp
# _emit_approval_request adds the session id when it sends the request.
registry.SERVER_REQUESTS["approval"].params.model_validate({**payload, "session_id": "sid"})

# Deny blocks the call.
answers.append("deny")
err = gate("caldav_delete_event", {"uid": "evt-1"}, "call-2")
assert err and "did not approve" in err, err
assert seen[-1]["mcp"]["arguments"] == {"uid": "evt-1"}

# Two different calls to one tool in flight at once get a prompt each.
# Identical prompt text used to coalesce them: the second waited on the first and
# adopted a deny it never showed. Both must be pending side by side.
seen.clear()
replayed.clear()
answers.extend(["once", "once"])
results = []
threads = [threading.Thread(target=lambda a=a, i=i: results.append(gate("caldav_create_event", a, f"p{i}")))
           for i, a in enumerate(({"summary": "A"}, {"summary": "B"}))]
for t in threads: t.start()
for t in threads: t.join(10)
assert results == [None, None], results
assert sorted(d["mcp"]["arguments"]["summary"] for d in seen) == ["A", "B"], seen
assert max(len(r) for r in replayed) == 2, replayed

# A session answer is remembered for that tool: the next call does not ask.
seen.clear()
answers.append("session")
assert gate("caldav_update_event", {"uid": "evt-2"}, "call-4") is None
assert len(seen) == 1 and seen[0]["mcp"]["tool"] == "caldav_update_event", seen
assert gate("caldav_update_event", {"uid": "evt-3"}, "call-5") is None and len(seen) == 1, seen

# Reads never ask.
seen.clear()
assert gate("caldav_list_calendars", {}, "call-3") is None and not seen

# The handler hands its arguments to the gate.
got = []
real = h._trust_gate_check
h._trust_gate_check = lambda s, t, booked=None, args=None: got.append((s, t, args)) or "blocked"
try:
    handler = h._make_tool_handler("calendar-test", "caldav_create_event", 5)
    assert "blocked" in handler({"summary": "X"})
finally:
    h._trust_gate_check = real
assert got == [("calendar-test", "caldav_create_event", {"summary": "X"})], got

os.write(1, b"image ok\n")
'''


def main() -> None:
    test_redaction()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER)


if __name__ == "__main__":
    main()
