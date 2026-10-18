#!/usr/bin/env python3
"""Put each gateway's control socket on the container filesystem.

Compatibility overlay (not upstream). Hermes v0.22 binds the gateway control
socket at $HERMES_HOME/gateway.sock, and every other gateway asks it which
profiles it serves before starting. $HERMES_HOME is data/hermes, a Docker
Desktop virtiofs bind mount, where connect() on a Unix socket fails with
EOPNOTSUPP. The answer never comes, so each standalone profile gateway
decides it must replace the default gateway, refuses (it cannot prove that
PID is its own), and s6 restarts it in a loop.

With HERMES_CONTROL_SOCKET_IN_TMP=1 the server binds Hermes' own temp-dir
fallback (/tmp/hermes-gw-<hash>.sock, written to $HERMES_HOME/gateway.sock.path)
and clients follow that pointer, the path Hermes already uses when the home
path is too long for sun_path. Every gateway, the dashboard and `hermes` CLI
calls share the container's /tmp, so they all reach each other.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
TARGET = ROOT / "gateway" / "control_socket.py"
MARKER = "# assistant-stack: control socket in tmp"
TAG = "[patch-control-socket-tmp]"

SERVER_OLD = '''    direct = Path(home) / _SOCKET_FILENAME
    return (direct, None) if _fits_sun_path(direct) else (_fallback_socket_path(home), Path(home) / _POINTER_FILENAME)
'''
SERVER_NEW = f'''    direct = Path(home) / _SOCKET_FILENAME
    if _assistant_socket_in_tmp():  {MARKER}
        return _fallback_socket_path(home), Path(home) / _POINTER_FILENAME
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


'''


def main() -> None:
    text = TARGET.read_text()
    if MARKER in text:
        print(f"{TAG} already applied ({TARGET})")
        return
    for old, label in ((SERVER_OLD, "server path"), (CLIENT_OLD, "client path"), (HELPER_ANCHOR, "helper anchor")):
        if text.count(old) != 1:
            raise SystemExit(f"{TAG} expected 1 x {label} in {TARGET}, found {text.count(old)}. "
                             "Hermes image may have changed — update this patch.")
    text = text.replace(SERVER_OLD, SERVER_NEW).replace(CLIENT_OLD, CLIENT_NEW)
    text = text.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR)
    TARGET.write_text(text)
    print(f"{TAG} patched {TARGET}")


if __name__ == "__main__":
    main()
