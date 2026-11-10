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
import threading
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


def test_release_before_move() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gate = Path(tmp) / "gate"
        with bt.broker_run(vault=bt.vault_data(edit_gate=str(gate))) as b:
            b.add_account("personal")
            b.start(unlocked=["personal"])
            a = handle("personal", "A")
            expect(a in b.handles(), "A listed before release")
            result = {}
            thread = threading.Thread(target=lambda: result.update(got=b.call("POST", "/release", {"handle": a})))
            thread.start()
            b.wait_for(lambda: any(e["argv"][:2] == ["edit", "item"] for e in b.run.log_lines()), "edit started")
            expect(a not in b.handles(), "/items lists A while the move runs")
            unknown = b.call("POST", "/fill", bt.fill_body("bwbroker:personal/ffffffff-0000-4000-8000-000000000000"))
            expect(b.call("POST", "/fill", bt.fill_body(a)) == unknown == (404, {"error": "unknown_handle"}),
                   "/fill refuses A while the move runs, as for an unknown handle")
            expect(a in denylist(b), "A is on the denylist before the move ends")
            gate.write_text("go", encoding="utf-8")
            thread.join(30)
            expect(result.get("got") == (200, {"released": True, "returned": True}), f"release {result}")
            b.stop()
            b.expect_clean()


def test_release_paths() -> None:
    # folder: scope: folderId goes to null.
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True, "returned": True}), "folder")
        expect(vault_item(b, "A")["folderId"] is None, "folderId not null")
        expect(vault_item(b, "A")["login"]["password"] == bt.ITEM_PASSWORDS["A"], "edit kept the rest of the item")
        expect(a not in b.handles(), "A listed after release")
        expect(b.call("POST", "/release", {"handle": handle("personal", "B")}) == (404, {"error": "unknown_handle"}),
               "release of an item outside scope")
        expect(b.call("POST", "/release", {"handle": "nope"}) == (404, {"error": "unknown_handle"}), "bad handle")
        edits = [e["argv"] for e in b.run.log_lines() if e["argv"][:1] == ["edit"]]
        expect(edits == [["edit", "item", item_id("A")]], f"the item JSON goes over stdin, not argv: {edits}")
        b.stop()
        b.expect_clean()

    # collection: scope: collectionIds goes to [].
    with bt.broker_run() as b:
        b.add_account("org", scope=f"collection:{bt.COLLECTION}")
        b.start(unlocked=["org"])
        c = handle("org", "C")
        expect(c in b.handles(), "C listed in the collection scope")
        expect(b.call("POST", "/release", {"handle": c}) == (200, {"released": True, "returned": True}), "collection")
        expect(vault_item(b, "C")["collectionIds"] == [], f"collectionIds {vault_item(b, 'C')['collectionIds']}")
        edits = [e["argv"] for e in b.run.log_lines() if e["argv"][:1] == ["edit"]]
        expect(edits == [["edit", "item-collections", item_id("C"), "--organizationid", bt.ORG]], f"edit argv {edits}")
        expect(c not in b.handles(), "C listed after release")
        b.stop()
        b.expect_clean()

    # R10: an item in two collections keeps the one outside the scope.
    other = "0c0d0000-0000-4000-8000-00000000000d"
    data = bt.vault_data()
    next(i for i in data["items"] if i["id"] == item_id("C"))["collectionIds"] = [bt.COLLECTION, other]
    with bt.broker_run(vault=data) as b:
        b.add_account("org", scope=f"collection:{bt.COLLECTION}")
        b.start(unlocked=["org"])
        c = handle("org", "C")
        expect(b.call("POST", "/release", {"handle": c}) == (200, {"released": True, "returned": True}), "two")
        expect(vault_item(b, "C")["collectionIds"] == [other], f"collectionIds {vault_item(b, 'C')['collectionIds']}")
        expect(c not in b.handles() and c not in denylist(b), "C served or still denylisted after release")
        b.stop()
        b.expect_clean()

    # The server refuses: not returned, still denylisted.
    with bt.broker_run(vault=bt.vault_data(refuse_edit="No rights to modify the collection.")) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True, "returned": False}), "refused")
        expect(vault_item(b, "A")["folderId"] == bt.FOLDER_AGENT, "a refused edit changed the vault")
        expect(a in denylist(b) and a not in b.handles(), "a refused return left A served")
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True, "returned": False}),
               "a release can be retried for a denylisted handle")
        b.stop()
        b.expect_clean()


def test_regrant_clears_denylist() -> None:
    with bt.broker_run() as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is True, "release")
        expect(a not in denylist(b), "the entry stays after the item left scope")
        edit_vault(b, "A", folderId=bt.FOLDER_AGENT)
        expect(b.call("POST", "/sync", {}) == (200, {"synced": ["personal"]}), "sync")
        expect(a in b.handles(), "a re-granted item is not listed after sync")
        b.stop()
        b.expect_clean()

    # The return failed, so the item never left scope: it stays denylisted across syncs
    # until it leaves, and a later grant lists it again.
    with bt.broker_run(vault=bt.vault_data(refuse_edit="No rights to modify the collection.")) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is False, "refused release")
        b.call("POST", "/sync", {})
        expect(a in denylist(b) and a not in b.handles(), "a sync dropped the entry while A is still in scope")
        edit_vault(b, "A", folderId=None)
        b.call("POST", "/sync", {})
        expect(a not in denylist(b), "the entry stays after A left scope")
        edit_vault(b, "A", folderId=bt.FOLDER_AGENT)
        b.call("POST", "/sync", {})
        expect(a in b.handles(), "A is not listed after it was granted again")
        b.stop()
        b.expect_clean()


