"""Signed webhook POSTs, shared by the watcher and the outbox forwarder.

The signature matches Hermes' generic webhook check (V2): the hex HMAC-SHA256
of "<X-Webhook-Timestamp>.<body>" in X-Webhook-Signature-V2, with the event id
in X-Request-ID so the receiver can drop duplicates. Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def post_event(url: str, secret: bytes, event: dict, timeout: float = 5.0) -> bool:
    """POST one event. True on a 2xx answer; False on any error or other status."""
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": sign(secret, timestamp, body),
        "X-Request-ID": event["id"],
        "User-Agent": "imessage-watch/1",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
