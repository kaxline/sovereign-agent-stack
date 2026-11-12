"""Stub OpenAI-compatible server for the client-gateway oracle: a fixed reply,
streamed or not. A last user message containing SLOW streams one word every 2 s,
so a turn can be cancelled mid-stream. Logs request paths and counts only.

Run: python3 stub_llm.py PORT"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "STUB-REPLY: hello from the stub model."
MODEL = "stub-model"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("stub %s\n" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "stub", "context_length": 131072,
                 "max_model_len": 131072}]})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        sys.stderr.write("stub POST %s stream=%s msgs=%d tools=%d\n" % (
            self.path, req.get("stream"), len(req.get("messages") or []), len(req.get("tools") or [])))
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        cid, now = "chatcmpl-stub", int(time.time())
        usage = {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
        if not req.get("stream"):
            return self._json(200, {"id": cid, "object": "chat.completion", "created": now, "model": MODEL,
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": REPLY}}],
                                    "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def ev(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        last_user = next((m for m in reversed(req.get("messages") or []) if m.get("role") == "user"), {})
        content = last_user.get("content")
        slow = "SLOW" in (content if isinstance(content, str) else json.dumps(content))
        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": MODEL}
        ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        for part in REPLY.split(" "):
            if slow:
                time.sleep(2)
            ev({**base, "choices": [{"index": 0, "delta": {"content": part + " "}, "finish_reason": None}]})
        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        ev({**base, "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
