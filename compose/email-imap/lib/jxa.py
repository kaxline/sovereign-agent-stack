"""Run a JXA script against Mail.app and parse its JSON reply. See docs/email.md.

Shared by the reader (mail_script.py) and the drafts helper (mail_drafts.py),
which are kept apart otherwise. Tests pass a different argv to run the same
script under Node against a fake Mail.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[[dict], dict]


class AutomationDenied(PermissionError):
    """macOS refused to let this process control Mail."""


class ScriptError(RuntimeError):
    pass


def command_runner(argv: list[str], timeout: int) -> Runner:
    """Run argv + [request JSON] and parse stdout as JSON, mapping osascript errors."""
    def call(request: dict) -> dict:
        try:
            proc = subprocess.run([*argv, json.dumps(request)],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ScriptError(f"Mail didn't answer `{request['command']}` within {timeout}s") from None
        if proc.returncode != 0:
            # osascript errors end with an Apple event code such as (-1743).
            # Report the code only: the text can echo what the script was handling.
            codes = re.findall(r"\((-?\d+)\)", proc.stderr)
            if "-1743" in codes:
                raise AutomationDenied()
            code = codes[-1] if codes else "unknown"
            raise ScriptError(f"Mail script `{request['command']}` failed (error {code})")
        return json.loads(proc.stdout)
    return call


def osascript_runner(script: Path, timeout: int) -> Runner:
    return command_runner(["osascript", "-l", "JavaScript", str(script)], timeout)
