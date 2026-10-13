"""Turn queued draft requests into drafts in Mail.app. See docs/email.md.

The mail-drafts MCP container writes requests into <dir>/queue. This helper
runs on the host, checks each request again (it doesn't trust the
container), asks Mail through mail_drafts.js to save it as a draft, records
the outcome in <dir>/status/<id>.json and deletes the request. It never
sends: the user sends drafts from Mail. It has no access to the mail index
and reads no message content.

Output is counts only. Status files hold a state and a short reason, never
the draft's text or recipients.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import jxa

_DRAFTS_LIB = Path(__file__).resolve().parents[2] / "compose" / "mail-drafts-mcp"
if str(_DRAFTS_LIB) not in sys.path:
    sys.path.insert(0, str(_DRAFTS_LIB))

import drafts  # noqa: E402

SCRIPT = Path(__file__).with_name("mail_drafts.js")
STATUS_KEEP_DAYS = 30


@dataclass
class Stats:
    created: int = 0
    failed: int = 0
    rejected: int = 0
    deferred: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Helper:
    def __init__(self, root: Path, *, runner: jxa.Runner | None = None, open_windows: bool = False,
                 max_per_hour: int = 20, timeout: int = 120) -> None:
        self.root = Path(root)
        self.runner = runner or jxa.osascript_runner(SCRIPT, timeout)
        self.open_windows = open_windows
        self.max_per_hour = max_per_hour

    def _created_since(self, since: float) -> int:
        _, status = drafts.dirs(self.root)
        count = 0
        for path in status.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if data.get("state") == "created" and data.get("at", 0) >= since:
                count += 1
        return count

    def _prune(self, now: float) -> None:
        _, status = drafts.dirs(self.root)
        cutoff = now - STATUS_KEEP_DAYS * 86400
        for path in status.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def process(self, *, now: float | None = None) -> Stats:
        """One pass over the queue, oldest first. AutomationDenied leaves the queue as it is."""
        now = time.time() if now is None else now
        stats = Stats()
        self._prune(now)
        budget = self.max_per_hour - self._created_since(now - 3600)
        for path in drafts.pending(self.root):
            request_id = path.stem
            if budget <= 0:
                stats.deferred += 1
                continue
            try:
                raw = json.loads(path.read_text())
                clean = drafts.validate(raw)
                if clean.get("id") != request_id:
                    raise drafts.BadRequest("request id doesn't match its file name")
            except (ValueError, drafts.BadRequest) as exc:
                reason = str(exc) if isinstance(exc, drafts.BadRequest) else "unreadable request"
                drafts.write_status(self.root, request_id, "rejected", reason, now=now)
                path.unlink(missing_ok=True)
                stats.rejected += 1
                continue
            try:
                reply = self.runner({"command": "draft", "request": clean, "open": self.open_windows})
            except jxa.ScriptError as exc:
                drafts.write_status(self.root, request_id, "failed", str(exc), now=now)
                path.unlink(missing_ok=True)
                stats.failed += 1
                continue
            if reply.get("ok"):
                drafts.write_status(self.root, request_id, "created", now=now)
                stats.created += 1
                budget -= 1
            else:
                drafts.write_status(self.root, request_id, "failed", str(reply.get("reason", "unknown")), now=now)
                stats.failed += 1
            path.unlink(missing_ok=True)
        return stats

    def doctor(self) -> dict:
        """Counts only: no addresses, subjects or bodies."""
        start = time.monotonic()
        senders = self.runner({"command": "senders"})["addresses"]
        return {
            "sender_addresses": len(senders),
            "pending": len(drafts.pending(self.root)),
            "created_last_hour": self._created_since(time.time() - 3600),
            "max_per_hour": self.max_per_hour,
            "seconds_to_reach_mail": round(time.monotonic() - start, 2),
        }
