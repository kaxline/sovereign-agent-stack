"""The broker's client for the signer's Unix socket (``BITWARDEN_SIGNIN_SOCKET``).

One request per call, no connection kept: the signer runs one action at a time
and a call can wait behind the action in front of it, so the timeout covers the
signer's own 30 s action bound with room to spare.

``call`` returns ``(status, body)``; a body that is not a JSON object comes back
as ``{}``, so a caller sees the status and never page text. A socket that is
missing, refuses the connection or drops it raises ``SignerUnavailable``, which
the routes turn into ``signer_unavailable``. No exception from here carries the
request body: ``/session/fill`` sends a password or a TOTP code.
"""

from __future__ import annotations

import http.client
import json

from .unlock import UnixHTTPConnection

TIMEOUT_SECONDS = 120.0
HEALTH_TIMEOUT_SECONDS = 5.0
MAX_REPLY = 4 << 20


class SignerUnavailable(Exception):
    """The signer did not answer on its socket."""


class SignerClient:
    def __init__(self, path, timeout: float = TIMEOUT_SECONDS):
        self.path = str(path)
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        conn = UnixHTTPConnection(self.path, timeout=self.timeout if timeout is None else timeout)
        try:
            try:
                conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read(MAX_REPLY)
                status = resp.status
            except (OSError, http.client.HTTPException):
                raise SignerUnavailable from None
        finally:
            data = None
            conn.close()
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            reply = None
        return status, reply if isinstance(reply, dict) else {}
