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

from overlay_patch import BOOTSTRAP, ROOT, edit, install, one_of

MARKER = "# assistant-stack: kanban wake"
TAG = "[patch-kanban-wake]"


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

POLL_SIG = one_of(
    # v0.22.7
    ("def _kb_poll_board(_kb, slug: str, session_key: str) -> list:\n",
     "def _kb_poll_board(_kb, slug: str, session_key: str, session: dict | None = None) -> list:\n"),
    # v0.23.0+: upstream reclaims subscriptions stranded on delegate subagents (#150198)
    ("def _kb_poll_board(_kb, slug: str, session_key: str, stray: tuple = (), unreclaimed: list | None = None) -> list:\n",
     "def _kb_poll_board(_kb, slug: str, session_key: str, stray: tuple = (), unreclaimed: list | None = None,\n"
     "                   session: dict | None = None) -> list:\n"),
)

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

CALL = one_of(
    ("    return [t for slug in unique.values() for t in _kb_poll_board(_kb, slug, session_key)]\n",
     "    return [t for slug in unique.values() for t in _kb_poll_board(_kb, slug, session_key, session)]\n"),
    ("    texts = [t for slug in unique.values() for t in _kb_poll_board(_kb, slug, session_key, stray, unreclaimed)]\n",
     "    texts = [t for slug in unique.values()\n"
     "             for t in _kb_poll_board(_kb, slug, session_key, stray, unreclaimed, session=session)]\n"),
)


def main() -> None:
    install(BOOTSTRAP / "kanban_wake.py", ROOT / "agent" / "kanban_wake.py", TAG)
    edit(ROOT / "tui_gateway" / "session_notifications.py", [
        (GAVE_UP_OLD, GAVE_UP_NEW),
        (FORMATTERS_OLD, FORMATTERS_NEW),
        POLL_SIG,
        (PRECHECK_OLD, PRECHECK_NEW),
        (FILTER_OLD, FILTER_NEW),
        CALL,
    ], MARKER, TAG, label="session notifications")


if __name__ == "__main__":
    main()
