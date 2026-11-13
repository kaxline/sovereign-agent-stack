"""Run the signer: ``python -m signer``.

Reads BITWARDEN_SIGNIN_SOCKET, BITWARDEN_SIGNIN_IDLE_MINUTES and
BITWARDEN_SIGNIN_ACTION_SECONDS, exits 2 on an invalid value, probes the
Chromium sandbox, and serves until SIGTERM or SIGINT. On the way out it ends
any open session (killing its Chromium and deleting the profile) and removes
the socket.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from . import server


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = server.load_config(os.environ)
    except server.ConfigError as exc:
        print(f"bitwarden-signin: {exc}", file=sys.stderr)
        return 2
    signer = server.Signer(config)
    try:
        signer.listen()
    except (server.ConfigError, OSError) as exc:
        print(f"bitwarden-signin: cannot listen on {config.socket_path} ({type(exc).__name__})", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=signer.serve, name="signer-http", daemon=True).start()
    logging.getLogger("signer").info("listening on %s", config.socket_path)
    stop.wait()
    signer.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
