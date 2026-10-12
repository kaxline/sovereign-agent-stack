"""Turn heartbeat and one-session live status for the dashboard gateway.

Compatibility overlay (not upstream), installed onto ``tui_gateway.server`` by
patch-gateway-liveness.py. Upstream's usage ticker skips frozen counters, so a
single slow model call (a local model reading a long prompt) is silent on the
wire for minutes, and the only per-session state query is ``session.active_list``,
which reads every live session's title and builds a transcript preview.

- ``turn.heartbeat`` every ``HERMES_TURN_HEARTBEAT_S`` seconds (default 30,
  ``0`` turns it off) while a turn runs, from the usage ticker's thread so none
  can arrive after ``message.complete``.
- ``session.live_status`` answers for one session from memory: no DB read, no
  title, no message text. A session that is not live answers ``idle``.
- ``gateway.capabilities`` advertises both.

Contracts are declared at install time, so upstream contract files stay untouched.
Payloads carry numbers and enum values only, never transcript content or the
agent's activity description (status text can quote the conversation).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

HEARTBEAT_ENV = "HERMES_TURN_HEARTBEAT_S"
DEFAULT_HEARTBEAT_S = 30.0
EVENT = "turn.heartbeat"
METHOD = "session.live_status"


def heartbeat_interval() -> float:
    """Seconds between heartbeats; 0 when off. A typo, NaN, Inf or negative warns and uses the default."""
    raw = os.environ.get(HEARTBEAT_ENV, "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        logger.warning("invalid %s=%r; using %.0fs", HEARTBEAT_ENV, raw, DEFAULT_HEARTBEAT_S)
        return DEFAULT_HEARTBEAT_S
    return value


def idle_seconds(session: dict | None) -> float | None:
    """Seconds since the agent last stamped its activity clock (API waits, stream chunks, tool
    progress): the clock Hermes' turn-liveness watchdog samples. None when unknown."""
    sess = session or {}
    if sess.get("_compute_host_turn_id"):
        stamp = sess.get("_compute_host_activity_ns")
        if isinstance(stamp, int):
            return round(max(0.0, (time.perf_counter_ns() - stamp) / 1e9), 1)
        return None
    summary_fn = getattr(sess.get("agent"), "get_activity_summary", None)
    if not callable(summary_fn):
        return None
    with contextlib.suppress(Exception):
        elapsed = summary_fn().get("seconds_since_activity")
        if elapsed is not None and math.isfinite(float(elapsed)):
            return round(max(0.0, float(elapsed)), 1)
    return None


def live_state(server: Any, sid: str, session: dict | None) -> dict:
    """``status`` / ``running`` / ``turn_started_at`` / ``idle_s`` for one session record."""
    if session is None or session.get("_finalized"):
        return {"status": "idle", "running": False, "turn_started_at": None, "idle_s": None}
    running = bool(session.get("running"))
    return {
        "status": server._session_live_status(sid, session),
        "running": running,
        "turn_started_at": server._turn_started_at(session),
        "idle_s": idle_seconds(session) if running else None,
    }


def find_session(server: Any, params: dict) -> tuple[str, dict] | None:
    """Live record by gateway ``session_id``, else by stored ``session_key``. With ``profile``,
    only that profile's sessions match; without it, any profile does (as ``session.active_list``)."""
    has_profile = bool(str(params.get("profile") or "").strip())
    # Raises ProfileUnavailableError for an unknown profile; the dispatcher maps it to 4064.
    home = server._profile_home(params.get("profile")) if has_profile else server._ANY_PROFILE
    sid = str(params.get("session_id") or "")
    if sid:
        with server._sessions_lock:
            session = server._sessions.get(sid)
        if session is not None and not session.get("_finalized") and server._live_profile_matches(session, home):
            return sid, session
    key = str(params.get("session_key") or "")
    if key:
        return server._find_live_session_by_key(key, home)
    return None


