#!/usr/bin/env python3
"""Forward container localhost:PORT to host.docker.internal:PORT.

Buzz communities are keyed by URL host. Hermes must keep BUZZ_RELAY_URL as
ws://localhost:3000 (same community as Desktop), but Docker's localhost is not
the host relay — so we proxy loopback to host.docker.internal.
"""
from __future__ import annotations

import os
import select
import socket
import threading

LISTEN_HOST = os.environ.get("BUZZ_LOCAL_PROXY_LISTEN", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BUZZ_LOCAL_PROXY_PORT", "3000"))
UPSTREAM_HOST = os.environ.get("BUZZ_LOCAL_PROXY_UPSTREAM", "host.docker.internal")
UPSTREAM_PORT = int(os.environ.get("BUZZ_LOCAL_PROXY_UPSTREAM_PORT", str(LISTEN_PORT)))


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            r, _, _ = select.select([src], [], [], 60.0)
            if not r:
                continue
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket) -> None:
    upstream = None
    try:
        upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=5)
        t1 = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass
        if upstream is not None:
            try:
                upstream.close()
            except OSError:
                pass


def main() -> None:
    # Fail fast if upstream is unreachable — cont-init can skip starting us.
    try:
        probe = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=2)
        probe.close()
    except OSError as exc:
        raise SystemExit(f"upstream {UPSTREAM_HOST}:{UPSTREAM_PORT} unreachable: {exc}") from exc

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    print(
        f"buzz-localhost-proxy listening on {LISTEN_HOST}:{LISTEN_PORT} "
        f"→ {UPSTREAM_HOST}:{UPSTREAM_PORT}",
        flush=True,
    )
    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
