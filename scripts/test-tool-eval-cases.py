#!/usr/bin/env python3
"""Checks the PyYAML-free parser in tool_eval_cases (stdlib only)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from testkit import Checks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tool_eval_cases as tc  # noqa: E402

checks = Checks()

# Both nesting levels share one comment stripper and one scalar parser.
FIXTURE = """\
# leading comment
top: "x # kept"  # dropped
top_null: null
top_tilde: ~
cases:
  - id: one  # dropped
    quoted: "a # b"
    single: 'c # d'
    plain: e # dropped
    gone: null
    gone2: ~
    flag: true
    prompt: |
      line one
      line two
    pass_any:
      - "f # g"
      - h # dropped
    fail_if:
      - outcome: text_only
  - "i # j"
  - null
  - ~
"""

data = tc._parse_simple(FIXTURE)
checks.expect(data.get("top") == "x # kept", f"top-level quoted #: {data.get('top')!r}")
checks.expect("top_null" in data and data["top_null"] is None, f"top-level null: {data.get('top_null')!r}")
checks.expect("top_tilde" in data and data["top_tilde"] is None, f"top-level ~: {data.get('top_tilde')!r}")

cases = data.get("cases") or []
checks.expect(len(cases) == 4, f"case count: {cases!r}")
first = cases[0] if cases else {}
expected_first = {
    "id": "one",
    "quoted": "a # b",
    "single": "c # d",
    "plain": "e",
    "gone": None,
    "gone2": None,
    "flag": True,
    "prompt": "line one\nline two\n",
    "pass_any": ["f # g", "h"],
    "fail_if": [{"outcome": "text_only"}],
}
checks.expect(first == expected_first, f"list mapping: {first!r}")
checks.expect(cases[1:] == ["i # j", None, None], f"list scalars: {cases[1:]!r}")

# The real cases file keeps the shape tool-eval.sh relies on.
real = tc._parse_simple((ROOT / "eval/tool-calling/cases.yaml").read_text(encoding="utf-8"))
checks.expect(list(real) == ["cases"], f"cases.yaml top keys: {list(real)}")
ids = []
for case in real.get("cases") or []:
    if not isinstance(case, dict):
        checks.fail(f"case is not a mapping: {case!r}")
        continue
    cid = case.get("id")
    ids.append(cid)
    checks.expect(isinstance(cid, str) and cid, f"case id: {case!r}")
    checks.expect(isinstance(case.get("gate"), str), f"{cid}: gate {case.get('gate')!r}")
    prompt = case.get("prompt")
    checks.expect(isinstance(prompt, str) and f"[eval:{cid}]" in prompt and prompt.endswith("\n"),
                  f"{cid}: prompt {prompt!r}")
    for key in ("pass_any", "fail_if_tools"):
        if key in case:
            val = case[key]
            checks.expect(isinstance(val, list) and val and all(isinstance(v, str) and v for v in val),
                          f"{cid}: {key} {val!r}")
    if "fail_if" in case:
        val = case["fail_if"]
        checks.expect(isinstance(val, list) and val and all(isinstance(v, dict) for v in val),
                      f"{cid}: fail_if {val!r}")
    for key in ("notes", "max_chars", "max_seconds"):
        if key in case:
            checks.expect(isinstance(case[key], str) and case[key], f"{cid}: {key} {case[key]!r}")
checks.expect(len(ids) >= 1 and len(ids) == len(set(ids)), f"case ids: {ids}")

checks.finish("tool_eval_cases: ok")
