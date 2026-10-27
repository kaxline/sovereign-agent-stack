#!/usr/bin/env python3
"""Smoke test for the Messages MCP container (needs Docker).

Builds the image, runs it locked down (read-only root, no capabilities, the
index mounted read-only) against a synthetic index, lists and calls tools
over MCP's HTTP transport, reads /watermark, and checks that an outbox event
is forwarded to a signed webhook on the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for sub in ("lib", "mcp"):
    if str(SRC / sub) not in sys.path:
        sys.path.insert(0, str(SRC / sub))

import messages_index as mi  # noqa: E402
import messages_query as mq  # noqa: E402
import messages_watch as mw  # noqa: E402
import synthetic_messages as sm  # noqa: E402

IMAGE = "assistant-messages-mcp:test"
SECRET = "container-secret-0123456789abcdef"
TOOLS = {"messages_status", "messages_chats", "messages_search", "messages_conversation", "messages_get",
         "messages_triage_batch"}


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


class Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ts = self.headers.get("X-Webhook-Timestamp", "")
        good = hmac.compare_digest(
            hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest(),
            self.headers.get("X-Webhook-Signature-V2", ""))
        Hook.received.append({"ok": good, "event": json.loads(body)})
        self.send_response(202 if good else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


def rpc(port: int, method: str, params: dict, rid: int) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode()
    if text.lstrip().startswith("{"):
        return json.loads(text)
    for line in text.splitlines():  # server-sent events framing
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    fail(f"no JSON-RPC answer to {method}: {text[:200]}")
    return {}


def main() -> int:
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode:
        print("SKIP Messages MCP container (needs Docker)")
        return 0
    built = subprocess.run(["docker", "build", "-q", "-f", str(SRC / "mcp" / "Dockerfile"), "-t", IMAGE, str(SRC)],
                           capture_output=True, text=True)
    expect(built.returncode == 0, f"image build: {built.stderr[-500:]}")

    with tempfile.TemporaryDirectory(prefix="msgmcp-container-") as tmp_name:
        tmp = Path(tmp_name)
        root = tmp / "synthetic-messages" / "store"
        sm.build(root)
        index_dir, events = tmp / "index", tmp / "events"
        cfg = tmp / "c.toml"
        cfg.write_text(f'store = "{root / "Messages"}"\nindex = "{index_dir / "index.sqlite"}"\n'
                       f'since = 2026-01-01\nchats = ["*"]\ncontacts = "{root / "AddressBook"}"\n')
        mi.run(mi.load_config(cfg))
        events.mkdir(mode=0o700)
        event = mw.test_event(mw.NotifyConfig(chats=["*"]))
        mw.write_outbox(events, event)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        name = f"msgmcp-test-{os.getpid()}"
        run = subprocess.run([
            "docker", "run", "-d", "--rm", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp",
            "-v", f"{index_dir}:/data/messages:ro", "-v", f"{events}:/data/messages-events",
            "-e", f"MESSAGES_WEBHOOK_URL=http://host.docker.internal:{server.server_port}/webhooks/messages",
            "-e", f"MESSAGES_WEBHOOK_SECRET={SECRET}", "-p", "127.0.0.1::8000", IMAGE],
            capture_output=True, text=True)
        expect(run.returncode == 0, f"container start: {run.stderr}")
        try:
            port = int(subprocess.run(["docker", "port", name, "8000"], capture_output=True, text=True)
                       .stdout.strip().rsplit(":", 1)[1])
            deadline = time.monotonic() + 30
            health = None
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                        health = json.loads(r.read())
                        break
                except OSError:
                    time.sleep(0.3)
            expect(health and health["index_present"], f"health: {health}")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/watermark", timeout=5) as r:
                mark = r.read().decode()
            expect(mark in ("none",) or mark.startswith("pending "), f"watermark: {mark}")

            init = rpc(port, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "smoke", "version": "1"}}, 1)
            expect("result" in init and "untrusted" in init["result"].get("instructions", ""),
                   "server instructions carry the untrusted notice")
            listed = rpc(port, "tools/list", {}, 2)
            names = {t["name"] for t in listed["result"]["tools"]}
            expect(names == TOOLS, f"tools: {sorted(names)}")
            found = rpc(port, "tools/call", {"name": "messages_search", "arguments": {"query": "ramen"}}, 3)
            payload = json.dumps(found["result"])
            expect(mq.TEXT_START in payload and "ramen" in payload, "a search through MCP returns wrapped text")

            deadline = time.monotonic() + 10
            while not Hook.received and time.monotonic() < deadline:
                time.sleep(0.2)
            expect(Hook.received and Hook.received[0]["ok"], f"forwarded with a valid signature: {Hook.received}")
            expect(Hook.received[0]["event"]["id"] == event["id"], "the forwarded event is the outbox event")
            time.sleep(0.5)
            expect(not list(events.glob("*.json")), "the delivered event file is removed")
            expect(not any(p.suffix == ".tmp" for p in index_dir.iterdir()), "nothing written beside the index")
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            server.shutdown()
    print("ok Messages MCP container")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
