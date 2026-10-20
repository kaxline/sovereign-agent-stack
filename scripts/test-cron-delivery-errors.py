#!/usr/bin/env python3
"""Synthetic jobs.json fixtures for scripts/lib/cron_delivery_errors.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "lib" / "cron_delivery_errors.py"


def run(path: Path) -> list[str]:
    res = subprocess.run([sys.executable, "-I", str(HELPER), str(path)],
                         capture_output=True, text=True, check=True)
    return res.stdout.split()


def job(id_, err):
    return {"id": id_, "prompt": "mentions api_server and not send()",
            "last_status": "ok", "last_delivery_error": err}


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "jobs.json"
        cases = [
            ("null error", {"jobs": [job("a", None), job("b", None)]}, []),
            ("api_server error", {"jobs": [job("a", None),
                                           job("b", "api_server has no send()")]}, ["b"]),
            ("not send() error, bare list", [job("c", "adapter does not send() here")], ["c"]),
            ("unrelated error", {"jobs": [job("a", "timeout")]}, []),
            ("key absent", {"jobs": [{"id": "a", "prompt": "api_server"}]}, []),
        ]
        failed = 0
        for name, data, want in cases:
            p.write_text(json.dumps(data))
            got = run(p)
            if got != want:
                print(f"FAIL {name}: got {got}, want {want}")
                failed += 1
        if run(Path(d) / "missing.json") != []:
            print("FAIL missing file")
            failed += 1
        p.write_text("{not json")
        if run(p) != []:
            print("FAIL invalid json")
            failed += 1
    print("ok" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
