#!/usr/bin/env python3
"""OpenAI-compatible proxy. Real upstream keys stay in this process.

Clients send a placeholder. This process replaces Authorization and streams
the upstream response. Nothing here is written to disk. The admin listener
updates the in-memory table only; a restart loads the environment again.

Each key can come from a file instead of the environment: set
LLM_BINDING_API_KEY_FILE (and the LIGHTRAG_/EMBEDDING_ equivalents) to a path
such as /run/secrets/llm_key. SIGHUP or POST /admin/reload re-reads those
files, so a key can rotate without recreating the container and never shows
up in docker inspect.

LLM_TIMEOUT bounds the wait for upstream response headers, which covers
prompt prefill. LLM_STREAM_IDLE_TIMEOUT (default: LLM_TIMEOUT) bounds a gap in
the body once headers are out. Either failure after headers drops the client
connection, so the client sees a dead stream instead of a quiet one. A client
that hangs up closes the upstream request too, so a single-slot model server
stops working on a request nobody is reading.

A downstream app may start the proxy with no keys and push them through the
admin API. LLM_PROXY_AWAIT_PUSH lists routes (e.g. chat,lightrag) that answer
503 until an admin POST names them, instead of reaching the upstream without
a key. Unlisted routes keep the usual meaning of an empty key: a local server
that needs none. GET /admin/upstreams reports a per-process boot_id so the
admin client can tell the proxy restarted and push again.
"""

from __future__ import annotations

import hmac
import json
import os
import select
import signal
import socket
import sys
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "authorization",
}

# Longer prefixes first so /lightrag/v1 is not swallowed by /v1.
PREFIXES = (
    ("/lightrag/v1", "lightrag"),
    ("/embed/v1", "embed"),
    ("/v1", "chat"),
)


class Route:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key


ROUTES = ("chat", "lightrag", "embed")


class Table:
    """In-memory upstreams. Admin updates replace fields; they are not saved."""

    def __init__(self, chat: Route, lightrag: Route, embed: Route, await_push: tuple[str, ...] = ()) -> None:
        self._lock = threading.Lock()
        self.chat = chat
        self.lightrag = lightrag
        self.embed = embed
        self.await_push = frozenset(await_push)
        # Routes named in an admin POST since this process started.
        self.pushed: set[str] = set()
        # Changes on every start, so an admin client can spot a restart.
        self.boot_id = uuid.uuid4().hex
        self.started_at = int(time.time())

    def waiting(self, name: str) -> bool:
        """True while a route listed in LLM_PROXY_AWAIT_PUSH has had no push."""
        with self._lock:
            return name in self.await_push and name not in self.pushed

    def route_for(self, name: str) -> Route:
        with self._lock:
            chosen = getattr(self, name)
            # LightRAG falls back to the shared chat route when its own
            # upstream was left unset.
            if name == "lightrag" and not chosen.url.strip():
                return Route(self.chat.url, self.chat.api_key)
            return Route(chosen.url, chosen.api_key)

    def match(self, path: str) -> tuple[str, str, str] | None:
        """Return (forward_url, api_key, route_name) for an inference path."""
        split = urlsplit(path)
        raw = split.path or "/"
        name = None
        remainder = ""
        for prefix, route_name in PREFIXES:
            if raw == prefix or raw.startswith(prefix + "/"):
                name = route_name
                remainder = raw[len(prefix) :]
                break
        if name is None:
            return None
        route = self.route_for(name)
        target = join_upstream(route.url, remainder, split.query)
        return target or "", route.api_key, name

    def update(self, payload: dict) -> None:
        with self._lock:
            for name in ROUTES:
                item = payload.get(name)
                if not isinstance(item, dict):
                    continue
                self.pushed.add(name)
                route = getattr(self, name)
                if "url" in item and item["url"] is not None:
                    route.url = str(item["url"]).strip()
                if "api_key" in item and item["api_key"] is not None:
                    route.api_key = str(item["api_key"])

    def public_view(self) -> dict:
        with self._lock:
            view: dict = {
                name: {
                    "url": getattr(self, name).url,
                    "api_key_set": bool(getattr(self, name).api_key),
                    "pushed": name in self.pushed,
                }
                for name in ROUTES
            }
            view["proxy"] = {"boot_id": self.boot_id, "started_at": self.started_at}
            return view


def join_upstream(upstream: str, remainder: str, query: str) -> str | None:
    """Join an upstream base with the path that followed the proxy prefix.

    ``http://host:1234/v1`` + ``/chat/completions`` stays on ``/v1``.
    A native host with no path (``http://ollama:11434``) gains ``/v1`` so
    OpenAI clients still speak the compatible API. The key never appears
    in the URL.
    """
    upstream = (upstream or "").strip()
    if not upstream:
        return None
    parsed = urlsplit(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    base_path = parsed.path.rstrip("/")
    if not base_path:
        base_path = "/v1"
    elif not base_path.endswith("/v1"):
        base_path = base_path + "/v1"
    if remainder and not remainder.startswith("/"):
        remainder = "/" + remainder
    path = base_path + remainder
    # Drop userinfo so a mis-set URL cannot leak into logs via the target.
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, path, query, ""))


