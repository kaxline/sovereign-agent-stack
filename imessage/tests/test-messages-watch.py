#!/usr/bin/env python3
"""Checks for the Messages watcher (lib/messages_watch.py) against the synthetic store."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import messages_index as mi  # noqa: E402
import messages_watch as mw  # noqa: E402
import synthetic_messages as sm  # noqa: E402

SECRET = b"test-secret-0123456789abcdef"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def make_store(tmp: Path, name: str) -> Path:
    root = tmp / f"synthetic-messages-{name}" / "store"
    sm.build(root)
    return root


def write_config(path: Path, root: Path, *, notify: str = "", watch: str = "") -> Path:
    path.write_text(
        f'store = "{root / "Messages"}"\nindex = "{path.parent / (path.stem + "-index") / "index.sqlite"}"\n'
        f'since = 2026-01-01\nchats = ["*"]\ncontacts = "{root / "AddressBook"}"\n'
        + (f"[watch]\n{watch}\n" if watch else "") + (f"[notify]\n{notify}\n" if notify else ""))
    return path


def incoming(key: str, chat: str, sender: str, ts: float, text: str) -> sm.Spec:
    return sm.Spec(key, chat, sender, iso(ts), text, read=False)


def test_config(tmp: Path, root: Path) -> None:
    secret = tmp / "secret"
    secret.write_bytes(SECRET)
    bad = {
        "remote http": f'chats = ["*"]\nurl = "http://example.com/hook"\nsecret_file = "{secret}"',
        "url without secret": 'chats = ["*"]\nurl = "http://127.0.0.1:9/hook"',
        "missing secret file": f'chats = ["*"]\nurl = "http://127.0.0.1:9/hook"\nsecret_file = "{tmp}/nope"',
        "no sink": 'chats = ["*"]',
        "no chats": f'outbox = "{tmp}/out"',
        "bad quiet hours": f'chats = ["*"]\noutbox = "{tmp}/out"\nquiet_hours = "late"',
        "outbox in store": f'chats = ["*"]\noutbox = "{root / "Messages" / "events"}"',
        "unknown key": f'chats = ["*"]\noutbox = "{tmp}/out"\nsend = true',
    }
    for name, notify in bad.items():
        path = write_config(tmp / f"bad-{name.replace(' ', '-')}.toml", root, notify=notify)
        try:
            mw.load(path)
        except mi.ConfigError:
            continue
        fail(f"[notify] with {name} must be refused")
    ok = write_config(tmp / "ok.toml", root, notify=(
        f'chats = ["*;-;*"]\nurl = "https://hooks.example.net/m"\nsecret_file = "{secret}"\n'
        f'outbox = "{tmp}/out"\nquiet_hours = "22:00-07:00"'), watch="poll_seconds = 1")
    _, watch, notify = mw.load(ok)
    expect(notify.secret == SECRET and notify.quiet == (22 * 60, 7 * 60), "notify parsed")
    expect(watch.poll_seconds == 1.0, "watch parsed")
    expect(mi.load_config(ok).chats == ["*"], "the indexer still loads a config with [watch] and [notify]")


def test_sync(tmp: Path) -> None:
    root = make_store(tmp, "sync")
    outbox = tmp / "sync-out"
    cfg_path = write_config(tmp / "sync.toml", root,
                            notify=f'chats = ["iMessage;-;*", "iMessage;+;*"]\noutbox = "{outbox}"\nmax_age_minutes = 10')
    config, watch, notify = mw.load(cfg_path)
    clock = Clock()
    lines: list[str] = []
    w = mw.Watcher(config, watch, notify, clock=clock, log=lines.append)

    first = w.sync()
    expect(first.mode == "full" and first.added > 0 and first.events == 0, f"baseline indexes, no events: {first}")
    expect(not outbox.exists() or not list(outbox.iterdir()), "no events for history")
    state = w.state_path
    expect(stat.S_IMODE(state.stat().st_mode) == 0o600, "state file is owner-only")

    clock.now += 5
    sm.append(root, incoming("w-alex", "alex", "alex", clock.now - 2, "Are you around?"))
    r = w.sync()
    expect(r.mode == "incremental" and r.added == 1 and r.events == 1 and r.delivered == 1, f"one event: {r}")
    files = sorted(outbox.glob("*.json"))
    expect(len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600, "event file written, owner-only")
    event = json.loads(files[0].read_text())
    expect(event["type"] == "messages.new" and event["count"] == 1, f"event shape: {event}")
    expect(event["chat"] == {"id": sm.CHATS["alex"].guid, "name": "Alex Chen", "is_group": False}, "chat block")
    expect(event["senders"] == ["Alex Chen"] and len(event["message_ids"]) == 1, "senders and ids")
    expect("Are you around" not in files[0].read_text(), "events never carry message text")

    # Two messages in one chat make one event; my own messages and SMS (not in notify.chats) make none.
    clock.now += 5
    sm.append(root, incoming("w-g1", "garden", "jordan", clock.now - 3, "Seeds are in"))
    sm.append(root, incoming("w-g2", "garden", "maria", clock.now - 2, "Great"))
    sm.append(root, sm.Spec("w-me", "alex", None, iso(clock.now - 1), "On my way"))
    sm.append(root, incoming("w-sms", "courier", "courier", clock.now - 1, "Delivered"))
    r = w.sync()
    expect(r.added == 4 and r.events == 1, f"group event only: {r}")
    event = json.loads(sorted(outbox.glob("*.json"))[-1].read_text())
    expect(event["chat"]["name"] == "Garden club" and event["count"] == 2, f"group event: {event}")
    expect(event["senders"] == ["Jordan Lee", "María Gómez"], "both senders")

    # Old-dated rows (iCloud backfill) don't notify.
    clock.now += 5
    sm.append(root, incoming("w-old", "alex", "alex", clock.now - 3600, "From an hour ago"))
    r = w.sync()
    expect(r.added == 1 and r.events == 0, f"old messages index but don't notify: {r}")

    # A row whose chat join isn't written yet waits, then notifies once.
    clock.now += 5
    pending = sm.append(root, incoming("w-late", "priya", "priya", clock.now - 1, "Hello?"), join=False)
    r = w.sync()
    expect(r.added == 0 and r.events == 0, f"no chat yet, no event: {r}")
    sm.add_join(root, pending["rowid"], "priya")
    clock.now += 1
    r = w.sync()
    expect(r.added == 1 and r.events == 1, f"late join notifies: {r}")

    # An edit updates the index without a new event.
    db = sqlite3.connect(root / "Messages" / "chat.db")
    db.execute("UPDATE message SET text = 'Hello!', date_edited = date + 1 WHERE ROWID = ?", (pending["rowid"],))
    db.commit()
    db.close()
    clock.now += 1
    r = w.sync()
    expect(r.updated == 1 and r.events == 0, f"edit updates, no event: {r}")

    # A restart keeps the cursor: no duplicate events.
    w2 = mw.Watcher(config, watch, notify, clock=clock, log=lines.append)
    expect(w2.state["cursor"] == w.state["cursor"], "cursor persisted")
    r = w2.sync(force_full=True)
    expect(r.events == 0 and r.added == 0, f"restart re-indexes nothing and raises nothing: {r}")
    expect(all("Hello" not in line and "Seeds" not in line for line in lines), "logs carry counts only")


def test_quiet_and_retry(tmp: Path) -> None:
    root = make_store(tmp, "quiet")
    secret = tmp / "quiet-secret"
    secret.write_bytes(SECRET)
    minute = datetime.now().hour * 60 + datetime.now().minute
    start, end = (minute - 5) % 1440, (minute + 5) % 1440
    quiet = f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
    cfg_path = write_config(tmp / "quiet.toml", root, notify=(
        f'chats = ["*"]\nurl = "http://127.0.0.1:9/hook"\nsecret_file = "{secret}"\nquiet_hours = "{quiet}"'))
    config, watch, notify = mw.load(cfg_path)
    clock = Clock()
    sent: list[dict] = []
    up = {"ok": False}

    def post(url, key, event):
        if up["ok"]:
            sent.append(event)
        return up["ok"]

    w = mw.Watcher(config, watch, notify, clock=clock, log=lambda _: None, post=post)
    w.sync()
    clock.now += 5
    sm.append(root, incoming("q1", "alex", "alex", clock.now - 2, "one"))
    w.sync()
    clock.now += 5
    sm.append(root, incoming("q2", "alex", "alex", clock.now - 2, "two"))
    r = w.sync()
    expect(r.held == 1 and r.delivered == 0 and not sent, f"quiet hours hold one merged event: {r}")

    notify.quiet = None  # quiet hours over; the endpoint is down
    clock.now += 5
    r = w.sync()
    expect(r.delivered == 0 and r.pending == 1 and r.held == 0, f"failed delivery is kept: {r}")
    up["ok"] = True
    clock.now += 5
    r = w.sync()
    expect(r.delivered == 1 and r.pending == 0, f"retry delivers: {r}")
    expect(sent[0]["count"] == 2 and len(sent[0]["message_ids"]) == 2, f"held events merged: {sent[0]}")

    up["ok"] = False
    clock.now += 5
    sm.append(root, incoming("q3", "alex", "alex", clock.now - 2, "three"))
    w.sync()
    clock.now += mw.PENDING_TTL + 1
    r = w.sync()
    expect(r.dropped == 1 and r.pending == 0, f"stale deliveries are dropped: {r}")


class Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ts = self.headers["X-Webhook-Timestamp"]
        expected = hmac.new(SECRET, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, self.headers["X-Webhook-Signature-V2"])
        Hook.received.append({"ok": ok, "id": self.headers["X-Request-ID"], "event": json.loads(body),
                              "age": abs(time.time() - int(ts))})
        self.send_response(202 if ok else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_webhook(tmp: Path) -> None:
    server = HTTPServer(("127.0.0.1", 0), Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/webhooks/messages"
        event = mw.test_event(mw.NotifyConfig(chats=["*"]))
        expect(mw.post_event(url, SECRET, event), "signed POST accepted")
        got = Hook.received[-1]
        expect(got["ok"] and got["id"] == event["id"] and got["event"] == event and got["age"] < 5,
               f"signature, request id and body: {got}")
        expect(not mw.post_event(url, b"wrong-secret-0123456789", event), "wrong secret rejected")
        expect(not mw.post_event("http://127.0.0.1:9/nothing", SECRET, event), "unreachable endpoint is a failure")
    finally:
        server.shutdown()


def test_live(tmp: Path) -> None:
    """The real loop: kqueue (on macOS), debounce, WAL deleted and recreated."""
    root = make_store(tmp, "live")
    outbox = tmp / "live-out"
    cfg_path = write_config(tmp / "live.toml", root, notify=f'chats = ["*"]\noutbox = "{outbox}"',
                            watch="poll_seconds = 30\ndebounce_ms = 100")
    config, watch, notify = mw.load(cfg_path)
    if not hasattr(__import__("select"), "kqueue"):
        watch.poll_seconds = 1.0  # no kqueue: rely on the poll
    w = mw.Watcher(config, watch, notify, log=lambda _: None)
    stop = threading.Event()
    thread = threading.Thread(target=w.run, args=(stop,), daemon=True)
    thread.start()

    def wait_events(n: int, limit: float = 5.0) -> float:
        start = time.monotonic()
        while time.monotonic() - start < limit:
            if len(list(outbox.glob("*.json"))) >= n:
                return time.monotonic() - start
            time.sleep(0.05)
        fail(f"expected {n} event files within {limit}s, have {len(list(outbox.glob('*.json')))}")
        return limit

    try:
        deadline = time.monotonic() + 10
        while w.state.get("cursor") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        expect(w.state.get("cursor") is not None, "watcher finished its first full pass")
        time.sleep(0.2)

        # Messages keeps chat.db open in WAL mode while it runs.
        writer = sqlite3.connect(root / "Messages" / "chat.db")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        spec = incoming("live-1", "alex", "alex", time.time() - 1, "ping")
        sm._insert_message(writer, root, spec, None, {s.key: s.guid for s in sm.SPECS} | {spec.key: spec.guid})
        writer.commit()
        latency = wait_events(1)
        expect(latency < 3.0, f"new message noticed in {latency:.2f}s")

        # Messages quits: checkpoint and close remove the WAL. Then it starts again.
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.close()
        time.sleep(0.5)
        writer = sqlite3.connect(root / "Messages" / "chat.db")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        spec = incoming("live-2", "priya", "priya", time.time() - 1, "pong")
        sm._insert_message(writer, root, spec, None, {s.key: s.guid for s in sm.SPECS} | {spec.key: spec.guid})
        writer.commit()
        latency = wait_events(2)
        expect(latency < 3.0, f"noticed after the WAL was recreated, in {latency:.2f}s")
        writer.close()
    finally:
        stop.set()
        thread.join(timeout=5)
    expect(not thread.is_alive(), "watcher stops when asked")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="msgwatch-test-") as tmp_name:
        tmp = Path(tmp_name)
        test_config(tmp, make_store(tmp, "config"))
        test_sync(tmp)
        test_quiet_and_retry(tmp)
        test_webhook(tmp)
        test_live(tmp)
    print("ok messages watcher")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
