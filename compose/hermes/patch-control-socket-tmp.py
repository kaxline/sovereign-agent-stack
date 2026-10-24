#!/usr/bin/env python3
"""Put each gateway's control socket on the container filesystem.

Compatibility overlay (not upstream). Hermes v0.22 binds the gateway control
socket at $HERMES_HOME/gateway.sock, and every other gateway asks it which
profiles it serves before starting. $HERMES_HOME is data/hermes, a Docker
Desktop virtiofs bind mount, where connect() on a Unix socket fails with
EOPNOTSUPP. The answer never comes, so each standalone profile gateway
decides it must replace the default gateway, refuses (it cannot prove that
PID is its own), and s6 restarts it in a loop.

With HERMES_CONTROL_SOCKET_IN_TMP=1 the server binds /tmp/hermes-gw-<hash>.sock,
writes that path to $HERMES_HOME/gateway.sock.path, and clients follow the
pointer: Hermes' own fallback for a home path too long for sun_path, except that
the directory is always /tmp. Hermes' fallback uses tempfile.gettempdir(), and
gateway processes point TMPDIR at $HERMES_HOME/cache/scratch, which is virtiofs
too. Stale socket files there (left by a Docker VM restart) cannot even be
unlinked, which is what broke the direct path. Every gateway, the dashboard
and `hermes` CLI calls share the container's /tmp, so they all reach each other.

v0.22.3 changed the fallback layout to <dir>/hermes-gw-<hash>/control.sock: the
server creates that directory 0700 and refuses a socket whose parent is not
private, and gateway_runtime_discovery (used by gateway --replace and the
session runtime) accepts a pointer only to that leaf. The /tmp path follows the
same layout, and discovery skips $HERMES_HOME/gateway.sock like the client does.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
TARGET = ROOT / "gateway" / "control_socket.py"
DISCOVERY = ROOT / "hermes_cli" / "gateway_runtime_discovery.py"
MARKER = "# assistant-stack: control socket in tmp"
TAG = "[patch-control-socket-tmp]"

SERVER_OLD = '''    direct = Path(home) / _SOCKET_FILENAME
    return (direct, None) if _fits_sun_path(direct) else (_fallback_socket_path(home), Path(home) / _POINTER_FILENAME)
'''
SERVER_NEW = f'''    direct = Path(home) / _SOCKET_FILENAME
    if _assistant_socket_in_tmp():  {MARKER}
        return _assistant_tmp_socket_path(home), Path(home) / _POINTER_FILENAME
    return (direct, None) if _fits_sun_path(direct) else (_fallback_socket_path(home), Path(home) / _POINTER_FILENAME)
'''

CLIENT_OLD = '''    direct = Path(home) / _SOCKET_FILENAME
    if direct.exists():
        return direct
'''
CLIENT_NEW = f'''    direct = Path(home) / _SOCKET_FILENAME
    if direct.exists() and not _assistant_socket_in_tmp():  {MARKER}
        return direct
'''

HELPER_ANCHOR = "def resolve_server_socket_path("
HELPER = f'''def _assistant_socket_in_tmp() -> bool:  {MARKER}
    """HERMES_CONTROL_SOCKET_IN_TMP=1: $HERMES_HOME cannot hold a connectable socket."""
    return os.environ.get("HERMES_CONTROL_SOCKET_IN_TMP", "").strip().lower() in ("1", "true", "yes", "on")


def _assistant_tmp_socket_path(home: Path) -> Path:
    """/tmp, not tempfile.gettempdir(): gateways point TMPDIR under $HERMES_HOME (virtiofs)."""
    return Path("/tmp") / f"hermes-gw-{{_home_hash(home)}}" / "control.sock"


'''


DISCOVERY_OLD = '''    direct = home / "gateway.sock"
    if os.path.lexists(direct):
'''
DISCOVERY_NEW = f'''    direct = home / "gateway.sock"
    from gateway.control_socket import _assistant_socket_in_tmp  {MARKER}
    if os.path.lexists(direct) and not _assistant_socket_in_tmp():
'''


def _patch(path: Path, edits: list[tuple[str, str, str]]) -> None:
    text = path.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({path})")
        return
    for old, new, label in edits:
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {path}")


def main() -> None:
    socket_edits = [(SERVER_OLD, SERVER_NEW, "server path"), (CLIENT_OLD, CLIENT_NEW, "client path"),
                    (HELPER_ANCHOR, HELPER + HELPER_ANCHOR, "helper anchor")]
    discovery_edits = [(DISCOVERY_OLD, DISCOVERY_NEW, "discovery direct path")]
    # Check both targets before touching either, so a moved anchor leaves Hermes as shipped.
    for path, edits in ((TARGET, socket_edits), (DISCOVERY, discovery_edits)):
        text = path.read_text()
        if MARKER in text:
            continue
        for old, _, label in edits:
            if text.count(old) != 1:
                raise SystemExit(f"{TAG} expected 1 x {label} in {path}, found {text.count(old)}. "
                                 "Hermes image may have changed — update this patch.")
    _patch(TARGET, socket_edits)
    _patch(DISCOVERY, discovery_edits)


if __name__ == "__main__":
    main()
