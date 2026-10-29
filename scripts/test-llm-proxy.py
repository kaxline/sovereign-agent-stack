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
from urllib.error import HTTPError
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


def test_default_max_tokens(proxy) -> None:
    add = proxy.with_default_max_tokens
    chat = b'{"model":"m","stream":true}'
    assert json.loads(add(chat, "/v1/chat/completions", "chat", 16384)) == {
        "model": "m", "stream": True, "max_tokens": 16384,
    }
    assert json.loads(add(b'{"prompt":"p"}', "/lightrag/v1/completions", "lightrag", 8))["max_tokens"] == 8
    assert json.loads(add(b'{"input":"i"}', "/v1/responses", "chat", 8)) == {"input": "i", "max_output_tokens": 8}
    # A request that set its own limit keeps it, whichever field it used.
    for own in (b'{"max_tokens":50}', b'{"max_completion_tokens":50}', b'{"max_output_tokens":50}'):
        assert add(own, "/v1/chat/completions", "chat", 16384) == own
    # An explicit null is no limit at all.
    assert json.loads(add(b'{"max_tokens":null}', "/v1/chat/completions", "chat", 9))["max_tokens"] == 9
    # Off, other paths, embeddings and bodies that are not a JSON object pass through.
    assert add(chat, "/v1/chat/completions", "chat", 0) == chat
    assert add(chat, "/v1/models", "chat", 16384) == chat
    assert add(chat, "/embed/v1/completions", "embed", 16384) == chat
    for raw in (b"", b"not json", b"[1,2]", b"\xff\xfe"):
        assert add(raw, "/v1/chat/completions", "chat", 16384) == raw


