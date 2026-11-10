#!/usr/bin/env python3
"""Checks for the broker's access requests, release and their persistence.

Stdlib only. Reuses the helpers in scripts/test-bitwarden-broker.py: the
synthetic vault, the fake bw and ``Broker``, which runs ``python3 -m
broker.server`` as a subprocess. Every test searches the responses, the
broker's output and the files it writes for the synthetic secrets.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bitwarden_broker_tests", ROOT / "scripts" / "test-bitwarden-broker.py")
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

from broker import requests_store  # noqa: E402

expect, fail, handle, item_id = bt.expect, bt.fail, bt.handle, bt.item_id
DAY = 86400


def expect_request_error(fn, code: str, msg: str) -> None:
    try:
        fn()
    except requests_store.RequestError as exc:
        expect(exc.code == code and str(exc) == code, f"{msg}: got {exc.code}, want {code}")
        return
    fail(f"{msg}: no RequestError")


def edit_vault(b, name: str, **fields) -> None:
    data = json.loads(b.run.vault_file.read_text(encoding="utf-8"))
    next(i for i in data["items"] if i["id"] == item_id(name)).update(fields)
    b.run.vault_file.write_text(json.dumps(data), encoding="utf-8")


def vault_item(b, name: str) -> dict:
    data = json.loads(b.run.vault_file.read_text(encoding="utf-8"))
    return next(i for i in data["items"] if i["id"] == item_id(name))


def denylist(b) -> list:
    path = b.run.tmp / "broker" / "denylist.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def test_request_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broker" / "requests.json"
        store = requests_store.Store(path, accounts=["personal"])
        for site in ("not a host!", "", "a..example.com", "-a.example.com", "x" * 64 + ".example.com",
                     "user@example.com", "example.com/path", None, 7, "com", "localhost",
                     "https://intranet/login", "com."):
            expect_request_error(lambda: store.add("personal", site, "r"), "invalid_site", f"site {site!r}")
        expect_request_error(lambda: store.add("nobody", "example.com", "r"), "unknown_account", "account")
        reason = "\x1b[31m" + "é​\n\t" * 200
        rid = store.add("personal", "HTTPS://Login.Example.com:8443/x", reason)
        got = store.pending()
        expect(len(got) == 1 and got[0]["request_id"] == rid and got[0]["site"] == "login.example.com",
               f"stored request {got}")
        stored = got[0]["reason"]
        expect(len(stored) == 200, f"reason length {len(stored)}")
        expect(not any(unicodedata.category(c).startswith("C") for c in stored), "control characters kept")
        expect(stored.startswith("[31m"), f"reason {stored[:10]!r}")
        expect(stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
        expect([p.name for p in path.parent.iterdir()] == ["requests.json"], "temp files left")

    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        expect(b.call("POST", "/requests", {"account": "personal", "site": "not a host!", "reason": "x"})
               == (400, {"error": "invalid_site"}), "API invalid_site")
        expect(b.call("POST", "/requests", {"account": "personal", "site": "com", "reason": "x"})
               == (400, {"error": "invalid_site"}), "API invalid_site for a single label")
        expect(b.call("POST", "/requests", {"account": "nobody", "site": "example.com", "reason": "x"})
               == (400, {"error": "unknown_account"}), "API unknown_account")
        status, got = b.call("POST", "/requests", {"site": "news.example.com", "reason": "\x1b[31m" + "y" * 500})
        expect(status == 200, f"one account: account may be left out ({status} {got})")
        pending = b.call("GET", "/requests")[1]["pending"]
        expect(pending[0]["account"] == "personal" and pending[0]["reason"] == "[31m" + "y" * 196,
               f"API stored request {pending}")
        b.stop()
        b.expect_clean()


def test_request_cap_and_expiry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = requests_store.Store(Path(tmp) / "requests.json", accounts=["personal"])
        now = time.time()
        old = store.add("personal", "old.example.com", "r", now=now - 8 * DAY)
        for n in range(9):
            store.add("personal", f"s{n}.example.com", "r")
        expect(len(store.pending()) == 10, "ten pending")
        expect_request_error(lambda: store.add("personal", "s10.example.com", "r"), "too_many", "eleventh")
        expect(store.add("personal", "s3.example.com", "again") == store.pending()[4]["request_id"],
               "a repeat of a pending request returns its id")
        store.expire(now)
        expect(old not in [r["request_id"] for r in store.pending()], "an 8-day-old request survived expire")
        expect(len(store.pending()) == 9, "expire dropped only the old request")
        store.add("personal", "s10.example.com", "r")
        store.mark_done("www.s0.example.com")
        expect(len(store.pending()) == 9 and store.recent()[0]["site"] == "s0.example.com", "mark_done by host")
        expect(store.recent()[0]["status"] == "done", "done status")

    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        for n in range(10):
            expect(b.call("POST", "/requests", {"site": f"s{n}.example.com", "reason": "r"})[0] == 200, f"request {n}")
        expect(b.call("POST", "/requests", {"site": "s10.example.com", "reason": "r"}) == (429, {"error": "too_many"}),
               "API too_many")
        b.stop()
        b.expect_clean()


def test_request_reveals_nothing() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        bodies = []
        # B (other.example.com) is in the vault, outside the agent's folder; nothing.example.com is nowhere.
        for site in ("other.example.com", "nothing.example.com"):
            status, got = b.call("POST", "/requests", {"account": "personal", "site": site, "reason": "r"})
            expect(status == 200, f"{site}: {status}")
            bodies.append(b.bodies[-1].replace(got["request_id"], "ID"))
        expect(bodies[0] == bodies[1], f"responses differ: {bodies}")
        pending = b.call("GET", "/requests")[1]["pending"]
        expect(sorted(pending[0]) == sorted(pending[1]) and all("item" not in k for k in pending[0]),
               f"request metadata {pending}")
        b.stop()
        b.expect_clean()


def bw_moves(b) -> list:
    """bw calls that read or change an item's folder or collections."""
    return [e["argv"] for e in b.run.log_lines()
            if e["argv"][:1] == ["edit"] or e["argv"][:2] == ["get", "item"]]


