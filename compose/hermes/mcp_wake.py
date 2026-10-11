"""Wake a parked MCP server in every Hermes gateway process.

Compatibility overlay (not upstream); installed as tools/mcp_wake.py by
patch-mcp-wake.py.

Hermes parks an MCP server after 5 failed reconnects. Parking drops its tools
and probes again only every 300 s. Two additions:

- ``mcp_servers.<name>.parked_retry_interval`` sets that probe interval per
  server (floor 10 s).
- ``POST /v1/mcp/servers/{name}/reconnect`` asks every gateway process to
  revive its parked connection now.

Each profile is its own ``hermes -p <name> gateway run`` process, and the
default profile has no API server. So the endpoint writes
``<hermes root>/mcp-wake/<name>``, and a watcher thread in every process with
an MCP loop polls that directory. A request stays live for WAKE_WINDOW_SECONDS
so a server that parks shortly after it is still woken. Each connection takes a
given request once, and only while parked: a connected session is never torn
down.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.mcp_tool")

WAKE_DIR_NAME = "mcp-wake"
MIN_PARKED_RETRY_SECONDS = 10.0
POLL_SECONDS = 1.0
WAKE_WINDOW_SECONDS = 30.0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_watcher_lock = threading.Lock()
_watcher: Optional[threading.Thread] = None


def parked_retry_interval(config: Any, default: float) -> float:
    """Per-server parked probe interval: ``parked_retry_interval`` (floor 10 s), else *default*."""
    raw = config.get("parked_retry_interval") if isinstance(config, dict) else None
    if raw is None or isinstance(raw, bool):
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("MCP parked_retry_interval %r is not a number; using %ss", raw, default)
        return float(default)
    return max(MIN_PARKED_RETRY_SECONDS, value)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def wake_dir(root: Optional[Path] = None) -> Path:
    return (root or hermes_root()) / WAKE_DIR_NAME


def configured_profiles(name: str, root: Optional[Path] = None) -> list[str]:
    """Profiles whose config.yaml lists *name* under ``mcp_servers``."""
    import yaml

    root = root or hermes_root()
    homes = [("default", root)]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += sorted((p.name, p) for p in profiles.iterdir()
                        if p.is_dir() and not p.name.startswith("."))
    found = []
    for profile, home in homes:
        try:
            config = yaml.safe_load((home / "config.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        servers = config.get("mcp_servers") if isinstance(config, dict) else None
        if isinstance(servers, dict) and name in servers:
            found.append(profile)
    return found


def request_wake(name: str, root: Optional[Path] = None) -> None:
    """Record a wake request for *name*; watchers compare the file's mtime."""
    directory = wake_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{name}.{os.getpid()}.tmp"
    tmp.write_text(f"{time.time():.3f}\n")
    os.replace(tmp, directory / name)


def handle_reconnect(name: str, root: Optional[Path] = None) -> tuple[dict, int]:
    """Body and status for ``POST /v1/mcp/servers/{name}/reconnect``."""
    if not valid_name(name):
        return {"error": {"message": "Invalid MCP server name", "type": "invalid_request_error",
                          "code": "invalid_mcp_server_name"}}, 400
    profiles = configured_profiles(name, root)
    if not profiles:
        return {"error": {"message": f"MCP server '{name}' is not configured in any profile",
                          "type": "invalid_request_error", "code": "mcp_server_not_found"}}, 404
    request_wake(name, root)
    logger.info("MCP server '%s': wake requested for profiles %s", name, ", ".join(profiles))
    return {"server": name, "profiles": profiles, "status": "wake_requested"}, 202


def wake_parked(name: str, request_ns: int) -> int:
    """Signal this process's parked connections named *name* that have not taken *request_ns*."""
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_loop import _signal_reconnect

    with _core._lock:
        targets = []
        for srv in list(_core._servers.values()):
            if getattr(srv, "name", None) != name or not getattr(srv, "_assistant_parked", False):
                continue
            if getattr(srv, "_assistant_wake_ns", 0) >= request_ns:
                continue
            srv._assistant_wake_ns = request_ns
            srv._assistant_parked = False
            targets.append(srv)
    woken = sum(1 for srv in targets if _signal_reconnect(srv))
    if woken:
        logger.info("MCP server '%s': woken from parked state by request", name)
    return woken


def _poll_once(directory: Path, started_ns: int) -> None:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    window_ns = int(WAKE_WINDOW_SECONDS * 1e9)
    now_ns = time.time_ns()
    for entry in entries:
        if entry.name.startswith(".") or not valid_name(entry.name):
            continue
        try:
            mtime_ns = entry.stat().st_mtime_ns
        except OSError:
            continue
        # Requests older than this process are stale: its connections started fresh.
        if mtime_ns <= started_ns or now_ns - mtime_ns > window_ns:
            continue
        wake_parked(entry.name, mtime_ns)


def _watch(directory: Path, started_ns: int) -> None:
    while True:
        try:
            _poll_once(directory, started_ns)
        except Exception:  # noqa: BLE001 - the watcher must outlive one bad poll
            logger.debug("MCP wake watcher poll failed", exc_info=True)
        time.sleep(POLL_SECONDS)


def start_watcher() -> None:
    """Start this process's wake watcher once (daemon thread)."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher.is_alive():
            return
        try:
            directory = wake_dir()
        except Exception:  # noqa: BLE001
            logger.debug("MCP wake watcher not started: no Hermes root", exc_info=True)
            return
        _watcher = threading.Thread(target=_watch, args=(directory, time.time_ns()),
                                    name="mcp-wake-watcher", daemon=True)
        _watcher.start()
