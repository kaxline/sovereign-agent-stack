#!/usr/bin/env python3
"""Route selection, Authorization replacement, and in-memory admin updates."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = ROOT / "compose" / "llm-proxy" / "proxy.py"


def load_proxy():
    spec = importlib.util.spec_from_file_location("llm_proxy", PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_routes(proxy) -> None:
    table = proxy.Table(
        proxy.Route("http://chat.example:1234/v1", "chat-secret"),
        proxy.Route("", ""),
        proxy.Route("http://embed.example:9/v1", "embed-secret"),
    )
    chat = table.match("/v1/chat/completions")
    assert chat is not None
    assert chat[0] == "http://chat.example:1234/v1/chat/completions"
    assert chat[1] == "chat-secret"
    # Unset LightRAG upstream uses the chat route.
    extract = table.match("/lightrag/v1/chat/completions")
    assert extract is not None
    assert extract[0] == "http://chat.example:1234/v1/chat/completions"
    assert extract[1] == "chat-secret"
    embed = table.match("/embed/v1/embeddings?model=nomic")
    assert embed is not None
    assert embed[0] == "http://embed.example:9/v1/embeddings?model=nomic"
    assert embed[1] == "embed-secret"
    native = proxy.join_upstream("http://ollama:11434", "/chat/completions", "")
    assert native == "http://ollama:11434/v1/chat/completions"
    assert table.match("/health") is None


def test_authorization(proxy) -> None:
    replaced = proxy.outbound_headers(
        [("Authorization", "Bearer local-llm"), ("Content-Type", "application/json")],
        "real-secret",
    )
    assert ("Authorization", "Bearer real-secret") in replaced
    assert not any(value == "Bearer local-llm" for _, value in replaced)
    assert ("Content-Type", "application/json") in replaced
    empty = proxy.outbound_headers([("Authorization", "Bearer local-llm")], "")
    assert not any(key.lower() == "authorization" for key, _ in empty)


def test_admin_stays_in_memory(proxy, tmp: Path) -> None:
    before = {p.name for p in tmp.iterdir()}
    table = proxy.Table(
        proxy.Route("http://chat.example/v1", "old"),
        proxy.Route("http://extract.example/v1", "old-extract"),
        proxy.Route("http://embed.example/v1", "old-embed"),
    )
    table.update({"chat": {"url": "http://pushed.example/v1", "api_key": "pushed-secret"}})
    view = table.public_view()
    assert view["chat"]["url"] == "http://pushed.example/v1"
    assert view["chat"]["api_key_set"] is True
    encoded = json.dumps(view)
    assert "pushed-secret" not in encoded
    assert "old-extract" not in encoded
    matched = table.match("/v1/models")
    assert matched is not None and matched[1] == "pushed-secret"
    after = {p.name for p in tmp.iterdir()}
    assert before == after


def test_round_trip(proxy) -> None:
    seen = {}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            seen["authorization"] = self.headers.get("Authorization")
            seen["path"] = self.path
            seen["body"] = body
            payload = b"data: hi\n\ndata: there\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    table = proxy.Table(
        proxy.Route(f"http://127.0.0.1:{port}/v1", "real-secret"),
        proxy.Route("", ""),
        proxy.Route(f"http://127.0.0.1:{port}/v1", "embed-secret"),
    )
    proxy.InferenceHandler.table = table
    proxy.InferenceHandler.timeout = 5
    inference = ThreadingHTTPServer(("127.0.0.1", 0), proxy.InferenceHandler)
    infer_port = inference.server_address[1]
    threading.Thread(target=inference.serve_forever, daemon=True).start()

    req = Request(
        f"http://127.0.0.1:{infer_port}/v1/chat/completions",
        data=b'{"model":"m"}',
        headers={"Authorization": "Bearer local-llm", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        body = resp.read()
    assert b"data: hi" in body
    assert seen["authorization"] == "Bearer real-secret"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"] == b'{"model":"m"}'
    upstream.shutdown()
    inference.shutdown()


def main() -> None:
    proxy = load_proxy()
    test_routes(proxy)
    test_authorization(proxy)
    test_admin_stays_in_memory(proxy, Path(os.getcwd()))
    test_round_trip(proxy)
    print("OK llm proxy")


if __name__ == "__main__":
    main()
