"""
A pacing proxy in front of SearXNG, so batches of searches don't get us blocked.

SearXNG sends every query to every enabled engine at once, and GPT Researcher sends
five to eight queries per report in parallel. From one IP that trips CAPTCHAs, 429s
and Cloudflare blocks within a few dozen queries. The gate takes any number of
searches and sends them on one at a time per engine:

- Each query goes to one engine (SearXNG's ``engines`` parameter), the one free
  soonest, and each engine is used at most once per its interval, with jitter.
- An engine SearXNG reports as unresponsive (CAPTCHA, 403, 429, timeout) cools down,
  for twice as long after each failure in a row, and the query moves to another
  engine. A query every engine failed on is answered with no results.
- Results are cached in SQLite by normalized query, and identical queries waiting at
  the same time share one search.
- ``GET /search`` answers like SearXNG and waits for its turn, up to
  SEARCH_GATE_MAX_WAIT seconds, then returns 503 with the query still queued. Its
  result is cached when it runs, so asking again later returns it at once.
- ``POST /queue`` with ``{"queries": [...]}`` queues searches without waiting, behind
  any waiting ``/search``. The queue is kept in SQLite, so a restart resumes it.
- ``GET /status`` shows the queue, each engine's state and the cache size.
- Each block is recorded, from the failure that started it to the first search the
  engine answers again, and logged as it starts, continues and ends. ``GET /blocks``
  lists them, so cool-downs and intervals can be set from how long blocks last.

Configuration, all from the environment:

    SEARXNG_URL               SearXNG's base URL (http://searxng:8080)
    SEARCH_GATE_ENGINES       name:interval[:daily cap], comma-separated, in order of
                              preference ("yandex:15:600,google cse:40:200")
    SEARCH_GATE_DB            the SQLite file for the cache and queue
    SEARCH_GATE_MAX_WAIT      seconds a /search waits for its turn (600)
    SEARCH_GATE_PORT          the port to listen on (8080)

Run it with ``python -m lead_gen.research.search_gate``. It needs only the standard
library, so it runs in the Django image without Django.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_ENGINES = "yandex:15:600,google cse:40:200,yep:20:400"
# A result list stays good for weeks; an engine that answered with nothing may just
# have had a bad day.
TTL_RESULTS_S = 14 * 24 * 3600
TTL_EMPTY_S = 24 * 3600
COOL_DOWN_S = 600
MAX_COOL_DOWN_S = 6 * 3600
# Intervals vary by this share either way, so requests don't arrive on a beat
JITTER = 0.3
CAP_RECHECK_S = 600.0
FETCH_TIMEOUT_S = 30
SYNC, BULK = 0, 1


@dataclass
class Engine:
    name: str
    interval: float
    daily_cap: int | None = None
    next_at: float = 0.0
    cooling_until: float = 0.0
    failures: int = 0
    used_today: int = 0
    day: str = ""
    last_error: str | None = None

    def ready_at(self, now: float, today: str) -> float | None:
        """When this engine may search next, or None when it's used up for today."""
        if self.day != today:
            self.day, self.used_today = today, 0
        if self.daily_cap is not None and self.used_today >= self.daily_cap:
            return None
        return max(self.next_at, self.cooling_until, now)


@dataclass(frozen=True)
class Block:
    """A stretch of failures from one engine, and when it answered again."""

    engine: str
    error: str
    started: float
    last_failed: float
    checks: int
    recovered: float | None

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "error": self.error,
            "started": _iso(self.started),
            "failed_checks": self.checks,
            "recovered": _iso(self.recovered) if self.recovered else None,
            "lasted_at_least_s": round(self.last_failed - self.started),
            "lasted_at_most_s": (
                round(self.recovered - self.started) if self.recovered else None
            ),
        }


@dataclass
class Job:
    key: str
    query: str
    priority: int
    added: float
    tried: set[str] = field(default_factory=set)
    running: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


def parse_engines(spec: str) -> list[Engine]:
    engines = []
    for item in spec.split(","):
        if not item.strip():
            continue
        parts = [p.strip() for p in item.split(":")]
        cap = int(parts[2]) if len(parts) > 2 and parts[2] else None  # noqa: PLR2004
        engines.append(Engine(parts[0], float(parts[1]), cap))
    return engines


