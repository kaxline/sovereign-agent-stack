import itertools
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from lead_gen.research import search_gate
from lead_gen.research.search_gate import Engine
from lead_gen.research.search_gate import Gate
from lead_gen.research.search_gate import Store

INTERVAL = 0.2


class FakeSearXNG:
    """Answers with one result per query, or as unresponsive for engines in down."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = down or set()
        self.calls: list[tuple[float, str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, query: str, engine: str) -> dict:
        with self._lock:
            self.calls.append((time.monotonic(), query, engine))
        if engine in self.down:
            return {
                "query": query,
                "results": [],
                "unresponsive_engines": [
                    [engine, "CAPTCHA"],
                ],
            }
        return {
            "query": query,
            "results": [{"url": f"https://{engine}.example/{query}", "title": query}],
            "unresponsive_engines": [],
        }


@pytest.fixture
def gates():
    made = []

    def make(engines, fetch, store=None):
        gate = Gate(engines, fetch, store or Store(":memory:"), jitter=0)
        made.append(gate)
        return gate

    yield make
    for gate in made:
        gate.stop()


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


def test_each_engine_searches_at_most_once_per_interval(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", INTERVAL)], fetch)

    results = search_all(gate, ["a", "b", "c"])

    assert all(r["results"] for r in results.values())
    times = [t for t, _, _ in fetch.calls]
    assert len(times) == 3  # noqa: PLR2004
    assert all(b - a >= INTERVAL * 0.95 for a, b in itertools.pairwise(times))


def test_queries_go_to_one_engine_each_and_share_the_load(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", 10), Engine("yep", 10)], fetch)

    results = search_all(gate, ["a", "b"])

    assert sorted(e for _, _, e in fetch.calls) == ["yandex", "yep"]
    assert {r["gate"]["engine"] for r in results.values()} == {"yandex", "yep"}


def test_a_blocked_engine_cools_down_and_the_query_moves_on(gates):
    fetch = FakeSearXNG(down={"yandex"})
    gate = gates([Engine("yandex", 0.01), Engine("yep", 0.01)], fetch)

    first = gate.search("a", 5)
    second = gate.search("b", 5)

    assert first["gate"]["engine"] == "yep"
    # yandex is cooling, so b doesn't try it at all
    assert [e for _, q, e in fetch.calls if q == "b"] == ["yep"]
    yandex = gate.status()["engines"][0]
    assert yandex["failures"] == 1
    assert yandex["cooling_s"] > 0
    assert yandex["last_error"] == "CAPTCHA"
    assert second["results"]


def test_a_query_every_engine_fails_on_gets_no_results(gates):
    gate = gates([Engine("yandex", 0.01)], FakeSearXNG(down={"yandex"}))

    result = gate.search("a", 5)

    assert result["results"] == []
    assert result["unresponsive_engines"][0][0] == "search-gate"


def test_results_are_cached_by_normalized_query(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", 0.01)], fetch)

    gate.search("Acme  Electric Ogden", 5)
    again = gate.search("acme electric ogden ", 5)

    assert len(fetch.calls) == 1
    assert again["gate"]["cached"] is True


def test_identical_queries_waiting_together_share_one_search(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", INTERVAL)], fetch)
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

    assert [q for _, q, _ in fetch.calls] == ["first", "same"]
    assert len(results) == 5  # noqa: PLR2004


def test_a_search_that_waits_too_long_stays_queued(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", 1.0)], fetch)
    gate.search("first", 5)

    assert gate.search("second", 0.1) is None
    assert gate.status()["waiting"] == 1
    # It runs on its turn and is cached for the next ask
    time.sleep(1.2)
    assert gate.search("second", 0)["gate"]["cached"] is True


def test_waiting_searches_go_before_the_bulk_queue(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", INTERVAL)], fetch)
    gate.search("first", 5)

    counts = gate.queue(["q1", "q2", "q3", "first"])
    gate.search("urgent", 5)

    assert counts == {"queued": 3, "cached": 1}
    order = [q for _, q, _ in fetch.calls]
    assert order[:2] == ["first", "urgent"]


def test_the_bulk_queue_survives_a_restart(gates, tmp_path):
    path = tmp_path / "gate.sqlite3"
    fetch = FakeSearXNG()
    slow = gates([Engine("yandex", 60)], fetch, Store(path))
    slow.search("first", 5)
    slow.queue(["later"])
    slow.stop()

    fetch.calls.clear()
    resumed = gates([Engine("yandex", 0.01)], fetch, Store(path))
    deadline = time.monotonic() + 5
    while not fetch.calls and time.monotonic() < deadline:
        time.sleep(0.02)

    assert [q for _, q, _ in fetch.calls] == ["later"]
    deadline = time.monotonic() + 5
    while resumed.status()["queued"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Store(path).queued() == []


def test_an_engine_stops_at_its_daily_cap(gates):
    fetch = FakeSearXNG()
    gate = gates([Engine("yandex", 0.01, daily_cap=1)], fetch)

    assert gate.search("a", 5)["results"]
    assert gate.search("b", 0.3) is None
    assert gate.status()["engines"][0]["used_today"] == 1


def test_parse_engines():
    engines = search_gate.parse_engines("yandex:15:600, google cse:40,")

    assert [(e.name, e.interval, e.daily_cap) for e in engines] == [
        ("yandex", 15.0, 600),
        ("google cse", 40.0, None),
    ]


def test_the_http_api_answers_like_searxng(gates):
    gate = gates([Engine("yandex", 0.01)], FakeSearXNG())
    handler = type(
        "TestHandler",
        (search_gate.Handler,),
        {"gate": gate, "max_wait": 5.0},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/search?q=acme&format=json") as r:  # noqa: S310
            body = json.load(r)
        request = urllib.request.Request(  # noqa: S310
            f"{base}/queue",
            data=json.dumps({"queries": ["acme", "other"]}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request) as r:  # noqa: S310
            queued = json.load(r)
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/search?q=")  # noqa: S310
    finally:
        server.shutdown()

    assert body["results"][0]["title"] == "acme"
    assert queued == {"queued": 1, "cached": 1}
    assert missing.value.code == 400  # noqa: PLR2004


def test_a_block_is_recorded_from_first_failure_to_recovery(
    gates,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(search_gate, "COOL_DOWN_S", 0.05)
    fetch = FakeSearXNG(down={"yandex"})
    gate = gates([Engine("yandex", 0.01)], fetch)

    gate.search("a", 5)
    gate.search("b", 5)
    assert gate.status()["engines"][0]["blocked_since"] is not None
    fetch.down.clear()
    gate.search("c", 5)

    [block] = gate.store.blocks()
    assert (block.engine, block.error, block.checks) == ("yandex", "CAPTCHA", 2)
    assert block.started < block.last_failed < block.recovered
    engine = gate.status()["engines"][0]
    assert engine["blocked_since"] is None
    assert engine["blocks_ended"] == 1
    assert engine["median_block_at_least_s"] <= engine["median_block_at_most_s"]
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("yandex blocked (CAPTCHA)")
    assert messages[1].startswith("yandex still blocked after 2 failed checks")
    assert messages[2].startswith("yandex answering again")