def test_default_max_tokens_reaches_upstream(proxy) -> None:
    seen = {}

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            seen["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    upstream = serve(Upstream)
    inference = start_proxy(proxy, upstream.server_address[1], 5, 5)
    proxy.InferenceHandler.default_max_tokens = 123
    try:
        req = Request(
            f"http://127.0.0.1:{inference.server_address[1]}/v1/chat/completions",
            data=b'{"model":"m"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            resp.read()
        assert json.loads(seen["body"]) == {"model": "m", "max_tokens": 123}
    finally:
        proxy.InferenceHandler.default_max_tokens = 0
        upstream.shutdown()
        inference.shutdown()


def test_stream_max_seconds_drops_endless_stream(proxy) -> None:
    # The upstream never goes quiet, so only the total limit can end it.
    stop = threading.Event()

    class Endless(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            event = b'data: {"tok":1}\n\n'
            try:
                while not stop.is_set():
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            self.close_connection = True

    upstream = serve(Endless)
    inference = start_proxy(proxy, upstream.server_address[1], 5, 5)
    proxy.InferenceHandler.stream_max_seconds = 1
    sock = open_stream(inference.server_address[1])
    try:
        began = time.monotonic()
        data = read_until(sock, b"\x00never", 4)
        elapsed = time.monotonic() - began
        assert 0.8 < elapsed < 3, "endless stream ran %.1fs against a 1s limit" % elapsed
        assert b'"tok":1' in data
        assert not data.endswith(b"0\r\n\r\n")
    finally:
        proxy.InferenceHandler.stream_max_seconds = 0
        stop.set()
        sock.close()
        upstream.shutdown()
        inference.shutdown()


def test_admin_view_reports_restart(proxy) -> None:
    table = proxy.Table(proxy.Route("", ""), proxy.Route("", ""), proxy.Route("", ""))
    view = table.public_view()
    assert set(view) == {"chat", "lightrag", "embed", "proxy"}
    assert view["chat"] == {"url": "", "api_key_set": False, "pushed": False, "fallbacks": []}
    assert "fallbacks" not in view["embed"]
    assert view["proxy"]["boot_id"] and isinstance(view["proxy"]["started_at"], int)
    # An empty key still counts as pushed: a local server needs none.
    table.update({"chat": {"url": "http://lmstudio.example/v1", "api_key": ""}})
    view = table.public_view()
    assert view["chat"]["pushed"] is True and view["chat"]["api_key_set"] is False
    assert view["lightrag"]["pushed"] is False
    other = proxy.Table(proxy.Route("", ""), proxy.Route("", ""), proxy.Route("", ""))
    assert other.public_view()["proxy"]["boot_id"] != view["proxy"]["boot_id"]


def test_await_push_env(proxy) -> None:
    saved = dict(os.environ)
    try:
        os.environ["LLM_PROXY_AWAIT_PUSH"] = " Chat, lightrag ,bogus,"
        assert proxy.await_push_routes() == ("chat", "lightrag")
        os.environ["LLM_PROXY_AWAIT_PUSH"] = ""
        assert proxy.await_push_routes() == ()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_await_push(proxy) -> None:
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            seen.append(self.headers.get("Authorization"))
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = serve(Upstream)
    url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    table = proxy.Table(proxy.Route("", ""), proxy.Route("", ""), proxy.Route(url, ""), ("chat",))
    proxy.InferenceHandler.table = table
    proxy.InferenceHandler.upstream_timeout = 5
    proxy.InferenceHandler.stream_idle_timeout = 5
    inference = serve(proxy.InferenceHandler)
    base = f"http://127.0.0.1:{inference.server_address[1]}"

    def post(path: str) -> tuple[int, bytes]:
        req = Request(base + path, data=b"{}", headers={"Authorization": "Bearer local-llm"}, method="POST")
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    try:
        # Health stays green with no keys and a route waiting for a push.
        with urlopen(base + "/health", timeout=5) as resp:
            assert resp.status == 200
        status, body = post("/v1/chat/completions")
        assert status == 503, status
        assert b"route chat" in body and b"admin" in body
        assert seen == []
        # An unlisted route with an empty key is a local server, as before.
        status, _ = post("/embed/v1/embeddings")
        assert status == 200 and seen == [None]
        table.update({"chat": {"url": url, "api_key": "pushed-secret"}})
        status, _ = post("/v1/chat/completions")
        assert status == 200 and seen[-1] == "Bearer pushed-secret"
    finally:
        upstream.shutdown()
        inference.shutdown()


def free_port() -> int:
    """A port with nothing listening, for a primary that is down."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def recording_upstream(status: int = 200, headers: dict | None = None):
    """An upstream that records each request and answers with ``status``."""
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            seen.append({"authorization": self.headers.get("Authorization"), "body": self.rfile.read(length)})
            payload = b'{"ok":true}'
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return serve(Upstream), seen


def start_chain(proxy, primary: str, fallbacks: list) -> ThreadingHTTPServer:
    proxy.InferenceHandler.table = proxy.Table(
        proxy.Route(primary, "primary-secret", proxy.parse_fallbacks("chat", fallbacks)),
        proxy.Route("", ""),
        proxy.Route("", ""),
    )
    proxy.InferenceHandler.upstream_timeout = 5
    proxy.InferenceHandler.stream_idle_timeout = 5
    proxy.InferenceHandler.connect_timeout = 2
    return serve(proxy.InferenceHandler)


def post_chat(port: int, path: str = "/v1/chat/completions") -> tuple[int, dict, bytes]:
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=b'{"model":"local-model"}',
        headers={"Authorization": "Bearer local-llm", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_fallback_when_primary_down(proxy) -> None:
    upstream, seen = recording_upstream()
    url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    inference = start_chain(
        proxy, f"http://127.0.0.1:{free_port()}/v1", [{"url": url, "api_key": "cloud-secret", "model": "cloud-model"}]
    )
    try:
        status, headers, _ = post_chat(inference.server_address[1])
        assert status == 200, status
        assert headers.get("X-LLM-Proxy-Upstream") == "chat;1", headers
        assert seen[0]["authorization"] == "Bearer cloud-secret"
        assert json.loads(seen[0]["body"])["model"] == "cloud-model"
        # LightRAG with no upstream of its own uses the chat chain, fallbacks too.
        status, headers, _ = post_chat(inference.server_address[1], "/lightrag/v1/chat/completions")
        assert status == 200 and headers.get("X-LLM-Proxy-Upstream") == "lightrag;1", headers
    finally:
        upstream.shutdown()
        inference.shutdown()


def test_primary_serves_with_index_zero(proxy) -> None:
    upstream, seen = recording_upstream()
    url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
    inference = start_chain(proxy, url, [{"url": f"http://127.0.0.1:{free_port()}/v1"}])
    try:
        status, headers, _ = post_chat(inference.server_address[1])
        assert status == 200 and headers.get("X-LLM-Proxy-Upstream") == "chat;0", headers
        # No model on the entry, and the primary never rewrites one.
        assert json.loads(seen[0]["body"])["model"] == "local-model"
        assert seen[0]["authorization"] == "Bearer primary-secret"
    finally:
        upstream.shutdown()
        inference.shutdown()


def test_fallback_on_retry_status(proxy) -> None:
    busy, busy_seen = recording_upstream(503)
    good, good_seen = recording_upstream()
    inference = start_chain(
        proxy,
        f"http://127.0.0.1:{busy.server_address[1]}/v1",
        [{"url": f"http://127.0.0.1:{good.server_address[1]}/v1"}],
    )
    try:
        status, headers, body = post_chat(inference.server_address[1])
        assert status == 200 and body == b'{"ok":true}', (status, body)
        assert headers.get("X-LLM-Proxy-Upstream") == "chat;1"
        assert len(busy_seen) == 1 and len(good_seen) == 1
        # An empty fallback key is a local server: no key, no placeholder.
        assert good_seen[0]["authorization"] is None
    finally:
        busy.shutdown()
        good.shutdown()
        inference.shutdown()


def test_last_upstream_failure_passes_through(proxy) -> None:
    # With nothing left to try, the client gets the last upstream's answer.
    busy, _ = recording_upstream(429)
    inference = start_chain(
        proxy,
        f"http://127.0.0.1:{free_port()}/v1",
        [{"url": f"http://127.0.0.1:{busy.server_address[1]}/v1"}],
    )
    try:
        status, headers, _ = post_chat(inference.server_address[1])
        assert status == 429 and headers.get("X-LLM-Proxy-Upstream") == "chat;1", (status, headers)
    finally:
        busy.shutdown()
        inference.shutdown()
    # Every upstream down: the usual 502.
    inference = start_chain(
        proxy, f"http://127.0.0.1:{free_port()}/v1", [{"url": f"http://127.0.0.1:{free_port()}/v1"}]
    )
    try:
        status, _, body = post_chat(inference.server_address[1])
        assert status == 502 and b"upstream request failed" in body
    finally:
        inference.shutdown()


def test_no_fallback_after_stream_starts(proxy) -> None:
    calls = []

    class Breaks(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            calls.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            event = b"data: partial\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)

    primary = serve(Breaks)
    fallback, fallback_seen = recording_upstream()
    inference = start_chain(
        proxy,
        f"http://127.0.0.1:{primary.server_address[1]}/v1",
        [{"url": f"http://127.0.0.1:{fallback.server_address[1]}/v1"}],
    )
    sock = open_stream(inference.server_address[1])
    try:
        data = read_until(sock, b"\x00never", 3)
        assert data.startswith(b"HTTP/1.1 200") and b"chat;0" in data and b"partial" in data
        assert not data.endswith(b"0\r\n\r\n")
        assert calls == [1] and fallback_seen == []
    finally:
        sock.close()
        primary.shutdown()
        fallback.shutdown()
        inference.shutdown()


def test_upstream_served_header_passes_through(proxy) -> None:
    upstream, _ = recording_upstream(headers={"X-LLM-Proxy-Upstream": "other-proxy;2"})
    inference = start_chain(proxy, f"http://127.0.0.1:{upstream.server_address[1]}/v1", [])
    try:
        _, headers, _ = post_chat(inference.server_address[1])
        assert headers.get("X-LLM-Proxy-Upstream") == "other-proxy;2", headers
    finally:
        upstream.shutdown()
        inference.shutdown()


def test_admin_fallbacks(proxy) -> None:
    table = proxy.Table(proxy.Route("http://local.example/v1", ""), proxy.Route("", ""), proxy.Route("", ""))
    table.update({"chat": {"fallbacks": [{"url": "https://cloud.example/api/v1", "api_key": "sk-cloud", "model": "m"}]}})
    view = table.public_view()
    assert view["chat"]["fallbacks"] == [{"url": "https://cloud.example/api/v1", "model": "m", "api_key_set": True}]
    assert "sk-cloud" not in json.dumps(view)
    # Leaving fallbacks out keeps them; [] clears them.
    table.update({"chat": {"api_key": "k"}})
    assert len(table.public_view()["chat"]["fallbacks"]) == 1
    table.update({"chat": {"fallbacks": []}})
    assert table.public_view()["chat"]["fallbacks"] == []
    # A bad push changes nothing, keys included, and the error names no key.
    for bad in (
        {"embed": {"fallbacks": [{"url": "http://e.example/v1"}]}},
        {"chat": {"api_key": "new", "fallbacks": [{"url": "ftp://x", "api_key": "sk-leak"}]}},
        {"chat": {"fallbacks": {"url": "http://x.example/v1"}}},
        {"lightrag": {"fallbacks": [{"url": "http://x.example/v1", "model": 3}]}},
    ):
        try:
            table.update(bad)
        except ValueError as exc:
            assert "sk-leak" not in str(exc)
        else:
            raise AssertionError("accepted %r" % bad)
    assert table.route_for("chat").api_key == "k"


def test_admin_rejects_bad_fallbacks_over_http(proxy) -> None:
    proxy.AdminHandler.table = proxy.Table(proxy.Route("", ""), proxy.Route("", ""), proxy.Route("", ""))
    proxy.AdminHandler.token = "admin-token"
    admin = serve(proxy.AdminHandler)
    try:
        req = Request(
            f"http://127.0.0.1:{admin.server_address[1]}/admin/upstreams",
            data=json.dumps({"embed": {"fallbacks": []}}).encode(),
            headers={"Authorization": "Bearer admin-token"},
            method="POST",
        )
        try:
            urlopen(req, timeout=5)
        except HTTPError as exc:
            assert exc.code == 400 and b"embed" in exc.read()
        else:
            raise AssertionError("embed fallbacks accepted")
    finally:
        admin.shutdown()


def test_fallbacks_file(proxy, tmp: Path) -> None:
    saved = dict(os.environ)
    path = tmp / "fallbacks.json"
    try:
        os.environ.clear()
        os.environ["LLM_BINDING_HOST"] = "http://local.example/v1"
        os.environ["LLM_PROXY_AWAIT_PUSH"] = "chat"
        os.environ["LLM_PROXY_FALLBACKS_FILE"] = str(path)
        path.write_text(json.dumps({"chat": [{"url": "https://a.example/v1", "api_key": "sk-a", "model": "a"}]}))
        table = proxy.load_table()
        assert table.route_for("chat").fallbacks[0].api_key == "sk-a"
        path.write_text(json.dumps({"lightrag": [{"url": "https://b.example/v1"}]}))
        assert proxy.reload_key_files(table) == ["chat.fallbacks", "lightrag.fallbacks"]
        # The file is the whole list: chat is cleared, lightrag set.
        assert table.public_view()["chat"]["fallbacks"] == []
        assert table.public_view()["lightrag"]["fallbacks"][0]["url"] == "https://b.example/v1"
        # Reloading fallbacks is not a push.
        assert table.waiting("chat")
        # A broken file keeps what is loaded.
        path.write_text("{not json")
        assert proxy.reload_key_files(table) == []
        assert len(table.public_view()["lightrag"]["fallbacks"]) == 1
        path.write_text(json.dumps({"embed": [{"url": "https://e.example/v1"}]}))
        assert proxy.reload_key_files(table) == []
        # A broken file at start means no fallbacks, not a crash.
        assert proxy.load_table().route_for("chat").fallbacks == []
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_with_model(proxy) -> None:
    assert json.loads(proxy.with_model(b'{"model":"a","x":1}', "b")) == {"model": "b", "x": 1}
    for raw in (b"", b"not json", b"[1]"):
        assert proxy.with_model(raw, "b") == raw
    assert proxy.with_model(b'{"model":"a"}', "") == b'{"model":"a"}'


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
    test_default_max_tokens(proxy)
    test_default_max_tokens_reaches_upstream(proxy)
    test_stream_max_seconds_drops_endless_stream(proxy)
    test_admin_view_reports_restart(proxy)
    test_with_model(proxy)
    test_admin_fallbacks(proxy)
    test_admin_rejects_bad_fallbacks_over_http(proxy)
    with tempfile.TemporaryDirectory() as tmp:
        test_fallbacks_file(proxy, Path(tmp))
    test_fallback_when_primary_down(proxy)
    test_primary_serves_with_index_zero(proxy)
    test_fallback_on_retry_status(proxy)
    test_last_upstream_failure_passes_through(proxy)
    test_no_fallback_after_stream_starts(proxy)
    test_upstream_served_header_passes_through(proxy)
    test_await_push_env(proxy)
    test_await_push(proxy)
    with tempfile.TemporaryDirectory() as tmp:
        test_key_files(proxy, Path(tmp))
    print("OK llm proxy")


if __name__ == "__main__":
    main()