def declare_contracts(server: Any) -> None:
    from tui_gateway.contracts import base, liveness, registry, sessions

    class SessionLiveStatusParams(base.Params):
        session_id: str | None = None
        session_key: str | None = None
        profile: str | None = None

    class SessionLiveStatusResult(base.Result):
        session_id: str | None
        session_key: str | None
        status: sessions.LiveSessionStatus
        running: bool
        turn_started_at: float | None
        idle_s: float | None

    class TurnHeartbeatPayload(base.Payload):
        """``gateway_liveness`` (stack overlay)."""

        running: bool
        status: sessions.LiveSessionStatus
        turn_started_at: float | None
        idle_s: float | None
        interval_s: float

    class GatewayCapabilitiesResult(liveness.GatewayCapabilitiesResult):
        turn_heartbeat: bool
        turn_heartbeat_s: float
        session_live_status: bool

    if METHOD not in registry.METHODS:
        registry.method(
            METHOD, params=SessionLiveStatusParams, result=SessionLiveStatusResult,
            doc="One session's live state from memory (stack overlay). Not live answers idle.")
    if EVENT not in registry.EVENTS:
        registry.event(
            EVENT, TurnHeartbeatPayload,
            doc="Sent at a fixed interval while a turn runs, never after message.complete (stack overlay).")
    caps = registry.METHODS["gateway.capabilities"]
    if caps.result is liveness.GatewayCapabilitiesResult:
        registry.METHODS[caps.name] = registry.MethodContract(caps.name, caps.params, GatewayCapabilitiesResult, caps.doc)


class _JoinBoth:
    """Stands in for the ticker thread: the turn joins it before ``message.complete``."""

    def __init__(self, *threads):
        self._threads = threads

    def join(self, timeout: float | None = None) -> None:
        for thread in self._threads:
            thread.join(timeout)

    def is_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)


def wrap_usage_ticker(server: Any, start_ticker):
    def _start(sid: str, agent, *args, **kwargs):
        stop, thread = start_ticker(sid, agent, *args, **kwargs)
        interval = heartbeat_interval()
        if interval <= 0:
            return stop, thread

        def _beat() -> None:
            while not stop.wait(interval):
                with contextlib.suppress(Exception):
                    session = server._sessions.get(sid)
                    if session is None or not session.get("running"):
                        continue
                    payload = {**live_state(server, sid, session), "interval_s": interval}
                    if stop.is_set():
                        break  # turn ended while sampling: message.complete is next
                    server._emit(EVENT, sid, payload)

        beat = server._RealThread(target=_beat, daemon=True, name=f"turn-heartbeat-{sid[:8]}")
        beat.start()
        return stop, _JoinBoth(thread, beat)

    _start.__wrapped__ = start_ticker
    return _start


def live_status_handler(server: Any):
    def handler(rid, params: dict) -> dict:
        if not (params.get("session_id") or params.get("session_key")):
            return server._err(rid, 4006, "session_id or session_key required")
        found = find_session(server, params)
        if found is None:
            return server._ok(rid, {
                "session_id": params.get("session_id") or None, "session_key": params.get("session_key") or None,
                **live_state(server, "", None)})
        sid, session = found
        return server._ok(rid, {
            "session_id": sid, "session_key": server._session_lookup_key(session, fallback=sid) or None,
            **live_state(server, sid, session)})
    return handler


def wrap_capabilities(server: Any, handler):
    def _caps(rid, params: dict) -> dict:
        response = handler(rid, params)
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, dict):
            interval = heartbeat_interval()
            result.update(turn_heartbeat=interval > 0, turn_heartbeat_s=interval, session_live_status=True)
        return response
    _caps.__wrapped__ = handler
    return _caps


def install(server: Any) -> None:
    if getattr(server, "_gateway_liveness_installed", False):
        return
    declare_contracts(server)
    # Split modules (prompt_turn) are rebound onto the server namespace, so the turn
    # resolves _start_usage_ticker here at call time.
    server._start_usage_ticker = wrap_usage_ticker(server, server._start_usage_ticker)
    server.register_method(METHOD, live_status_handler(server))
    server.register_method("gateway.capabilities", wrap_capabilities(server, server._methods["gateway.capabilities"]))
    server._gateway_liveness_installed = True