def unreturned(b) -> list:
    return b.call("GET", "/requests")[1]["unreturned"]


def unreturned_file(b) -> list:
    path = b.run.tmp / "broker" / "unreturned.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


REFUSE = "No rights to modify the collection."


def test_unreturned_listed_after_refused_move() -> None:
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        expect(unreturned(b) == [], f"unreturned before any release: {unreturned(b)}")
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is False, "refused release")
        want = [{"account": "personal", "label": "Item A", "site": "login.example.com"}]
        expect(unreturned(b) == want, f"unreturned {unreturned(b)}")
        path = b.run.tmp / "broker" / "unreturned.json"
        expect(stat.S_IMODE(path.stat().st_mode) == 0o600, "unreturned.json is not mode 600")
        expect(unreturned_file(b) == [{"handle": a, **want[0]}], f"file {unreturned_file(b)}")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is False, "retry")
        expect(unreturned(b) == want, f"a retry changed the entry: {unreturned(b)}")
        b.stop()
        b.expect_clean()


def test_unreturned_cleared_by_sync() -> None:
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/release", {"handle": a})
        expect(len(unreturned(b)) == 1, "listed")
        edit_vault(b, "A", folderId=None)
        b.call("POST", "/sync", {})
        expect(unreturned(b) == [] and a not in denylist(b), "a sync kept the entry after A left scope")
        expect(unreturned_file(b) == [], f"file {unreturned_file(b)}")
        b.stop()
        b.expect_clean()


def test_unreturned_hidden_for_removed_account() -> None:
    """A disconnected account's records are not listed; the file keeps them."""
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal")
        b.add_account("work")
        b.start(unlocked=["personal", "work"])
        a = handle("personal", "A")
        b.call("POST", "/release", {"handle": a})
        expect([r["account"] for r in unreturned(b)] == ["personal"], f"listed {unreturned(b)}")
        b.stop()
        (b.accounts_dir / "personal.env").unlink()
        b.start(unlocked=["work"])
        expect(unreturned(b) == [], f"listed a removed account's record: {unreturned(b)}")
        expect([r["handle"] for r in unreturned_file(b)] == [a], f"file {unreturned_file(b)}")
        b.stop()
        b.expect_clean()


def test_orphan_unreturned_dropped_by_prune() -> None:
    """A record whose handle is not on the denylist (a prune that overtook a release, or a
    crash between the two removals) is dropped at the next prune of its account."""
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/release", {"handle": a})
        b.stop()
        path = b.run.tmp / "broker" / "unreturned.json"
        orphan = {"handle": handle("personal", "B"), "account": "personal", "label": "Item B",
                  "site": "b.example.com"}
        path.write_text(json.dumps(unreturned_file(b) + [orphan]), encoding="utf-8")
        path.chmod(0o600)
        expect(orphan["handle"] not in denylist(b), "the orphan is on the denylist")
        b.start(unlocked=["personal"])
        b.call("POST", "/sync", {})
        expect([r["handle"] for r in unreturned_file(b)] == [a],
               f"a prune kept the orphan or dropped the denylisted record: {unreturned_file(b)}")
        expect([r["label"] for r in unreturned(b)] == ["Item A"], f"listed {unreturned(b)}")
        b.stop()
        b.expect_clean()


def test_unreturned_survives_lock_and_restart() -> None:
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal", unlock="manual")
        b.start()
        expect(b.sock_call("POST", "/unlock", {"slug": "personal", "password": bt.MASTER})[0] == 200, "unlock")
        b.call("POST", "/release", {"handle": handle("personal", "A")})
        want = [{"account": "personal", "label": "Item A", "site": "login.example.com"}]
        expect(unreturned(b) == want, f"listed {unreturned(b)}")
        expect(b.sock_call("POST", "/lock", {"slug": "personal"})[0] == 200, "lock")
        expect(unreturned(b) == want, f"after lock {unreturned(b)}")
        b.stop()
        b.start()
        expect(unreturned(b) == want, f"after restart {unreturned(b)}")
        b.stop()
        path = b.run.tmp / "broker" / "unreturned.json"
        saved = path.read_bytes()
        path.write_text("{", encoding="utf-8")
        b.start(expect_exit=True)
        path.write_bytes(saved)
        b.expect_clean()


