#!/usr/bin/env python3
"""Search gate pacing, failover, cache, block records and HTTP answers, against a fake
SearXNG with 0.2 s intervals and no jitter. Ported from lead-gen's pytest suite."""

from __future__ import annotations

import importlib.util
import itertools
import json
import logging
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "compose" / "search-gate" / "search_gate.py"
INTERVAL = 0.2


def load_gate():
    spec = importlib.util.spec_from_file_location("search_gate", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["search_gate"] = module
    spec.loader.exec_module(module)
    return module


sg = load_gate()
Engine, Gate, Store = sg.Engine, sg.Gate, sg.Store
# Keep expected block warnings off the console
sg.logger.addHandler(logging.NullHandler())


class FakeSearXNG:
    """Answers with one result per query, or as unresponsive for engines in down."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = down or set()
        self.calls: list[tuple[float, str, str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, query: str, engine: str, params: dict) -> dict:
        with self._lock:
            self.calls.append((time.monotonic(), query, engine, params))
        if engine in self.down:
            return {
                "query": query,
                "results": [],
                "unresponsive_engines": [[engine, "CAPTCHA"]],
            }
        return {
            "query": query,
            "results": [{"url": f"https://{engine}.example/{query}", "title": query}],
            "unresponsive_engines": [],
        }


class Gates:
    """Makes gates and stops them all afterwards."""

    def __init__(self) -> None:
        self.made: list = []

    def __call__(self, engines, fetch, store=None, fallback_fetch=None):
        gate = Gate(
            engines,
            fetch,
            store or Store(":memory:"),
            jitter=0,
            fallback_fetch=fallback_fetch,
        )
        self.made.append(gate)
        return gate

    def stop(self) -> None:
        for gate in self.made:
            gate.stop()


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def search_all(gate, queries, wait=5.0):
    results = {}
    threads = [
        threading.Thread(
            target=lambda q=q: results.__setitem__(q, gate.search(q, wait)),
        )
        for q in queries
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def serve(gate, max_wait=5.0):
    handler = type("TestHandler", (sg.Handler,), {"gate": gate, "max_wait": max_wait})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def get_json(url):
    with urlopen(url) as r:
        return r.status, dict(r.headers), json.load(r)


def test_each_engine_searches_at_most_once_per_interval(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", INTERVAL)], fetch)

    results = search_all(gate, ["a", "b", "c"])

    assert all(r["results"] for r in results.values())
    times = [t for t, *_ in fetch.calls]
    assert len(times) == 3
    assert all(b - a >= INTERVAL * 0.95 for a, b in itertools.pairwise(times))


def test_queries_go_to_one_engine_each_and_share_the_load(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 10), Engine("google cse", 10)], fetch)

    results = search_all(gate, ["a", "b"])

    assert sorted(e for _, _, e, _ in fetch.calls) == ["bing", "google cse"]
    assert {r["gate"]["engine"] for r in results.values()} == {"bing", "google cse"}


def test_a_blocked_engine_cools_down_and_the_query_moves_on(gates):
    fetch = FakeSearXNG(down={"bing"})
    gate = gates([Engine("bing", 0.01), Engine("google cse", 0.01)], fetch)

    first = gate.search("a", 5)
    second = gate.search("b", 5)

    assert first["gate"]["engine"] == "google cse"
    # bing is cooling, so b doesn't try it at all
    assert [e for _, q, e, _ in fetch.calls if q == "b"] == ["google cse"]
    bing = gate.status()["engines"][0]
    assert bing["failures"] == 1
    assert bing["cooling_s"] > 0
    assert bing["last_error"] == "CAPTCHA"
    assert second["results"]


def test_a_query_every_engine_fails_on_gets_no_results(gates):
    gate = gates([Engine("bing", 0.01)], FakeSearXNG(down={"bing"}))

    result = gate.search("a", 5)

    assert result["results"] == []
    assert result["unresponsive_engines"][0][0] == "search-gate"


def test_results_are_cached_by_normalized_query(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 0.01)], fetch)

    gate.search("Acme  Electric Ogden", 5)
    again = gate.search("acme electric ogden ", 5)

    assert len(fetch.calls) == 1
    assert again["gate"]["cached"] is True


def test_forwarded_parameters_reach_searxng_and_key_the_cache(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 0.01)], fetch)

    gate.search("acme", 5, {"pageno": "1", "language": "all", "categories": "news"})
    gate.search("acme", 5, {"pageno": "2", "time_range": "week"})
    again = gate.search("ACME", 5, {"time_range": "week", "pageno": "2"})

    # Defaults and unknown parameters are dropped, so the first is a plain search
    assert [p for *_, p in fetch.calls] == [{}, {"pageno": "2", "time_range": "week"}]
    assert again["gate"]["cached"] is True


def test_identical_queries_waiting_together_share_one_search(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", INTERVAL)], fetch)
    gate.search("first", 5)  # makes the engine busy for INTERVAL

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(gate.search("same", 5)))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [q for _, q, _, _ in fetch.calls] == ["first", "same"]
    assert len(results) == 5


def test_a_search_that_waits_too_long_stays_queued(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 1.0)], fetch)
    gate.search("first", 5)

    assert gate.search("second", 0.1) is None
    assert gate.status()["waiting"] == 1
    # It runs on its turn and is cached for the next ask
    time.sleep(1.2)
    assert gate.search("second", 0)["gate"]["cached"] is True


def test_waiting_searches_go_before_the_bulk_queue(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", INTERVAL)], fetch)
    gate.search("first", 5)

    counts = gate.queue(["q1", "q2", "q3", "first"])
    gate.search("urgent", 5)

    assert counts == {"queued": 3, "cached": 1}
    order = [q for _, q, _, _ in fetch.calls]
    assert order[:2] == ["first", "urgent"]


def test_the_bulk_queue_survives_a_restart(gates, tmp_path):
    path = tmp_path / "gate.sqlite3"
    fetch = FakeSearXNG()
    slow = gates([Engine("bing", 60)], fetch, Store(path))
    slow.search("first", 5)
    slow.queue(["later"])
    slow.stop()

    fetch.calls.clear()
    resumed = gates([Engine("bing", 0.01)], fetch, Store(path))
    deadline = time.monotonic() + 5
    while not fetch.calls and time.monotonic() < deadline:
        time.sleep(0.02)

    assert [q for _, q, _, _ in fetch.calls] == ["later"]
    deadline = time.monotonic() + 5
    while resumed.status()["queued"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Store(path).queued() == []


def test_an_engine_stops_at_its_daily_cap(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 0.01, daily_cap=1)], fetch)

    assert gate.search("a", 5)["results"]
    assert gate.search("b", 0.3) is None
    assert gate.status()["engines"][0]["used_today"] == 1


def test_cache_lifetimes_come_from_the_store(tmp_path):
    store = Store(tmp_path / "ttl.sqlite3", ttl=0, empty_ttl=0)
    store.put("a", {"results": [{"url": "x"}]}, "bing")

    assert store.get("a") is None


def test_parse_engines():
    engines = sg.parse_engines("bing:10:600, google cse:20,")

    assert [(e.name, e.interval, e.daily_cap) for e in engines] == [
        ("bing", 10.0, 600),
        ("google cse", 20.0, None),
    ]


def test_the_http_api_answers_like_searxng(gates):
    gate = gates([Engine("bing", 0.01)], FakeSearXNG())
    server, base = serve(gate)
    try:
        _, _, body = get_json(f"{base}/search?q=acme&format=json")
        request = Request(
            f"{base}/queue",
            data=json.dumps({"queries": ["acme", "other"]}).encode(),
            method="POST",
        )
        with urlopen(request) as r:
            queued = json.load(r)
        try:
            urlopen(f"{base}/search?q=")
            missing = None
        except HTTPError as e:
            missing = e.code
    finally:
        server.shutdown()

    assert body["results"][0]["title"] == "acme"
    assert queued == {"queued": 1, "cached": 1}
    assert missing == 400


def test_a_search_that_times_out_gets_an_empty_answer_naming_the_gate(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("bing", 1.0)], fetch)
    gate.search("first", 5)
    server, base = serve(gate, max_wait=0.1)
    try:
        status, headers, body = get_json(f"{base}/search?q=second&format=json")
    finally:
        server.shutdown()

    assert status == 200
    assert headers["Retry-After"] == "60"
    assert body["results"] == []
    assert body["unresponsive_engines"][0][0] == "search-gate"
    assert body["gate"] == {"queued": True}


def test_a_block_is_recorded_from_first_failure_to_recovery(gates):
    records = Records()
    sg.logger.addHandler(records)
    cool_down = sg.COOL_DOWN_S
    sg.COOL_DOWN_S = 0.05
    try:
        fetch = FakeSearXNG(down={"bing"})
        gate = gates([Engine("bing", 0.01)], fetch)

        gate.search("a", 5)
        gate.search("b", 5)
        assert gate.status()["engines"][0]["blocked_since"] is not None
        fetch.down.clear()
        gate.search("c", 5)
    finally:
        sg.COOL_DOWN_S = cool_down
        sg.logger.removeHandler(records)

    [block] = gate.store.blocks()
    assert (block.engine, block.error, block.checks) == ("bing", "CAPTCHA", 2)
    assert block.started < block.last_failed < block.recovered
    engine = gate.status()["engines"][0]
    assert engine["blocked_since"] is None
    assert engine["blocks_ended"] == 1
    assert engine["median_block_at_least_s"] <= engine["median_block_at_most_s"]
    assert records.messages[0].startswith("bing blocked (CAPTCHA)")
    assert records.messages[1].startswith("bing still blocked after 2 failed checks")
    assert records.messages[2].startswith("bing answering again")


def test_the_fallback_waits_while_local_engines_are_only_paced(gates):
    local, remote = FakeSearXNG(), FakeSearXNG()
    gate = gates(
        [Engine("bing", INTERVAL), Engine("bing", 0.01, fallback=True)],
        local,
        fallback_fetch=remote,
    )

    results = search_all(gate, ["a", "b", "c"])

    assert len(local.calls) == 3
    assert remote.calls == []
    assert {r["gate"]["engine"] for r in results.values()} == {"bing"}
    assert gate.status()["fallback_active"] is False


def test_a_blocked_local_engine_sends_queries_to_the_fallback(gates):
    local, remote = FakeSearXNG(down={"bing"}), FakeSearXNG()
    gate = gates(
        [Engine("bing", 0.01), Engine("bing", 0.01, fallback=True)],
        local,
        fallback_fetch=remote,
    )

    first = gate.search("a", 5)
    second = gate.search("b", 5)

    assert first["gate"]["engine"] == "fallback:bing"
    assert first["results"]
    # Local bing is cooling, so b goes straight to the fallback
    assert [q for _, q, _, _ in local.calls] == ["a"]
    assert [q for _, q, _, _ in remote.calls] == ["a", "b"]
    assert second["gate"]["engine"] == "fallback:bing"
    status = gate.status()
    assert status["fallback_active"] is True
    assert [(e["name"], e["upstream"]) for e in status["engines"]] == [
        ("bing", "local"),
        ("fallback:bing", "fallback"),
    ]


def test_traffic_returns_to_local_when_its_cool_down_ends(gates):
    cool_down = sg.COOL_DOWN_S
    sg.COOL_DOWN_S = 0.05
    try:
        local, remote = FakeSearXNG(down={"bing"}), FakeSearXNG()
        gate = gates(
            [Engine("bing", 0.01), Engine("bing", 0.01, fallback=True)],
            local,
            fallback_fetch=remote,
        )
        assert gate.search("a", 5)["gate"]["engine"] == "fallback:bing"
        local.down.clear()
        time.sleep(0.1)
        back = gate.search("b", 5)
    finally:
        sg.COOL_DOWN_S = cool_down

    assert back["gate"]["engine"] == "bing"
    assert [q for _, q, _, _ in remote.calls] == ["a"]
    [block] = gate.store.blocks()
    assert block.engine == "bing"
    assert block.recovered is not None
    assert gate.status()["fallback_active"] is False


def test_a_query_both_upstreams_fail_on_gets_no_results(gates):
    local, remote = FakeSearXNG(down={"bing"}), FakeSearXNG(down={"bing"})
    gate = gates(
        [Engine("bing", 0.01), Engine("bing", 0.01, fallback=True)],
        local,
        fallback_fetch=remote,
    )

    result = gate.search("a", 5)

    assert result["results"] == []
    assert result["unresponsive_engines"][0][0] == "search-gate"
    # Each upstream keeps its own block record
    assert sorted(b.engine for b in gate.store.blocks()) == ["bing", "fallback:bing"]


def test_a_blocked_engine_s_twin_stands_in_while_other_local_engines_work(gates):
    local = FakeSearXNG(down={"google cse"})
    remote = FakeSearXNG()
    gate = gates(
        [
            Engine("bing", INTERVAL),
            Engine("google cse", 0.01),
            Engine("bing", 0.01, fallback=True),
            Engine("google cse", 0.01, fallback=True),
        ],
        local,
        fallback_fetch=remote,
    )

    results = [gate.search(q, 5) for q in ["a", "b", "c", "d"]]

    used = {r["gate"]["engine"] for r in results}
    assert used == {"bing", "fallback:google cse"}
    # Local bing works, so its twin never runs
    assert {e for _, _, e, _ in remote.calls} == {"google cse"}
    assert gate.status()["fallback_active"] is True


def test_a_fallback_engine_without_a_local_twin_waits_for_every_local_block(gates):
    local = FakeSearXNG(down={"google cse"})
    remote = FakeSearXNG()
    gate = gates(
        [
            Engine("bing", 0.01),
            Engine("google cse", 0.01),
            Engine("brave", 0.01, fallback=True),
        ],
        local,
        fallback_fetch=remote,
    )

    first = [gate.search(q, 5) for q in ["a", "b", "c"]]
    local.down.add("bing")
    time.sleep(0.05)
    last = gate.search("d", 5)

    assert remote.calls[0][1] == "d"
    assert {r["gate"]["engine"] for r in first} == {"bing"}
    assert last["gate"]["engine"] == "fallback:brave"


def test_a_twin_still_stands_in_when_the_tried_local_engine_recovers():
    gate = Gate.__new__(Gate)
    gate.engines = [Engine("bing", 1), Engine("bing", 1, fallback=True)]
    # Local bing failed on this query earlier, and its cool-down has since ended
    eligible = Gate._eligible(gate, 100.0, sg._today(), {"bing"})

    assert eligible(gate.engines[1]) is True
    assert Gate._eligible(gate, 100.0, sg._today())(gate.engines[1]) is False


def test_fallback_engines_need_a_fallback_fetch():
    try:
        Gate([Engine("bing", 1, fallback=True)], FakeSearXNG(), Store(":memory:"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_the_fallback_fetch_sends_basic_auth():
    seen = {}

    class Upstream(sg.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["path"] = self.path
            data = json.dumps({"results": [], "unresponsive_engines": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        sg.searxng_fetch(base, auth="user:secret")("acme", "bing", {})
        with_auth = seen.pop("auth")
        sg.searxng_fetch(base)("acme", "bing", {})
        without_auth = seen.pop("auth")
    finally:
        server.shutdown()

    assert with_auth == "Basic dXNlcjpzZWNyZXQ="
    assert without_auth is None
    assert seen["path"].startswith("/search?")


def main() -> None:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_")]
    for name, fn in tests:
        gates = Gates()
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = {
                "gates": gates,
                "tmp_path": Path(tmp),
            }
            args = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            try:
                fn(**{a: kwargs[a] for a in args})
            finally:
                gates.stop()
        print(f"ok {name}")
    print("OK search gate")


if __name__ == "__main__":
    main()
