#!/usr/bin/env python3
"""Make WebUI session-stream self-heal read disk counts, not the LRU cache.

Bug: persisted_message_count_for_session() used get_session(metadata_only=True),
which returns the in-memory SESSIONS LRU without a disk freshness check.
After a cancel/replace shortens the transcript on disk, the cache can keep a
higher _metadata_message_count. Subscribe recovery then emits session-updated
(server ahead) while GET /api/session reports the disk count — the client
force-reloads, reconnects with the same known_count, and loops (UI flicker).

Fix: read Session.load_metadata_only (same sidecar basis as the metadata API
path) so known_count and persisted_count stay apples-to-apples.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "# assistant-stack: persisted count from disk sidecar"
OLD = '''    try:
        from api.models import get_session

        s = get_session(session_id, metadata_only=True)
        count = getattr(s, "_metadata_message_count", None)
        if count is None:
            msgs = getattr(s, "messages", None)
            count = len(msgs) if isinstance(msgs, list) and msgs else None
        return int(count) if count is not None else None
    except Exception:
        logger.debug(
            "persisted_message_count_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
        return None'''

NEW = '''    try:
        # assistant-stack: persisted count from disk sidecar
        # Avoid get_session(metadata_only=True): that returns the SESSIONS LRU
        # without a disk freshness check, so a stale cached message_count can
        # be STRICTLY ahead of GET /api/session and spin session-updated →
        # loadSession forever (WebUI flicker / request storm).
        from api.models import Session

        s = Session.load_metadata_only(session_id)
        if not s:
            return None
        count = getattr(s, "_metadata_message_count", None)
        if count is None:
            msgs = getattr(s, "messages", None)
            count = len(msgs) if isinstance(msgs, list) and msgs else None
        return int(count) if count is not None else None
    except Exception:
        logger.debug(
            "persisted_message_count_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
        return None'''

_AGENT_LOG_RE = re.compile(
    r"\n        # #region agent log\n.*?        # #endregion\n",
    re.S,
)


def patch(path: Path) -> str:
    if not path.is_file():
        return f"skip missing {path}"
    text = path.read_text()
    if MARKER in text:
        cleaned, n = _AGENT_LOG_RE.subn("\n", text, count=1)
        if n:
            path.write_text(cleaned)
            return f"stripped debug instrumentation from {path}"
        return f"already patched {path}"
    if OLD not in text:
        return f"pattern not found in {path} (upstream changed?)"
    path.write_text(text.replace(OLD, NEW, 1))
    return f"patched {path}"


def main(argv: list[str]) -> int:
    targets = [Path(p) for p in argv[1:]] or [
        Path("/apptoo/api/background_process.py"),
        Path("/app/api/background_process.py"),
    ]
    for target in targets:
        print(f"[patch-webui-persisted-count] {patch(target)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