def test_unreturned_skips_returned_and_scope_all() -> None:
    # The edit works, the refresh after it fails: returned, so not listed.
    with tempfile.TemporaryDirectory() as tmp:
        gate = Path(tmp) / "gate"
        with bt.broker_run(vault=bt.vault_data(edit_gate=str(gate))) as b:
            b.add_account("org", scope=f"collection:{bt.COLLECTION}")
            b.start(unlocked=["org"])
            c = handle("org", "C")
            result = {}
            thread = threading.Thread(target=lambda: result.update(got=b.call("POST", "/release", {"handle": c})))
            thread.start()
            b.wait_for(lambda: any(e["argv"][:2] == ["edit", "item-collections"] for e in b.run.log_lines()),
                       "edit started")
            data = json.loads(b.run.vault_file.read_text(encoding="utf-8"))
            data["truncate_lists"] = True
            b.run.vault_file.write_text(json.dumps(data), encoding="utf-8")
            gate.write_text("go", encoding="utf-8")
            thread.join(30)
            expect(result.get("got") == (200, {"released": True, "returned": True}), f"release {result}")
            expect(vault_item(b, "C")["collectionIds"] == [], "the edit did not run")
            expect(unreturned(b) == [] and unreturned_file(b) == [], f"listed a returned item: {unreturned(b)}")
            b.stop()

    with bt.broker_run() as b:
        b.add_account("all", scope="all")
        b.start(unlocked=["all"])
        a = handle("all", "A")
        expect(a in b.handles(), "A listed in scope all")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is False, "scope all release")
        expect(unreturned(b) == [] and unreturned_file(b) == [], f"listed a scope all item: {unreturned(b)}")
        b.stop()
        b.expect_clean()


def test_regrant_clears_unreturned() -> None:
    with bt.broker_run(vault=bt.vault_data(refuse_edit=REFUSE)) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/release", {"handle": a})
        edit_vault(b, "A", folderId=None)
        b.call("POST", "/sync", {})
        edit_vault(b, "A", folderId=bt.FOLDER_AGENT)
        b.call("POST", "/sync", {})
        expect(a in b.handles(), "A is not listed after it was granted again")
        expect(unreturned(b) == [] and unreturned_file(b) == [], f"regrant kept the record: {unreturned(b)}")
        b.stop()
        b.expect_clean()


def test_retry_after_user_moved_item() -> None:
    """A failed release, then the user moves the item out of scope: the retry edits nothing."""
    refuse = "No rights to modify the collection."
    with bt.broker_run(vault=bt.vault_data(refuse_edit=refuse)) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        expect(b.call("POST", "/release", {"handle": a})[1]["returned"] is False, "refused release")
        edit_vault(b, "A", folderId=bt.FOLDER_OTHER)
        edits = sum(e["argv"][:1] == ["edit"] for e in b.run.log_lines())
        expect(b.call("POST", "/release", {"handle": a}) == (200, {"released": True, "returned": True}), "retry")
        expect(sum(e["argv"][:1] == ["edit"] for e in b.run.log_lines()) == edits, "the retry ran bw edit")
        expect(vault_item(b, "A")["folderId"] == bt.FOLDER_OTHER, "the retry moved the item again")
        expect(a not in denylist(b) and a not in b.handles(), "the entry stays after the item left scope")
        b.stop()
        b.expect_clean()

    with bt.broker_run(vault=bt.vault_data(refuse_edit=refuse)) as b:
        b.add_account("org", scope=f"collection:{bt.COLLECTION}")
        b.start(unlocked=["org"])
        c = handle("org", "C")
        expect(b.call("POST", "/release", {"handle": c})[1]["returned"] is False, "refused collection release")
        edit_vault(b, "C", collectionIds=[])
        edits = sum(e["argv"][:1] == ["edit"] for e in b.run.log_lines())
        expect(b.call("POST", "/release", {"handle": c})[1]["returned"] is True, "collection retry")
        expect(sum(e["argv"][:1] == ["edit"] for e in b.run.log_lines()) == edits, "the retry ran bw edit")
        expect(c not in denylist(b), "the collection entry stays after the item left scope")
        b.stop()
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
    with bt.broker_run(vault=bt.vault_data(refuse_edit="No rights to modify the collection.")) as b:
        b.add_account("personal")
        b.start(unlocked=["personal"])
        a = handle("personal", "A")
        b.call("POST", "/requests", {"site": "one.example.com", "reason": "first"})
        b.call("POST", "/requests", {"site": "two.example.com", "reason": "second"})
        expect(b.call("POST", "/release", {"handle": a})[1] == {"released": True, "returned": False}, "release")
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
    test_release_before_move()
    test_release_paths()
    test_regrant_clears_denylist()
    test_unreturned_listed_after_refused_move()
    test_unreturned_cleared_by_sync()
    test_unreturned_survives_lock_and_restart()
    test_unreturned_hidden_for_removed_account()
    test_orphan_unreturned_dropped_by_prune()
    test_unreturned_skips_returned_and_scope_all()
    test_regrant_clears_unreturned()
    test_retry_after_user_moved_item()
    test_sync_marks_requests_done()
    test_restart_keeps_requests_and_denylist()
    print("ok")


if __name__ == "__main__":
    sys.exit(main())
