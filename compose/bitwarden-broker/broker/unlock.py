"""Unlock or lock a ``manual`` account in the running broker, over its Unix socket.

``python3 -m broker.unlock <slug>`` reads the master password from stdin (one
line, without its trailing newline) and sends it in a ``POST /unlock`` body.
``python3 -m broker.unlock --lock <slug>`` sends ``POST /lock``. The socket is
``BITWARDEN_SYNC_SOCKET``; the TCP API has neither call. The password never
goes into argv, and every message printed here is fixed text.

Exit codes: 0 done, 1 the broker is unreachable or the unlock failed for
another reason, 2 wrong master password, 3 unknown slug or not a manual account.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys

DEFAULT_SOCKET = "/run/bitwarden-sync/sync.sock"
TIMEOUT_SECONDS = 180  # bw unlock plus the first listing


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a Unix stream socket."""

    def __init__(self, path: str, timeout: float = TIMEOUT_SECONDS):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _post(path: str, body: dict) -> int | None:
    """The response status, or None when the broker cannot be reached."""
    data = json.dumps(body).encode("utf-8")
    body.clear()
    conn = UnixHTTPConnection(os.environ.get("BITWARDEN_SYNC_SOCKET") or DEFAULT_SOCKET)
    try:
        conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        data = None
        conn.close()


def _read_password() -> str:
    line = sys.stdin.readline()
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def main(argv: list[str]) -> int:
    lock = argv[:1] == ["--lock"]
    args = argv[1:] if lock else argv
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: python3 -m broker.unlock <slug> | --lock <slug>", file=sys.stderr)
        return 1
    slug = args[0]
    if lock:
        status = _post("/lock", {"slug": slug})
    else:
        password = _read_password()
        if not password:
            print("No master password on stdin.", file=sys.stderr)
            return 2
        status = _post("/unlock", {"slug": slug, "password": password})
        password = None
    if status is None:
        print("The Bitwarden broker is not reachable.", file=sys.stderr)
        return 1
    if status == 200:
        print("Locked." if lock else "Unlocked.")
        return 0
    if status == 401:
        print("Wrong master password.", file=sys.stderr)
        return 2
    if status == 404:
        print("No manual account with that slug.", file=sys.stderr)
        return 3
    print("The broker could not lock the account." if lock else "The broker could not unlock the account.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
