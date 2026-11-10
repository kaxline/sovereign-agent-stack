#!/usr/bin/env python3
"""Checks how tool_eval_trace names tool calls for scoring (stdlib only, no Hermes import)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "hermes"))

import tool_eval_trace as tt  # noqa: E402

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
