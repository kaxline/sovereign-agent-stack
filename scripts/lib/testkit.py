"""Pass/fail helpers for the Python tests in scripts/.

Two styles, with the same output: a failure prints ``FAIL <message>`` to
stderr.

- ``fail`` and ``expect`` stop the test at the first failure (exit 1).
- ``Checks`` collects failures, so one run reports every broken check, then
  ``finish`` exits 1 if any failed.

Failures go to ``sys.__stderr__``, so they reach the terminal while a test
has redirected ``sys.stderr`` to capture output, and stdout is flushed first,
so a log that combines both streams keeps them in order.
Image drivers run inside the Hermes image without scripts/lib, so they keep
their own copies.
"""

from __future__ import annotations

import sys
from typing import NoReturn


def _report(line: str) -> None:
    sys.stdout.flush()
    print(line, file=sys.__stderr__, flush=True)


def fail(msg: object) -> NoReturn:
    _report(f"FAIL {msg}")
    raise SystemExit(1)


def expect(cond: object, msg: object) -> None:
    if not cond:
        fail(msg)


class Checks:
    """Failures collected over a run; ``finish`` reports and exits."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def fail(self, msg: object) -> None:
        self.failures.append(str(msg))
        _report(f"FAIL {msg}")

    def expect(self, cond: object, msg: object) -> bool:
        if not cond:
            self.fail(msg)
        return bool(cond)

    def finish(self, ok_line: str = "ok") -> NoReturn:
        """Exit 1 after a summary when anything failed, else print ok_line and exit 0."""
        if self.failures:
            _report(f"{len(self.failures)} check(s) failed")
            raise SystemExit(1)
        print(ok_line)
        raise SystemExit(0)