def test_release_denylists_and_stops() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(a in b.handles(), "A listed before release")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True}), "release reply")
        expect(a in denylist(b), "A is not on the denylist")
        expect(a not in b.handles(), "/items lists A after release")
        unknown = b.call("POST", "/fill", bt.fill_body("bwbroker:personal/ffffffff-0000-4000-8000-000000000000"))
        expect(b.call("POST", "/fill", bt.fill_body(a)) == unknown == (404, {"error": "unknown_handle"}),
               "/fill refuses A, as for an unknown handle")
        expect(bw_moves(b) == [], f"release called bw to move the item: {bw_moves(b)}")
        expect(vault_item(b, "A")["folderId"] == bt.FOLDER_AGENT, "release changed the item in the vault")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True}), "release of a denylisted handle")
        expect(b.call("POST", "/release", {"handle": handle("personal", "B")}) == (404, {"error": "unknown_handle"}),
               "release of an item outside scope")
        expect(b.call("POST", "/release", {"handle": "nope"}) == (404, {"error": "unknown_handle"}), "bad handle")
        b.stop()
        b.expect_clean()


def test_release_leaves_collection_and_scope_all_items() -> None:
    with bt.broker_run() as b:
        b.add_account("org", scope=f"collection:{bt.COLLECTION}")
        b.start(unlocked=["org"])
        c = handle("org", "C")
        expect(c in b.handles(), "C listed in the collection scope")
        expect(b.call("POST", "/release", {"handle": c}) == (200, {"released": True}), "collection")
        expect(c in denylist(b) and c not in b.handles(), "C served or not denylisted after release")
        expect(vault_item(b, "C")["collectionIds"] == [bt.COLLECTION], "release changed the item's collections")
        expect(bw_moves(b) == [], f"release called bw to move the item: {bw_moves(b)}")
        b.stop()
        b.expect_clean()

    with bt.broker_run() as b:
        b.add_account("all", scope="all")
        b.start(unlocked=["all"])
        a = handle("all", "A")
        expect(a in b.handles(), "A listed in scope all")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True}), "scope all")
        expect(a in denylist(b) and a not in b.handles(), "A served or not denylisted after release")
        expect(bw_moves(b) == [], f"release called bw to move the item: {bw_moves(b)}")
        b.stop()
        b.expect_clean()


