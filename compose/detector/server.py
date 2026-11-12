"""The detector add-on's HTTP service. See docs/detector.md.

Two listeners, both serving POST /check ({"text": ...} in, the check_text
result out) and GET /health:

- INTERNAL_PORT (8000), on the internal network only detector-mcp shares. No
  token: the network is the boundary.
- HOST_PORT (8001), published on the host's 127.0.0.1 for Boundary. Every
  request needs `Authorization: Bearer <token>` matching the http_token
  secret file, and a Host of 127.0.0.1 or localhost, so a web page in the
  user's browser can neither post to it nor reach it through DNS rebinding.

Standard library only. Logs carry the method, path, status and error code,
never the text or the provider's answer.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import detector

INTERNAL_PORT = int(os.environ.get("DETECTOR_INTERNAL_PORT", "8000"))
HOST_PORT = int(os.environ.get("DETECTOR_HOST_PORT", "8001"))
MAX_BODY = 4 * detector.MAX_CHARS + 1024  # UTF-8 is at most 4 bytes a character
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

# HTTP status for each error code. Every error body is {error: {code, message}}.
STATUS = {"bad_input": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404,
          "too_long": 413, "too_short": 422, "no_key": 409, "quota": 429,
          "provider_error": 502, "timeout": 504}


def _token() -> str:
    try:
        return (detector.SECRETS_DIR / "http_token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def handler(require_token: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "detector"
        sys_version = ""

        def log_message(self, fmt, *args):  # replaced by _log
            pass

        def _log(self, status: int, code: str = "") -> None:
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {status}"
                             f"{' ' + code if code else ''} {time.monotonic() - self._t0:.2f}s\n")

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            err = body.get("error") if isinstance(body.get("error"), dict) else None
            if err and err.get("retryAfter"):
                self.send_header("Retry-After", str(err["retryAfter"]))
            self.end_headers()
            self.wfile.write(data)
            self._log(status, err["code"] if err else "")

        def _fail(self, code: str, message: str) -> None:
            self._send(STATUS[code], {"error": {"code": code, "message": message}})

        def _allowed(self) -> bool:
            if not require_token:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name not in LOCAL_HOSTS:
                self._fail("forbidden", "Host must be 127.0.0.1 or localhost")
                return False
            token = _token()
            auth = self.headers.get("Authorization") or ""
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not token or not given or not hmac.compare_digest(given.encode(), token.encode()):
                self._fail("unauthorized", "a valid bearer token is required")
                return False
            return True

        def do_GET(self):
            self._t0 = time.monotonic()
            if self.path.split("?")[0] != "/health":
                return self._fail("not_found", "no such path")
            if not self._allowed():
                return
            self._send(200, {"ok": True, "provider": detector.provider()})

        def do_POST(self):
            self._t0 = time.monotonic()
            if self.path.split("?")[0] != "/check":
                return self._fail("not_found", "no such path")
            if not self._allowed():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length < 0:
                return self._fail("bad_input", "Content-Length is required")
            if length > MAX_BODY:
                return self._fail("too_long", f"text is over {detector.MAX_CHARS} characters")
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._fail("bad_input", "the body must be JSON")
            if not isinstance(body, dict) or "text" not in body:
                return self._fail("bad_input", 'the body must be {"text": ...}')
            result = detector.check_text(body["text"])
            err = result.get("error")
            self._send(STATUS.get(err["code"], 502) if err else 200, result)

    return Handler


def serve(port: int, require_token: bool) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler(require_token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve(INTERNAL_PORT, require_token=False)
    serve(HOST_PORT, require_token=True)
    sys.stderr.write(f"detector listening on {INTERNAL_PORT} (internal) and {HOST_PORT} (token)\n")
    threading.Event().wait()