def normalize(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip().casefold()


def searxng_fetch(base_url: str) -> Callable[[str, str], dict]:
    """A fetch(query, engine) that asks SearXNG with only that engine."""

    def fetch(query: str, engine: str) -> dict:
        params = {"q": query, "format": "json", "engines": engine}
        url = f"{base_url.rstrip('/')}/search?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310
            return json.load(response)

    return fetch


class Store:
    """The result cache and the bulk queue, in one SQLite file."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, "
                "payload TEXT, engine TEXT, fetched REAL, expires REAL)",
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS queue (key TEXT PRIMARY KEY, query TEXT, "
                "added REAL)",
            )
            # One row per block: started is the first failure, last_failed the latest,
            # and recovered the first answer after them, so the block lasted at least
            # last_failed - started and at most recovered - started.
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS blocks (engine TEXT, error TEXT, "
                "started REAL, last_failed REAL, checks INTEGER, recovered REAL)",
            )

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM results WHERE key = ? AND expires > ?",
                (key, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, payload: dict, engine: str) -> None:
        ttl = TTL_RESULTS_S if payload.get("results") else TTL_EMPTY_S
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)",
                (key, json.dumps(payload), engine, now, now + ttl),
            )

    def enqueue(self, key: str, query: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO queue VALUES (?, ?, ?)",
                (key, query, time.time()),
            )

    def dequeue(self, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM queue WHERE key = ?", (key,))

    def queued(self) -> list[tuple[str, str]]:
        with self._lock:
            return self._db.execute(
                "SELECT key, query FROM queue ORDER BY added",
            ).fetchall()

    def block_failed(self, engine: str, error: str, now: float) -> Block:
        """Record a failure: a new block, or one more failed check on an open one."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT rowid, started, checks FROM blocks "
                "WHERE engine = ? AND recovered IS NULL",
                (engine,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO blocks VALUES (?, ?, ?, ?, 1, NULL)",
                    (engine, error, now, now),
                )
                return Block(engine, error, now, now, 1, None)
            self._db.execute(
                "UPDATE blocks SET error = ?, last_failed = ?, checks = checks + 1 "
                "WHERE rowid = ?",
                (error, now, row[0]),
            )
            return Block(engine, error, row[1], now, row[2] + 1, None)

    def block_ended(self, engine: str, now: float) -> Block | None:
        """Close the engine's open block, if it has one, and return it."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT rowid, error, started, last_failed, checks FROM blocks "
                "WHERE engine = ? AND recovered IS NULL",
                (engine,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE blocks SET recovered = ? WHERE rowid = ?",
                (now, row[0]),
            )
        return Block(engine, row[1], row[2], row[3], row[4], now)

    def blocks(self) -> list[Block]:
        with self._lock:
            rows = self._db.execute(
                "SELECT engine, error, started, last_failed, checks, recovered "
                "FROM blocks ORDER BY started",
            ).fetchall()
        return [Block(*row) for row in rows]

    def cached(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT count(*) FROM results WHERE expires > ?",
                (time.time(),),
            ).fetchone()[0]


class Gate:
    """Queues searches and sends them to one engine at a time, each at its pace."""

    def __init__(  # noqa: PLR0913
        self,
        engines: list[Engine],
        fetch: Callable[[str, str], dict],
        store: Store,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        jitter: float = JITTER,
    ) -> None:
        self.engines = engines
        self.fetch = fetch
        self.store = store
        self.clock = clock
        self.wall = wall
        self.jitter = jitter
        self.jobs: dict[str, Job] = {}
        self.searched = 0
        self._cond = threading.Condition()
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(engines)))
        self._stopped = False
        for key, query in store.queued():
            self.jobs[key] = Job(key, query, BULK, self.clock())
        self._thread = threading.Thread(target=self._dispatch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join()
        self._pool.shutdown(wait=True)

    def search(self, query: str, wait: float) -> dict | None:
        """The results for query, from the cache or after its turn; None when its turn
        didn't come within wait seconds (it stays queued)."""
        key = normalize(query)
        if (cached := self.store.get(key)) is not None:
            return cached | {"gate": {"cached": True}}
        job = self._submit(key, query, SYNC)
        if not job.done.wait(wait):
            return None
        return job.result

    def queue(self, queries: list[str]) -> dict:
        """Queue searches without waiting; return how many were queued and cached."""
        queued = cached = 0
        for query in queries:
            key = normalize(query)
            if not key:
                continue
            if self.store.get(key) is not None:
                cached += 1
                continue
            self.store.enqueue(key, query)
            self._submit(key, query, BULK)
            queued += 1
        return {"queued": queued, "cached": cached}

    def status(self) -> dict:
        now = self.clock()
        today = _today()
        with self._cond:
            waiting = list(self.jobs.values())
            engines = [
                {
                    "name": e.name,
                    "interval_s": e.interval,
                    "daily_cap": e.daily_cap,
                    "used_today": e.used_today if e.day == today else 0,
                    "cooling_s": round(max(0.0, e.cooling_until - now)),
                    "failures": e.failures,
                    "last_error": e.last_error,
                }
                for e in self.engines
            ]
        blocks = self.store.blocks()
        for engine in engines:
            engine.update(
                _block_summary([b for b in blocks if b.engine == engine["name"]]),
            )
        return {
            "waiting": sum(j.priority == SYNC for j in waiting),
            "queued": sum(j.priority == BULK for j in waiting),
            "searched": self.searched,
            "cached": self.store.cached(),
            "engines": engines,
        }

    def _submit(self, key: str, query: str, priority: int) -> Job:
        with self._cond:
            job = self.jobs.get(key)
            if job is None:
                job = Job(key, query, priority, self.clock())
                self.jobs[key] = job
            else:
                # A /search for a query already queued in bulk moves it ahead
                job.priority = min(job.priority, priority)
            self._cond.notify_all()
            return job

    def _dispatch(self) -> None:
        while True:
            with self._cond:
                if self._stopped:
                    return
                picked, delay = self._pick()
                if picked is None:
                    self._cond.wait(timeout=delay)
                    continue
                job, engine = picked
                job.tried.add(engine.name)
                job.running = True
                engine.used_today += 1
                spread = 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311
                engine.next_at = self.clock() + engine.interval * spread
            self._pool.submit(self._run, job, engine)

    def _pick(self) -> tuple[tuple[Job, Engine] | None, float | None]:
        """The next job and the engine for it, or None and how long to wait."""
        waiting = [j for j in self.jobs.values() if not j.running]
        if not waiting:
            return None, None
        job = min(waiting, key=lambda j: (j.priority, j.added))
        untried = [e for e in self.engines if e.name not in job.tried]
        if not untried:
            self._finish(job, _empty(job.query, "every engine failed on it"))
            return None, 0.0
        now = self.clock()
        today = _today()
        options = [
            (ready, i, e)
            for i, e in enumerate(untried)
            if (ready := e.ready_at(now, today)) is not None
        ]
        if not options:
            # Every engine left is used up for today; look again in a while
            return None, CAP_RECHECK_S
        ready, _, engine = min(options, key=lambda o: (o[0], o[1]))
        if ready > now:
            return None, ready - now
        return (job, engine), None

    def _run(self, job: Job, engine: Engine) -> None:
        try:
            payload = self.fetch(job.query, engine.name)
            error = next(
                (
                    reason
                    for name, reason in payload.get("unresponsive_engines") or []
                    if name == engine.name
                ),
                None,
            )
        except Exception as e:  # noqa: BLE001 - any failure is the engine's turn lost
            payload, error = None, f"{type(e).__name__}: {e}"
        with self._cond:
            self.searched += 1
            if error is None:
                engine.failures, engine.last_error = 0, None
            else:
                engine.failures += 1
                engine.last_error = error
                cool = min(COOL_DOWN_S * 2 ** (engine.failures - 1), MAX_COOL_DOWN_S)
                engine.cooling_until = self.clock() + cool
                block = self.store.block_failed(engine.name, error, self.wall())
                if block.checks == 1:
                    logger.warning(
                        "%s blocked (%s); resting %s",
                        engine.name,
                        error,
                        _span(cool),
                    )
                else:
                    logger.warning(
                        "%s still blocked after %s failed checks over %s (%s); "
                        "resting %s",
                        engine.name,
                        block.checks,
                        _span(block.last_failed - block.started),
                        error,
                        _span(cool),
                    )
                # Try the query on another engine
                job.running = False
                self._cond.notify_all()
                return
        if (block := self.store.block_ended(engine.name, self.wall())) is not None:
            logger.warning(
                "%s answering again: blocked for between %s and %s, %s failed checks",
                engine.name,
                _span(block.last_failed - block.started),
                _span(block.recovered - block.started),
                block.checks,
            )
        self.store.put(job.key, payload, engine.name)
        payload = payload | {"gate": {"cached": False, "engine": engine.name}}
        with self._cond:
            self._finish(job, payload)

    def _finish(self, job: Job, payload: dict) -> None:
        job.result = payload
        self.jobs.pop(job.key, None)
        self.store.dequeue(job.key)
        job.done.set()
        self._cond.notify_all()


def _block_summary(blocks: list[Block]) -> dict:
    """When the engine's open block started, and how long its ended blocks lasted."""
    open_blocks = [b for b in blocks if b.recovered is None]
    ended = [b for b in blocks if b.recovered is not None]
    summary: dict[str, Any] = {
        "blocked_since": _iso(open_blocks[0].started) if open_blocks else None,
        "blocks_ended": len(ended),
    }
    if ended:
        most = sorted(b.recovered - b.started for b in ended)
        least = sorted(b.last_failed - b.started for b in ended)
        summary |= {
            "median_block_at_least_s": round(least[len(least) // 2]),
            "median_block_at_most_s": round(most[len(most) // 2]),
            "longest_block_at_most_s": round(most[-1]),
        }
    return summary


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _span(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:  # noqa: PLR2004
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _empty(query: str, why: str) -> dict:
    return {
        "query": query,
        "results": [],
        "unresponsive_engines": [["search-gate", why]],
    }


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


class Handler(BaseHTTPRequestHandler):
    gate: Gate
    max_wait: float

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parts.query)
        if parts.path == "/healthz":
            self._send(HTTPStatus.OK, {"ok": True})
        elif parts.path == "/status":
            self._send(HTTPStatus.OK, self.gate.status())
        elif parts.path == "/blocks":
            blocks = [b.as_dict() for b in self.gate.store.blocks()]
            self._send(HTTPStatus.OK, {"blocks": blocks})
        elif parts.path == "/search":
            query = (params.get("q") or [""])[0]
            if not query.strip():
                self._send(HTTPStatus.BAD_REQUEST, {"error": "q is required"})
                return
            result = self.gate.search(query, self.max_wait)
            if result is None:
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "still queued; ask again later", "query": query},
                    {"Retry-After": "60"},
                )
            else:
                self._send(HTTPStatus.OK, result)
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/queue":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            queries = [str(q) for q in body["queries"]]
        except KeyError, TypeError, ValueError:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": 'send {"queries": ["...", ...]}'},
            )
            return
        self._send(HTTPStatus.ACCEPTED, self.gate.queue(queries))

    def _send(
        self,
        status: HTTPStatus,
        body: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Queries can name people; log only that a request happened
        logger.debug("%s %s", self.command, urllib.parse.urlsplit(self.path).path)


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # Each waiting /search holds a thread; small stacks let a thousand wait
    threading.stack_size(512 * 1024)
    gate = Gate(
        parse_engines(os.environ.get("SEARCH_GATE_ENGINES") or DEFAULT_ENGINES),
        searxng_fetch(os.environ.get("SEARXNG_URL") or "http://searxng:8080"),
        Store(os.environ.get("SEARCH_GATE_DB") or "data/search-gate/gate.sqlite3"),
    )
    Handler.gate = gate
    Handler.max_wait = float(os.environ.get("SEARCH_GATE_MAX_WAIT") or 600)
    port = int(os.environ.get("SEARCH_GATE_PORT") or 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    server.daemon_threads = True
    logger.info(
        "search gate on :%s, engines %s",
        port,
        ", ".join(f"{e.name} every {e.interval:g}s" for e in gate.engines),
    )
    server.serve_forever()


if __name__ == "__main__":
    serve()
