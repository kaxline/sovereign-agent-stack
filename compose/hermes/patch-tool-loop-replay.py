#!/usr/bin/env python3
"""Stop turns that keep re-running calls whose results they have already seen.

Compatibility overlay. Upstream's identical-call guards only catch a call
repeated back to back, or a cycle of at most 4 calls whose arguments and
results match on every lap. On 2026-11-05 a Boundary turn cycled through the
same 9 browser_exec calls for about 30 minutes: the cycle was too long, and a
couple of pages returned slightly different text on some laps, so neither
guard fired.

This adds a stale-replay count per (tool, arguments) signature, per turn. A
call is a stale replay when an earlier call this turn had the same signature
and returned the same result. The second stale replay of one signature adds a
note to the result; with hard stops on, the third ends the turn
(stale_replay_halt). Cycle length and call order don't matter, and a repeated
call that returns something new is not counted, so re-reading a page after it
changes stays allowed. Poller tools are exempt, as upstream exempts them.

Run after patch-tool-loop-notes.py. Run scripts/test-tool-loop-replay.py on
Hermes bumps.
"""
from __future__ import annotations

from pathlib import Path

HERMES = Path("/opt/hermes")
GUARDRAILS = HERMES / "agent" / "tool_guardrails.py"
RUN_AGENT = HERMES / "run_agent.py"
METRICS = HERMES / "hermes_cli" / "observability" / "shared_metrics_harness.py"
MARKER = "# assistant-stack: stale-replay loop guard"

GUARDRAIL_EDITS = [
    # Thresholds, next to upstream's cycle constants.
    (
        "_STALL_GUARD_CYCLE_HISTORY = 64\n",
        "_STALL_GUARD_CYCLE_HISTORY = 64\n"
        f"{MARKER}\n"
        "# Stale replay: a call whose (tool, args) and result both match an earlier call this turn.\n"
        "STALE_REPLAY_NOTICE_AFTER = 2\n"
        "STALE_REPLAY_HALT_AFTER = 3\n",
    ),
    # Halt message.
    (
        '    "loop_web_search_cap": (\n',
        '    "stale_replay_halt": (\n'
        '        "Stopped {tool_name}: this turn has re-run the same call {count} more times and got back "\n'
        '        "a result it had already seen each time. Re-running it is not progress; use the results "\n'
        '        "already provided, try something different, or answer the user with what you have."\n'
        "    ),\n"
        '    "loop_web_search_cap": (\n',
    ),
    # Notice text.
    (
        "# tool -> (LoopCapConfig field, controller counter attribute, decision code)\n",
        "_STALE_REPLAY_NOTICE = (\n"
        '    "[hermes note: this turn has re-run this {tool_name} call {count} times and got back a "\n'
        '    "result it had already seen. Do not run it again; use the results you have, change the "\n'
        '    "approach, or answer the user.]"\n'
        ")\n\n"
        "# tool -> (LoopCapConfig field, controller counter attribute, decision code)\n",
    ),
    # Per-turn state.
    (
        "        self._turn_subagent_count = 0\n",
        "        self._turn_subagent_count = 0\n"
        "        # signature -> result hashes seen this turn, and its stale-replay count\n"
        "        self._replay_seen: dict[ToolCallSignature, set[str]] = {}\n"
        "        self._replay_counts: dict[ToolCallSignature, int] = {}\n",
    ),
    # Detection, after upstream's streak and cycle checks.
    (
        "        stub = None\n",
        "        if is_plain_str and not is_stall_guard_repeatable(tool_name):\n"
        "            seen = self._replay_seen.setdefault(signature, set())\n"
        "            if result_hash in seen:\n"
        "                stale = self._replay_counts[signature] = self._replay_counts.get(signature, 0) + 1\n"
        "                if notice is None and stale >= STALE_REPLAY_NOTICE_AFTER:\n"
        "                    notice, kind = _STALE_REPLAY_NOTICE.format(count=stale, tool_name=tool_name), \"stale_replay\"\n"
        "                if self.config.hard_stop_enabled and stale >= STALE_REPLAY_HALT_AFTER and self._halt_decision is None:\n"
        "                    self._decide(\"halt\", \"stale_replay_halt\", tool_name, stale, signature)\n"
        "            seen.add(result_hash)\n"
        "\n"
        "        stub = None\n",
    ),
]

RUN_AGENT_EDITS = [
    (
        'streak_halt.code in ("identical_call_streak_halt", "identical_cycle_halt"):\n',
        'streak_halt.code in ("identical_call_streak_halt", "identical_cycle_halt", "stale_replay_halt"):\n',
    ),
]

METRICS_EDITS = [
    (
        '    "loop_web_search_cap": "web_search_cap",',
        '    "stale_replay_halt": "stale_replay", "stale_replay": "stale_replay",\n'
        '    "loop_web_search_cap": "web_search_cap",',
    ),
]


def _plan(path: Path, edits: list[tuple[str, str]]) -> str | None:
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        if text.count(old) != 1:
            print(f"[patch-tool-loop-replay] anchor missing in {path.name}, not patched: {old.strip()}")
            return None
        text = text.replace(old, new)
    return text


def main() -> None:
    files = ((GUARDRAILS, GUARDRAIL_EDITS), (RUN_AGENT, RUN_AGENT_EDITS), (METRICS, METRICS_EDITS))
    missing = [str(p) for p, _ in files if not p.is_file()]
    if missing:
        print(f"[patch-tool-loop-replay] skip: {', '.join(missing)} missing")
        return
    if MARKER in GUARDRAILS.read_text(encoding="utf-8"):
        print("[patch-tool-loop-replay] already applied")
        return
    # Check every anchor before writing anything, so a Hermes bump never leaves a half-patched guard.
    planned = [(path, _plan(path, edits)) for path, edits in files]
    if any(text is None for _, text in planned):
        return
    for path, text in planned:
        path.write_text(text, encoding="utf-8")
    print(f"[patch-tool-loop-replay] patched {GUARDRAILS}, {RUN_AGENT.name}, {METRICS.name}")


if __name__ == "__main__":
    main()
