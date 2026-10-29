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

Both of those watch for silence, so a model that never stops producing tokens
trips neither. Two limits cover that case. LLM_DEFAULT_MAX_TOKENS (default
16384) adds max_tokens to a chat or completions request that sets no output
limit of its own. LLM_STREAM_MAX_SECONDS (default 3600) bounds one request
from start to finish, and is dropped the same way as an idle stream. 0 turns
either off.

A downstream app may start the proxy with no keys and push them through the
admin API. LLM_PROXY_AWAIT_PUSH lists routes (e.g. chat,lightrag) that answer
503 until an admin POST names them, instead of reaching the upstream without
a key. Unlisted routes keep the usual meaning of an empty key: a local server
that needs none. GET /admin/upstreams reports a per-process boot_id so the
admin client can tell the proxy restarted and push again.

The chat and lightrag routes can each carry an ordered list of fallback
upstreams ({url, api_key, model?}), set by POST /admin/upstreams or by the JSON
file at LLM_PROXY_FALLBACKS_FILE, which SIGHUP and /admin/reload re-read. The
proxy moves to the next upstream when one cannot be connected to within
LLM_CONNECT_TIMEOUT (default 10s; it applies only to an upstream with another
after it) or answers 429, 500, 502, 503 or 504. It does
not move on after a timeout waiting for headers, since that covers prefill, and
never once the response has started. A fallback that names a model gets the
request with that model. Every proxied response carries X-LLM-Proxy-Upstream:
<route>;<index> (0 = primary) unless the upstream sent its own. The embed
route has no fallbacks: another model's vectors would break the index.
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


class Upstream:
    """One fallback entry. ``model``, when set, replaces the request's model."""

    def __init__(self, url: str, api_key: str = "", model: str = "") -> None:
        self.url = url
        self.api_key = api_key
        self.model = model


class Route:
    def __init__(self, url: str, api_key: str, fallbacks: list[Upstream] | tuple = ()) -> None:
        self.url = url
        self.api_key = api_key
        # Tried in order after the primary. See the module docstring.
        self.fallbacks = list(fallbacks)


ROUTES = ("chat", "lightrag", "embed")
# Embeddings from another model would land in the same vector index and
# break retrieval without an error, so embed has no fallbacks.
FALLBACK_ROUTES = ("chat", "lightrag")
# Statuses that move on to the next upstream. The response has not been
# forwarded yet, so the client never sees the failed attempt.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Names the route and the upstream index that served (0 = primary).
SERVED_HEADER = "X-LLM-Proxy-Upstream"


