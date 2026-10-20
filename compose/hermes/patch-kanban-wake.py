#!/usr/bin/env python3
"""Deliver kanban events to dashboard sessions after compression; a repairable give-up text.

Compatibility overlay (not upstream), for Boundary change request 0006 R4.
Copies kanban_wake.py into agent/ and edits tui_gateway/session_notifications.py:

- ``_kb_poll_board`` also claims ``tui`` subscriptions whose session key
  compression rotated into the polling session's key;
- the ``gave_up`` line (also the wake turn's text) comes from
  ``kanban_wake.gave_up_suffix``.

See kanban_wake.py for the design and docs/extending.md for the contract.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
BOOTSTRAP = Path(os.environ.get("KANBAN_WAKE_BOOTSTRAP", "/bootstrap"))
MARKER = "# assistant-stack: kanban wake"
TAG = "[patch-kanban-wake]"


def _install() -> None:
    src, dst = BOOTSTRAP / "kanban_wake.py", ROOT / "agent" / "kanban_wake.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, dst)
    print(f"{TAG} installed {dst}")


def _edit(path: Path, label: str, edits: list[tuple[str, str, int]]) -> None:
    """Apply (old, new, expected_count) edits to *path* once; MARKER makes it idempotent."""
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({label})")
        return
    for old, new, count in edits:
        found = text.count(old)
        if found != count:
            raise SystemExit(
                f"{TAG} expected {count} x {old.strip()[:60]!r} in {path}, found {found}. "
                "Hermes image may have changed — update this patch.")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {label}")


GAVE_UP_OLD = (
    "    \"gave_up\": (\"✖\", lambda t, p, title: \" gave up after repeated spawn failures\"\n"
    "                + (f\"\\n{str(p.get('error'))[:200]}\" if p.get(\"error\") else \"\")),\n"
)
GAVE_UP_NEW = "    \"gave_up\": (\"✖\", _kb_gave_up),\n"

FORMATTERS_OLD = "# kind -> (glyph, suffix after \"Kanban <id>\"); silent kinds (archived/unblocked) are absent → None.\n"
FORMATTERS_NEW = (
    f"def _kb_gave_up(task, payload, title):  {MARKER}\n"
    "    from agent.kanban_wake import gave_up_suffix\n"
    "    return gave_up_suffix(task, payload, title)\n"
    "\n"
    "\n"
) + FORMATTERS_OLD

POLL_SIG_OLD = "def _kb_poll_board(_kb, slug: str, session_key: str) -> list:\n"
POLL_SIG_NEW = "def _kb_poll_board(_kb, slug: str, session_key: str, session: dict | None = None) -> list:\n"

PRECHECK_OLD = (
    "    with contextlib.suppress(Exception):\n"
    "        if _kbn.count_notify_subs(board=slug, platform=\"tui\", chat_id=session_key) == 0:\n"
    "            return []\n"
)
PRECHECK_NEW = (
    f"    {MARKER}: also claim subscriptions compression rotated into this key.\n"
    "    _assistant_keys = {session_key}\n"
    "    with contextlib.suppress(Exception):\n"
    "        from agent.kanban_wake import rotated_chat_ids as _assistant_rotated\n"
    "        _assistant_keys |= _assistant_rotated(_kb.kanban_db_path(slug), session_key, session,\n"
    "                                              _notif_resolve_event_key, _notif_live_session_matches)\n"
    "    with contextlib.suppress(Exception):\n"
    "        if len(_assistant_keys) == 1 and _kbn.count_notify_subs(board=slug, platform=\"tui\", chat_id=session_key) == 0:\n"
    "            return []\n"
)

FILTER_OLD = "            if (sub.get(\"platform\") or \"\").lower() != \"tui\" or sub.get(\"chat_id\") != session_key:\n"
FILTER_NEW = "            if (sub.get(\"platform\") or \"\").lower() != \"tui\" or sub.get(\"chat_id\") not in _assistant_keys:\n"

CALL_OLD = "    return [t for slug in unique.values() for t in _kb_poll_board(_kb, slug, session_key)]\n"
CALL_NEW = "    return [t for slug in unique.values() for t in _kb_poll_board(_kb, slug, session_key, session)]\n"


def main() -> None:
    _install()
    _edit(ROOT / "tui_gateway" / "session_notifications.py", "session notifications", [
        (GAVE_UP_OLD, GAVE_UP_NEW, 1),
        (FORMATTERS_OLD, FORMATTERS_NEW, 1),
        (POLL_SIG_OLD, POLL_SIG_NEW, 1),
        (PRECHECK_OLD, PRECHECK_NEW, 1),
        (FILTER_OLD, FILTER_NEW, 1),
        (CALL_OLD, CALL_NEW, 1),
    ])


if __name__ == "__main__":
    main()
