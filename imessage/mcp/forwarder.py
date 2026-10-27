"""Forward the watcher's outbox events to a signed webhook.

The watcher (on the host) writes one JSON file per new-message event into an
outbox folder. This forwarder runs next to the receiver (in the assistant
stack, inside the messages MCP container), so the receiver's port never has
to be published on the host. Each file is checked strictly, rebuilt from the
fields an event may have, signed (webhook.py) and POSTed. A delivered file is
deleted; a failed one waits for the next tick, in order; files older than
MAX_AGE are dropped. Anything that isn't a well-formed event is deleted and
counted, never forwarded.

Standard library only. Logs are counts only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from webhook import post_event

MAX_AGE = 24 * 3600
MAX_FILE_BYTES = 64 * 1024
TYPES = {"messages.new", "messages.test"}
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_CHAT_ID = re.compile(r"^(test|[A-Za-z]{2,16};[-+];[^\s]{1,250})$")


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("bad text field")
    return value


def clean_event(raw: object) -> dict:
    """The event with only the fields it may have, checked. Raises ValueError."""
    if not isinstance(raw, dict) or raw.get("type") not in TYPES:
        raise ValueError("not an event")
    if not isinstance(raw.get("id"), str) or not _HEX_ID.match(raw["id"]):
        raise ValueError("bad id")
    chat = raw.get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), str) or not _CHAT_ID.match(chat["id"]):
        raise ValueError("bad chat")
    if not isinstance(chat.get("is_group"), bool):
        raise ValueError("bad chat")
    count = raw.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 10_000:
        raise ValueError("bad count")
    senders = raw.get("senders")
    if not isinstance(senders, list) or len(senders) > 50:
        raise ValueError("bad senders")
    ids = raw.get("message_ids")
    if (not isinstance(ids, list) or len(ids) > 1000
            or not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids)):
        raise ValueError("bad message_ids")
    latest = raw.get("latest_at")
    return {
        "type": raw["type"],
        "id": raw["id"],
        "created_at": _text(raw.get("created_at"), 40),
        "chat": {"id": chat["id"], "name": _text(chat.get("name"), 200), "is_group": chat["is_group"]},
        "count": count,
        "senders": [_text(s, 200) for s in senders],
        "message_ids": ids,
        "latest_at": None if latest is None else _text(latest, 40),
    }


class Forwarder:
    def __init__(self, outbox: Path, url: str, secret: bytes, *,
                 post: Callable[[str, bytes, dict], bool] = post_event,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print) -> None:
        self.outbox, self.url, self.secret = Path(outbox), url, secret
        self.post, self.clock, self.log = post, clock, log
        self.totals = {"delivered": 0, "failed": 0, "rejected": 0, "dropped": 0}

    def tick(self) -> dict:
        counts = {"delivered": 0, "failed": 0, "rejected": 0, "dropped": 0, "waiting": 0}
        try:
            files = sorted(p for p in self.outbox.glob("*.json") if not p.name.startswith("."))
        except OSError:
            return counts
        now = self.clock()
        blocked = False
        for path in files:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if now - st.st_mtime > MAX_AGE:
                path.unlink(missing_ok=True)
                counts["dropped"] += 1
                continue
            if blocked:
                counts["waiting"] += 1
                continue
            try:
                if st.st_size > MAX_FILE_BYTES:
                    raise ValueError("too large")
                event = clean_event(json.loads(path.read_text()))
            except (ValueError, OSError, UnicodeDecodeError):
                path.unlink(missing_ok=True)
                counts["rejected"] += 1
                continue
            if self.post(self.url, self.secret, event):
                path.unlink(missing_ok=True)
                counts["delivered"] += 1
            else:
                counts["failed"] += 1
                blocked = True  # keep order: the rest wait for the next tick
        for key in self.totals:
            self.totals[key] += counts[key]
        if any(counts[k] for k in ("delivered", "failed", "rejected", "dropped")):
            self.log(json.dumps({"forwarder": counts}))
        return counts

    def run(self, stop: threading.Event, interval: float = 1.0) -> None:
        backoff = interval
        while not stop.is_set():
            counts = self.tick()
            backoff = min(backoff * 2, 60.0) if counts["failed"] else interval
            stop.wait(backoff)