def parse_fallbacks(name: str, value) -> list[Upstream]:
    """Validate a ``fallbacks`` list from the admin API or the fallbacks file.

    Raises ValueError with a message that names the route and index only,
    never a key.
    """
    if name not in FALLBACK_ROUTES:
        raise ValueError("route %s takes no fallbacks" % name)
    if not isinstance(value, list):
        raise ValueError("%s.fallbacks must be a list" % name)
    parsed = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("%s.fallbacks[%d] must be an object" % (name, index))
        url = str(item.get("url") or "").strip()
        if join_upstream(url, "", "") is None:
            raise ValueError("%s.fallbacks[%d].url must be an http(s) URL" % (name, index))
        api_key = item.get("api_key")
        model = item.get("model")
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError("%s.fallbacks[%d].api_key must be a string" % (name, index))
        if model is not None and not isinstance(model, str):
            raise ValueError("%s.fallbacks[%d].model must be a string" % (name, index))
        parsed.append(Upstream(url, api_key or "", (model or "").strip()))
    return parsed


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
            # LightRAG falls back to the shared chat route, fallbacks
            # included, when its own upstream was left unset.
            if name == "lightrag" and not chosen.url.strip():
                chosen = self.chat
            return Route(chosen.url, chosen.api_key, chosen.fallbacks)

    def match(self, path: str) -> tuple[str, str, str] | None:
        """Return (forward_url, api_key, route_name) for the primary upstream."""
        found = self.chain(path)
        if found is None:
            return None
        name, attempts = found
        target, api_key, _ = attempts[0]
        return target, api_key, name

    def chain(self, path: str) -> tuple[str, list[tuple[str, str, str]]] | None:
        """Return (route_name, [(forward_url, api_key, model), ...]) for a path.

        The primary comes first, then each fallback. A forward_url is "" when
        that upstream is not configured.
        """
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
        attempts = [(join_upstream(route.url, remainder, split.query) or "", route.api_key, "")]
        for fallback in route.fallbacks:
            attempts.append((join_upstream(fallback.url, remainder, split.query) or "", fallback.api_key, fallback.model))
        return name, attempts

    def update(self, payload: dict) -> None:
        """Apply an admin push. Raises ValueError, changing nothing, on a bad ``fallbacks``.

        ``fallbacks`` replaces the route's whole list; ``[]`` clears it and
        leaving it out keeps the current one.
        """
        fallbacks = {}
        for name in ROUTES:
            item = payload.get(name)
            if isinstance(item, dict) and item.get("fallbacks") is not None:
                fallbacks[name] = parse_fallbacks(name, item["fallbacks"])
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
                if name in fallbacks:
                    route.fallbacks = fallbacks[name]

    def set_fallbacks(self, fallbacks: dict[str, list[Upstream]]) -> None:
        """Replace every route's fallbacks; a route left out gets none.

        Unlike update(), this does not count as a push, so a route waiting
        under LLM_PROXY_AWAIT_PUSH keeps waiting.
        """
        with self._lock:
            for name in FALLBACK_ROUTES:
                getattr(self, name).fallbacks = list(fallbacks.get(name, ()))

    def public_view(self) -> dict:
        with self._lock:
            view: dict = {}
            for name in ROUTES:
                route = getattr(self, name)
                view[name] = {"url": route.url, "api_key_set": bool(route.api_key), "pushed": name in self.pushed}
                if name in FALLBACK_ROUTES:
                    view[name]["fallbacks"] = [
                        {"url": f.url, "model": f.model, "api_key_set": bool(f.api_key)} for f in route.fallbacks
                    ]
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


def fallbacks_file() -> str:
    return os.environ.get("LLM_PROXY_FALLBACKS_FILE", "").strip()