# Route name -> environment prefix for <prefix>_HOST and <prefix>_API_KEY[_FILE].
ENV_PREFIXES = {
    "chat": "LLM_BINDING",
    "lightrag": "LIGHTRAG_LLM_BINDING",
    "embed": "EMBEDDING_BINDING",
}


def key_file(name: str) -> str:
    return os.environ.get(f"{ENV_PREFIXES[name]}_API_KEY_FILE", "").strip()


def read_key_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        sys.stderr.write("llm-proxy: cannot read key file %s: %s\n" % (path, exc.strerror))
        return None


def initial_key(name: str) -> str:
    path = key_file(name)
    if path:
        return read_key_file(path) or ""
    return os.environ.get(f"{ENV_PREFIXES[name]}_API_KEY", "")


def await_push_routes() -> tuple[str, ...]:
    names = []
    for raw in os.environ.get("LLM_PROXY_AWAIT_PUSH", "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in ROUTES:
            sys.stderr.write("llm-proxy: LLM_PROXY_AWAIT_PUSH: unknown route %r ignored\n" % name)
            continue
        names.append(name)
    return tuple(names)


def load_table() -> Table:
    routes = {
        name: Route(os.environ.get(f"{prefix}_HOST", ""), initial_key(name))
        for name, prefix in ENV_PREFIXES.items()
    }
    return Table(routes["chat"], routes["lightrag"], routes["embed"], await_push_routes())


def reload_key_files(table: Table) -> list[str]:
    """Re-read every route whose key comes from a file. Returns the routes reloaded.

    Routes without a *_FILE keep their current key, including one pushed
    through the admin API. An unreadable file also keeps the current key, so
    a non-atomic replace does not drop requests mid-rotation.
    """
    payload = {}
    for name in ENV_PREFIXES:
        path = key_file(name)
        if path:
            key = read_key_file(path)
            if key is not None:
                payload[name] = {"api_key": key}
    table.update(payload)
    return sorted(payload)


def outbound_headers(inbound: list[tuple[str, str]], api_key: str) -> list[tuple[str, str]]:
    """Copy request headers. A configured key replaces Authorization.

    An empty key means a local server: do not inject a key, and do not
    forward the client's placeholder either.
    """
    kept = []
    for key, value in inbound:
        if key.lower() in HOP:
            continue
        kept.append((key, value))
    if api_key:
        kept.append(("Authorization", f"Bearer {api_key}"))
    return kept


def _connection(url: str, timeout: float):
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return HTTPSConnection(parsed.hostname, port, timeout=timeout)
    return HTTPConnection(parsed.hostname, port, timeout=timeout)


def _request_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        return path + "?" + parsed.query
    return path


class ClientWatcher:
    """Close the upstream socket when the client hangs up.

    While the model is prefilling, or between tokens, nothing is written to the
    client, so a write error cannot reveal that it left. This polls the client
    socket for EOF and shuts the upstream socket down, which unblocks the
    handler's read and drops the upstream request.
    """

    POLL = 0.25

    def __init__(self, client: socket.socket, upstream: socket.socket | None) -> None:
        self._client = client
        self._upstream = upstream
        self._lock = threading.Lock()
        self._stopped = False
        self.client_gone = False
        if upstream is not None:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
            try:
                readable, _, _ = select.select([self._client], [], [], self.POLL)
                if not readable:
                    continue
                data = self._client.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                data = b""
            if data:
                # A pipelined request, not a hang-up. It cannot be told apart
                # from here without consuming it, so stop watching.
                return
            with self._lock:
                if self._stopped:
                    return
                self.client_gone = True
                try:
                    self._upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return

    def stop(self) -> None:
        with self._lock:
            self._stopped = True


class InferenceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    table: Table
    # Wait for upstream response headers (covers prefill).
    upstream_timeout: float = 600
    # Longest gap between upstream body bytes once headers are out.
    stream_idle_timeout: float = 600
    # StreamRequestHandler's own attribute: the inbound socket timeout, which
    # closes idle keep-alive connections and stalled request bodies.
    timeout: float = 600

    def log_message(self, fmt: str, *args) -> None:
        # Method and path only. Never the headers, which carry the key.
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        matched = self.table.match(self.path)
        if matched is None:
            self._text(404, "not found\n")
            return
        target, api_key, name = matched
        if self.table.waiting(name):
            # The request body is not read, so do not reuse the connection.
            self.close_connection = True
            self._text(
                503,
                "llm-proxy: no upstream for route %s yet; the admin client pushes it "
                "(POST /admin/upstreams)\n" % name,
            )
            return
        if not target:
            self._text(502, "upstream is not configured\n")
            return
        body = self._read_body()
        headers = outbound_headers(list(self.headers.items()), api_key)
        conn = None
        watcher = None
        started = False
        began = time.monotonic()
        try:
            conn = _connection(target, self.upstream_timeout)
            conn.putrequest(self.command, _request_path(target), skip_host=True, skip_accept_encoding=True)
            for key, value in headers:
                conn.putheader(key, value)
            conn.putheader("Host", urlsplit(target).netloc)
            if body:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body if body else None)
            # Keep our own reference: getresponse() drops conn.sock when the
            # upstream will close, but the response still reads from it.
            upstream_sock = conn.sock
            watcher = ClientWatcher(self.connection, upstream_sock)
            resp = conn.getresponse()
            if upstream_sock is not None:
                upstream_sock.settimeout(self.stream_idle_timeout)
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() in HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            started = True
            while True:
                # read1 returns what has arrived. read(n) would hold small SSE
                # events until n bytes collect, which looks like a stall.
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if watcher.client_gone:
                raise ConnectionError("client closed")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as exc:
            gone = watcher is not None and watcher.client_gone
            elapsed = time.monotonic() - began
            path = self.path.split("?", 1)[0]
            if gone:
                sys.stderr.write("llm-proxy: client left %s after %.0fs; closed upstream\n" % (path, elapsed))
            else:
                sys.stderr.write("llm-proxy: upstream %s failed after %.0fs: %s\n" % (path, elapsed, type(exc).__name__))
            if started or gone:
                # Headers are already out (or nobody is listening). A 502 now
                # would land inside the chunked body. Drop the connection so
                # the client sees a dead stream.
                self.close_connection = True
            else:
                try:
                    self._text(502, "upstream request failed\n")
                except Exception:
                    self.close_connection = True
        finally:
            if watcher is not None:
                watcher.stop()
            if conn is not None:
                conn.close()

    def _text(self, status: int, message: str) -> None:
        data = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    table: Table
    token: str = ""

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("admin %s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlsplit(self.path).path != "/admin/upstreams":
            self._json(404, {"error": "not found"})
            return
        self._json(200, self.table.public_view())

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlsplit(self.path).path == "/admin/reload":
            reloaded = reload_key_files(self.table)
            self._json(200, {"reloaded": reloaded, "upstreams": self.table.public_view()})
            return
        if urlsplit(self.path).path != "/admin/upstreams":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "expected an object"})
            return
        self.table.update(payload)
        self._json(200, self.table.public_view())

    def _authorized(self) -> bool:
        if not self.token:
            self._json(401, {"error": "admin token is not configured"})
            return False
        header = self.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else ""
        if not presented or not hmac.compare_digest(presented, self.token):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    table = load_table()
    timeout = float(os.environ.get("LLM_TIMEOUT", "600") or "600")
    idle_timeout = float(os.environ.get("LLM_STREAM_IDLE_TIMEOUT", "") or timeout)
    infer_port = int(os.environ.get("LLM_PROXY_PORT", "4000"))
    admin_port = int(os.environ.get("LLM_PROXY_ADMIN_PORT", "4001"))
    token = os.environ.get("LLM_PROXY_ADMIN_TOKEN", "")

    inference = ThreadingHTTPServer(("0.0.0.0", infer_port), InferenceHandler)
    inference.daemon_threads = True
    InferenceHandler.table = table
    InferenceHandler.upstream_timeout = timeout
    InferenceHandler.stream_idle_timeout = idle_timeout
    InferenceHandler.timeout = timeout

    # Listen on all interfaces so Docker can publish the port. Compose binds
    # that publish to 127.0.0.1 on the host. The bearer token is the gate.
    admin = ThreadingHTTPServer(("0.0.0.0", admin_port), AdminHandler)
    admin.daemon_threads = True
    AdminHandler.table = table
    AdminHandler.token = token

    def on_hup(signum, frame) -> None:
        reloaded = reload_key_files(table)
        sys.stderr.write("llm-proxy: SIGHUP reloaded key files: %s\n" % (", ".join(reloaded) or "none"))

    signal.signal(signal.SIGHUP, on_hup)

    if table.await_push:
        sys.stderr.write(
            "llm-proxy: waiting for an admin push on: %s\n" % ", ".join(sorted(table.await_push))
        )

    threading.Thread(target=admin.serve_forever, daemon=True).start()
    sys.stderr.write("llm-proxy inference :%s admin :%s\n" % (infer_port, admin_port))
    inference.serve_forever()


if __name__ == "__main__":
    main()
