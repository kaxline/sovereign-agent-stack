#!/usr/bin/env python3
"""Print the ids of cron jobs whose last_delivery_error is an api_server error.

Usage: cron_delivery_errors.py JOBS_JSON

Reads only id/name, last_delivery_error from each job (a list, or an object
with a "jobs" list). Exit 0 and print matching ids, one per line; a missing
or unparsable file prints nothing.
"""

from __future__ import annotations

import json
import sys


def failing_jobs(data) -> list[str]:
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    out = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict):
            continue
        err = job.get("last_delivery_error")
        if isinstance(err, str) and ("api_server" in err or "not send()" in err):
            out.append(str(job.get("id") or job.get("name") or "?"))
    return out


def main() -> int:
    try:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, IndexError):
        return 0
    print("\n".join(failing_jobs(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