def read_fallbacks_file(path: str) -> dict[str, list[Upstream]] | None:
    """Parse ``{"chat": [...], "lightrag": [...]}``. None when unreadable or invalid.

    Errors name the file and the problem, never an entry's key.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        sys.stderr.write("llm-proxy: cannot read fallbacks file %s: %s\n" % (path, exc.strerror))
        return None
    except ValueError:
        sys.stderr.write("llm-proxy: fallbacks file %s is not valid JSON\n" % path)
        return None
    if not isinstance(raw, dict):
        sys.stderr.write("llm-proxy: fallbacks file %s must hold an object\n" % path)
        return None
    try:
        return {name: parse_fallbacks(name, value) for name, value in raw.items()}
    except ValueError as exc:
        sys.stderr.write("llm-proxy: fallbacks file %s: %s\n" % (path, exc))
        return None


def load_table() -> Table:
    routes = {
        name: Route(os.environ.get(f"{prefix}_HOST", ""), initial_key(name))
        for name, prefix in ENV_PREFIXES.items()
    }
    path = fallbacks_file()
    if path:
        for name, fallbacks in (read_fallbacks_file(path) or {}).items():
            routes[name].fallbacks = fallbacks
    return Table(routes["chat"], routes["lightrag"], routes["embed"], await_push_routes())


def reload_key_files(table: Table) -> list[str]:
    """Re-read key files and the fallbacks file. Returns what was reloaded.

    Routes without a *_FILE keep their current key, including one pushed
    through the admin API. An unreadable file also keeps the current key, so
    a non-atomic replace does not drop requests mid-rotation. The fallbacks
    file works the same way: when it is set and parses, its lists replace
    the current ones (``chat.fallbacks`` in the result); otherwise they stay.
    """
    payload: dict = {}
    reloaded = []
    for name in ENV_PREFIXES:
        path = key_file(name)
        if path:
            key = read_key_file(path)
            if key is not None:
                payload[name] = {"api_key": key}
                reloaded.append(name)
    table.update(payload)
    path = fallbacks_file()
    parsed = read_fallbacks_file(path) if path else None
    if parsed is not None:
        table.set_fallbacks(parsed)
        reloaded.extend(f"{name}.fallbacks" for name in FALLBACK_ROUTES)
    return sorted(reloaded)


# Request fields that already limit the output. Any one of them, set, means
# the client chose its own cap.
OUTPUT_CAP_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
# Routes that serve generations. Embeddings have no output to cap.
GENERATION_ROUTES = ("chat", "lightrag")


def with_default_max_tokens(body: bytes, path: str, route: str, limit: int) -> bytes:
    """Add max_tokens to a generation request that sets no output limit.

    Covers /chat/completions and /completions (max_tokens) and /responses
    (max_output_tokens). Anything else, including a body that is not a JSON
    object, passes through unchanged.
    """
    if limit <= 0 or not body or route not in GENERATION_ROUTES:
        return body
    path = urlsplit(path).path.rstrip("/")
    if path.endswith("/completions"):
        field = "max_tokens"
    elif path.endswith("/responses"):
        field = "max_output_tokens"
    else:
        return body
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict):
        return body
    if any(payload.get(name) is not None for name in OUTPUT_CAP_FIELDS):
        return body
    payload[field] = limit
    return json.dumps(payload, separators=(",", ":")).encode()


def with_model(body: bytes, model: str) -> bytes:
    """Set the request's ``model`` for a fallback that names one.

    A body that is not a JSON object passes through unchanged.
    """
    if not model or not body:
        return body
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict):
        return body
    payload["model"] = model
    return json.dumps(payload, separators=(",", ":")).encode()


def env_number(name: str, default: float) -> float:
    """Read a number from the environment; empty means the default."""
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


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
    # Longest one request may take, start to finish. 0 means no limit.
    stream_max_seconds: float = 0
    # max_tokens added to generation requests that set no limit. 0 means none.
    default_max_tokens: int = 0
    # StreamRequestHandler's own attribute: the inbound socket timeout, which
    # closes idle keep-alive connections and stalled request bodies.
    timeout: float = 600

    # Upper bound on opening the TCP (and TLS) connection to an upstream that
    # has another after it, so a server that is down fails fast and the next
    # is tried. The last upstream keeps upstream_timeout, as with no fallbacks.
    connect_timeout: float = 10
    # "<route>;<index>" once an upstream answers, for the request log line.
    served: str = ""

    def log_message(self, fmt: str, *args) -> None:
        # Method and path only. Never the headers, which carry the key.
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def log_request(self, code="-", size="-") -> None:
        # Status and the upstream that served, still no headers or body.
        line = "%s %s %s" % (self.command, self.path.split("?", 1)[0], getattr(code, "value", code))
        if self.served:
            line += " upstream=" + self.served
        sys.stderr.write(line + "\n")

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
        self.served = ""
        path = urlsplit(self.path).path
        if path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        found = self.table.chain(self.path)
        if found is None:
            self._text(404, "not found\n")
            return
        name, chain = found
        if self.table.waiting(name):
            # The request body is not read, so do not reuse the connection.
            self.close_connection = True
            self._text(
                503,
                "llm-proxy: no upstream for route %s yet; the admin client pushes it "
                "(POST /admin/upstreams)\n" % name,
            )
            return
        # Index stays the position in the configured chain (0 = primary),
        # so an unset primary with a fallback still reports index 1.
        attempts = [(index, *entry) for index, entry in enumerate(chain) if entry[0]]
        if not attempts:
            self._text(502, "upstream is not configured\n")
            return
        body = self._read_body()
        if self.command == "POST":
            body = with_default_max_tokens(body, self.path, name, self.default_max_tokens)
        conn = None
        watcher = None
        started = False
        began = time.monotonic()
        try:
            for position, (index, target, api_key, model) in enumerate(attempts):
                last = position == len(attempts) - 1
                host = urlsplit(target).netloc
                conn = _connection(target, self.upstream_timeout if last else self.connect_timeout)
                try:
                    conn.connect()
                except OSError as exc:
                    conn.close()
                    conn = None
                    if last:
                        raise
                    sys.stderr.write(
                        "llm-proxy: %s upstream %d (%s) unreachable: %s; trying the next\n"
                        % (name, index, host, type(exc).__name__)
                    )
                    continue
                # Connected: from here the wait for headers covers prefill.
                conn.sock.settimeout(self.upstream_timeout)
                sent = with_model(body, model)
                conn.putrequest(self.command, _request_path(target), skip_host=True, skip_accept_encoding=True)
                for key, value in outbound_headers(list(self.headers.items()), api_key):
                    conn.putheader(key, value)
                conn.putheader("Host", host)
                if sent:
                    conn.putheader("Content-Length", str(len(sent)))
                conn.endheaders(sent if sent else None)
                # Keep our own reference: getresponse() drops conn.sock when the
                # upstream will close, but the response still reads from it.
                upstream_sock = conn.sock
                watcher = ClientWatcher(self.connection, upstream_sock)
                resp = conn.getresponse()
                if resp.status in RETRY_STATUSES and not last:
                    watcher.stop()
                    if watcher.client_gone:
                        raise ConnectionError("client closed")
                    watcher = None
                    conn.close()
                    conn = None
                    sys.stderr.write(
                        "llm-proxy: %s upstream %d (%s) answered %d; trying the next\n"
                        % (name, index, host, resp.status)
                    )
                    continue
                break
            if upstream_sock is not None:
                upstream_sock.settimeout(self.stream_idle_timeout)
            self.served = "%s;%d" % (name, index)
            self.send_response(resp.status)
            upstream_named = False
            for key, value in resp.headers.items():
                if key.lower() in HOP:
                    continue
                # An upstream that names its own source (another router or
                # proxy) knows more than the index does, so keep its value.
                if key.lower() == SERVED_HEADER.lower():
                    upstream_named = True
                self.send_header(key, value)
            if not upstream_named:
                self.send_header(SERVED_HEADER, self.served)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            started = True
            deadline = began + self.stream_max_seconds if self.stream_max_seconds > 0 else None
            while True:
                if deadline is not None:
                    # A model that never goes quiet never trips the idle
                    # timeout, so the total limit shortens each wait instead.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("stream exceeded LLM_STREAM_MAX_SECONDS")
                    if upstream_sock is not None:
                        upstream_sock.settimeout(min(self.stream_idle_timeout, remaining))
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
            elif started and self.stream_max_seconds > 0 and elapsed >= self.stream_max_seconds:
                sys.stderr.write(
                    "llm-proxy: upstream %s ran past LLM_STREAM_MAX_SECONDS (%.0fs); dropped\n"
                    % (path, self.stream_max_seconds)
                )
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
        try:
            self.table.update(payload)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
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
    connect_timeout = env_number("LLM_CONNECT_TIMEOUT", 10)
    idle_timeout = float(os.environ.get("LLM_STREAM_IDLE_TIMEOUT", "") or timeout)
    stream_max = env_number("LLM_STREAM_MAX_SECONDS", 3600)
    default_max_tokens = int(env_number("LLM_DEFAULT_MAX_TOKENS", 16384))
    infer_port = int(os.environ.get("LLM_PROXY_PORT", "4000"))
    admin_port = int(os.environ.get("LLM_PROXY_ADMIN_PORT", "4001"))
    token = os.environ.get("LLM_PROXY_ADMIN_TOKEN", "")

    inference = ThreadingHTTPServer(("0.0.0.0", infer_port), InferenceHandler)
    inference.daemon_threads = True
    InferenceHandler.table = table
    InferenceHandler.upstream_timeout = timeout
    InferenceHandler.connect_timeout = connect_timeout
    InferenceHandler.stream_idle_timeout = idle_timeout
    InferenceHandler.stream_max_seconds = stream_max
    InferenceHandler.default_max_tokens = default_max_tokens
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

    for name in FALLBACK_ROUTES:
        count = len(getattr(table, name).fallbacks)
        if count:
            sys.stderr.write("llm-proxy: %s has %d fallback upstream(s)\n" % (name, count))

    threading.Thread(target=admin.serve_forever, daemon=True).start()
    sys.stderr.write("llm-proxy inference :%s admin :%s\n" % (infer_port, admin_port))
    sys.stderr.write(
        "llm-proxy: default max_tokens %s, stream limit %s\n"
        % (default_max_tokens or "off", "%.0fs" % stream_max if stream_max > 0 else "off")
    )
    inference.serve_forever()


if __name__ == "__main__":
    main()
