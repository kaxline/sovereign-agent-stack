"""The broker's TCP API for scripts/vault.sh, which runs this through ``docker compose exec``.

    python3 -m broker.cli accounts
    python3 -m broker.cli requests [--json | --tsv]
    python3 -m broker.cli sync

The API listens on ``BITWARDEN_LISTEN`` (127.0.0.1:8099) inside hermes' network
namespace, so the host reaches it only through the broker container.

``accounts`` prints one tab-separated line per account (slug, mode, scope kind,
state, error class or ``-``), then ``refused<TAB><message>`` for each account
file the broker refused at start. ``requests`` prints the pending access
requests for a person; ``--json`` prints ``GET /requests`` as the broker sent
it, and ``--tsv`` one line per pending request (id, account or ``-``, site,
reason). ``sync`` runs ``POST /sync``.

Exit codes: 0 done, 1 the broker is not reachable, 2 bad usage or the broker
refused the call.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time

DEFAULT_LISTEN = "127.0.0.1:8099"
TIMEOUT_SECONDS = 120  # POST /sync runs bw sync for every account


class Unreachable(Exception):
    pass


class Refused(Exception):
    pass


def _call(method: str, path: str):
    host, _, port = (os.environ.get("BITWARDEN_LISTEN") or DEFAULT_LISTEN).rpartition(":")
    conn = http.client.HTTPConnection(host or "127.0.0.1", int(port), timeout=TIMEOUT_SECONDS)
    try:
        body = b"{}" if method == "POST" else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        raise Unreachable from None
    finally:
        conn.close()
    if resp.status != 200:
        raise Refused(f"{method} {path} answered HTTP {resp.status}")
    try:
        return json.loads(text)
    except ValueError:
        raise Refused(f"{method} {path} did not answer JSON") from None


def _cell(value) -> str:
    text = "-" if value in (None, "") else str(value)
    return "".join(" " if c in "\t\r\n" else c for c in text)


def _age(created) -> str:
    if not isinstance(created, (int, float)):
        return "?"
    minutes = max(0, int(time.time() - created) // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def cmd_accounts() -> None:
    for a in _call("GET", "/accounts"):
        row = [_cell(a.get(k)) for k in ("slug", "mode", "scope", "state", "error_class")]
        row.append("1" if a.get("logged_in") else "0")
        row.append("" if a.get("retry_after") is None else str(a["retry_after"]))
        print("\t".join(row))
    for error in _call("GET", "/health").get("account_errors") or []:
        print(f"refused\t{_cell(error)}")


def cmd_requests(form: str | None) -> None:
    data = _call("GET", "/requests")
    pending = data.get("pending") or []
    if form == "--json":
        print(json.dumps(data))
        return
    if form == "--tsv":
        for r in pending:
            print("\t".join(_cell(r.get(k)) for k in ("request_id", "account", "site", "reason")))
        return
    unreturned = data.get("unreturned") or []
    if not pending:
        print("No pending access requests.")
    else:
        print("Pending access requests:")
        for number, r in enumerate(pending, 1):
            account = f"account {r['account']}, " if r.get("account") else ""
            print(f"  {number}. {_cell(r.get('site'))}  ({account}{_age(r.get('created'))})")
            if r.get("reason"):
                print(f"     Reason, in the agent's text: {_cell(r.get('reason'))}")
    if unreturned:
        print("Released logins Bitwarden would not move out of the agent's scope (move them yourself):")
        for r in unreturned:
            print(f"  - {_cell(r.get('label'))}  ({_cell(r.get('site'))}, account {_cell(r.get('account'))})")


def cmd_sync() -> None:
    reply = _call("POST", "/sync")
    if reply.get("reason") == "busy":
        print("A sync is already running; try again when it ends.")
        return
    synced = reply.get("synced") or []
    if reply.get("reason") == "recent":
        print(f"The last sync ended under 30 s ago; its result: {', '.join(synced) or 'no account synced'}.")
        return
    print(f"Synced: {', '.join(synced)}" if synced else "No account synced (all locked or failing).")
    if reply.get("busy"):
        print(f"Already syncing: {', '.join(reply['busy'])}")


def main(argv: list[str]) -> int:
    try:
        if argv == ["accounts"]:
            cmd_accounts()
        elif argv[:1] == ["requests"] and len(argv) <= 2 and argv[1:] in ([], ["--json"], ["--tsv"]):
            cmd_requests(argv[1] if len(argv) == 2 else None)
        elif argv == ["sync"]:
            cmd_sync()
        else:
            print("usage: python3 -m broker.cli accounts | requests [--json | --tsv] | sync", file=sys.stderr)
            return 2
    except Unreachable:
        print("The Bitwarden broker is not reachable.", file=sys.stderr)
        return 1
    except Refused as exc:
        print(f"The Bitwarden broker refused the call: {exc}.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
