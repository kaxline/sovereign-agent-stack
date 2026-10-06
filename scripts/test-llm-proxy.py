#!/usr/bin/env python3
"""Route selection, Authorization replacement, admin updates, and stream handling."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
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


def test_key_files(proxy, tmp: Path) -> None:
    key_path = tmp / "chat_key"
    key_path.write_text("from-file\n")
    saved = dict(os.environ)
    try:
        os.environ["LLM_BINDING_HOST"] = "http://chat.example/v1"
        os.environ["LLM_BINDING_API_KEY"] = "from-env"
        os.environ["LLM_BINDING_API_KEY_FILE"] = str(key_path)
        os.environ["EMBEDDING_BINDING_HOST"] = "http://embed.example/v1"
        os.environ["EMBEDDING_BINDING_API_KEY"] = "embed-env"
        os.environ.pop("EMBEDDING_BINDING_API_KEY_FILE", None)
        table = proxy.load_table()
        # The file wins over the environment, trailing newline stripped.
        assert table.match("/v1/models")[1] == "from-file"
        # A pushed key on a route without a file survives a reload.
        table.update({"embed": {"api_key": "embed-pushed"}})
        key_path.write_text("rotated")
        assert proxy.reload_key_files(table) == ["chat"]
        assert table.match("/v1/models")[1] == "rotated"
        assert table.match("/embed/v1/embeddings")[1] == "embed-pushed"
        # An unreadable file keeps the last good key.
        key_path.unlink()
        assert proxy.reload_key_files(table) == []
        assert table.match("/v1/models")[1] == "rotated"
    finally:
        os.environ.clear()
        os.environ.update(saved)


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
    proxy.InferenceHandler.upstream_timeout = 5
    proxy.InferenceHandler.stream_idle_timeout = 5
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


class QuietServer(ThreadingHTTPServer):
    # Dropped connections are the point of these tests; skip the tracebacks.
    def handle_error(self, request, client_address):
        return


def serve(handler_cls) -> ThreadingHTTPServer:
    server = QuietServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_proxy(proxy, upstream_port: int, upstream_timeout: float, idle_timeout: float) -> ThreadingHTTPServer:
    proxy.InferenceHandler.table = proxy.Table(
        proxy.Route(f"http://127.0.0.1:{upstream_port}/v1", ""),
        proxy.Route("", ""),
        proxy.Route("", ""),
    )
    proxy.InferenceHandler.upstream_timeout = upstream_timeout
    proxy.InferenceHandler.stream_idle_timeout = idle_timeout
    return serve(proxy.InferenceHandler)


def open_stream(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
    return sock


def read_until(sock: socket.socket, needle: bytes, timeout: float) -> bytes:
    """Read until needle shows up, EOF, or timeout. Returns what arrived."""
    sock.settimeout(timeout)
    data = b""
    deadline = time.monotonic() + timeout
    while needle not in data and time.monotonic() < deadline:
        try:
            part = sock.recv(65536)
        except socket.timeout:
            break
        if not part:
            break
        data += part
    return data


class ChunkedUpstream(BaseHTTPRequestHandler):
    """Sends one SSE event, then waits on ``release`` before finishing."""

    protocol_version = "HTTP/1.1"
    release = threading.Event()

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        event = b'data: {"tok":0}\n\n'
        self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
        self.wfile.flush()
        if self.release.wait(5):
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()


def test_streams_each_chunk(proxy) -> None:
    # A small event must reach the client while the upstream is still open.
    ChunkedUpstream.release = threading.Event()
    upstream = serve(ChunkedUpstream)
    inference = start_proxy(proxy, upstream.server_address[1], 5, 5)
    sock = open_stream(inference.server_address[1])
    try:
        first = read_until(sock, b'"tok":0', 2)
        assert b'"tok":0' in first, first
        ChunkedUpstream.release.set()
        rest = read_until(sock, b"0\r\n\r\n", 2)
        assert (first + rest).endswith(b"0\r\n\r\n")
    finally:
        sock.close()
        upstream.shutdown()
        inference.shutdown()


def test_stall_after_headers_drops_connection(proxy) -> None:
    # Headers are out, then the upstream goes quiet. The client must get EOF
    # with no terminating chunk and no 502 written into the body.
    ChunkedUpstream.release = threading.Event()
    upstream = serve(ChunkedUpstream)
    inference = start_proxy(proxy, upstream.server_address[1], 5, 0.5)
    sock = open_stream(inference.server_address[1])
    try:
        began = time.monotonic()
        data = read_until(sock, b"\x00never", 3)
        elapsed = time.monotonic() - began
        assert elapsed < 2.5, "proxy kept a stalled stream open for %.1fs" % elapsed
        assert b'"tok":0' in data
        assert b"502" not in data and b"upstream request failed" not in data
        assert not data.endswith(b"0\r\n\r\n")
    finally:
        ChunkedUpstream.release.set()
        sock.close()
        upstream.shutdown()
        inference.shutdown()


def test_client_leaving_closes_upstream(proxy) -> None:
    # The client hangs up during prefill (no headers yet). The upstream
    # request must be closed, not left running until LLM_TIMEOUT.
    closed = threading.Event()

    class Prefill(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            self.connection.settimeout(5)
            try:
                if self.connection.recv(1, socket.MSG_PEEK) == b"":
                    closed.set()
            except OSError:
                closed.set()
            self.close_connection = True

    upstream = serve(Prefill)
    inference = start_proxy(proxy, upstream.server_address[1], 30, 30)
    sock = open_stream(inference.server_address[1])
    try:
        time.sleep(0.3)
        sock.close()
        assert closed.wait(3), "upstream request stayed open after the client left"
    finally:
        upstream.shutdown()
        inference.shutdown()


def test_slow_headers_still_502(proxy) -> None:
    # Before headers, a timeout is still reported as a plain 502.
    class Slow(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            time.sleep(2)
            self.close_connection = True

    upstream = serve(Slow)
    inference = start_proxy(proxy, upstream.server_address[1], 0.5, 0.5)
    sock = open_stream(inference.server_address[1])
    try:
        data = read_until(sock, b"upstream request failed", 3)
        assert data.startswith(b"HTTP/1.1 502"), data
    finally:
        sock.close()
        upstream.shutdown()
        inference.shutdown()


def main() -> None:
    proxy = load_proxy()
    test_routes(proxy)
    test_authorization(proxy)
    test_admin_stays_in_memory(proxy, Path(os.getcwd()))
    test_round_trip(proxy)
    test_streams_each_chunk(proxy)
    test_stall_after_headers_drops_connection(proxy)
    test_client_leaving_closes_upstream(proxy)
    test_slow_headers_still_502(proxy)
    with tempfile.TemporaryDirectory() as tmp:
        test_key_files(proxy, Path(tmp))
    print("OK llm proxy")


if __name__ == "__main__":
    main()