def test_sync_drops_denylist_entry_after_user_moves_item() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/release", {"handle": a})
        b.call("POST", "/sync", {})
        expect(a in denylist(b) and a not in b.handles(), "a sync dropped the entry while A is still in scope")
        edit_vault(b, "A", folderId=None)  # the user moves it out in Bitwarden
        b.call("POST", "/sync", {})
        expect(a not in denylist(b) and a not in b.handles(), "the entry stays after A left scope")
        edit_vault(b, "A", folderId=bt.FOLDER_AGENT)  # and back in
        expect(b.call("POST", "/sync", {}) == (200, {"synced": ["personal"]}), "sync")
        expect(a in b.handles(), "A is not listed after the user moved it back")
        b.stop()
        b.expect_clean()


def test_requests_reply_has_no_unreturned() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        b.call("POST", "/release", {"handle": handle("personal", "A")})
        status, got = b.call("GET", "/requests")
        expect(status == 200 and sorted(got) == ["pending", "recent"], f"GET /requests keys {sorted(got)}")
        b.stop()
        b.expect_clean()


def test_leftover_unreturned_file_is_ignored() -> None:
    """A broker from before this change may have left unreturned.json; it is never read."""
    with bt.broker_run() as b:
        b.add_account("personal")
        path = b.run.tmp / "broker" / "unreturned.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ('[{"handle": "bwbroker:personal/x", "account": "personal", "label": "L", "site": "s"}]', "{"):
            path.write_text(content, encoding="utf-8")
            b.start(unlocked=["personal"])
            status, got = b.call("GET", "/requests")
            expect(status == 200 and sorted(got) == ["pending", "recent"], f"GET /requests with {content!r}: {got}")
            expect(b.call("POST", "/release", {"handle": handle("personal", "A")}) == (200, {"released": True}),
                   "release")
            b.call("POST", "/sync", {})
            b.stop()
            expect(path.read_text(encoding="utf-8") == content, "the broker rewrote unreturned.json")
        b.expect_clean()


def test_sync_marks_requests_done() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        b.call("POST", "/requests", {"site": "other.example.com", "reason": "r"})
        b.call("POST", "/requests", {"site": "elsewhere.example.com", "reason": "r"})
        edit_vault(b, "B", folderId=bt.FOLDER_AGENT)
        b.call("POST", "/sync", {})
        got = b.call("GET", "/requests")[1]
        expect([r["site"] for r in got["pending"]] == ["elsewhere.example.com"], f"pending {got}")
        expect([(r["site"], r["status"]) for r in got["recent"]] == [("other.example.com", "done")], f"recent {got}")
        b.stop()
        b.expect_clean()


def test_restart_keeps_requests_and_denylist() -> None:
    """Review Focus 4: the broker restarts while hermes stays up."""
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/requests", {"site": "one.example.com", "reason": "first"})
        b.call("POST", "/requests", {"site": "two.example.com", "reason": "second"})
        expect(b.call("POST", "/release", {"handle": a})[1] == {"released": True}, "release")
        before = b.call("GET", "/requests")[1]
        b.stop()

        b.start(unlocked=["personal"])
        expect(b.call("GET", "/requests")[1] == before, "requests changed across a restart")
        expect(a in denylist(b) and a not in b.handles(), "the denylist did not survive a restart")
        expect(handle("personal", "E") in b.handles(), "the stored account serves items again")
        b.stop()

        # A corrupt store fails closed: the broker refuses to start rather than forget entries.
        for name in ("requests.json", "denylist.json"):
            path = b.run.tmp / "broker" / name
            saved = path.read_bytes()
            path.write_text("{", encoding="utf-8")
            b.start(expect_exit=True)
            path.write_bytes(saved)
        b.expect_clean()


def main() -> None:
    test_request_validation()
    test_request_cap_and_expiry()
    test_request_reveals_nothing()
    test_release_denylists_and_stops()
    test_release_leaves_collection_and_scope_all_items()
    test_sync_drops_denylist_entry_after_user_moves_item()
    test_requests_reply_has_no_unreturned()
    test_leftover_unreturned_file_is_ignored()
    test_sync_marks_requests_done()
    test_restart_keeps_requests_and_denylist()
    print("ok")


if __name__ == "__main__":
    sys.exit(main())
