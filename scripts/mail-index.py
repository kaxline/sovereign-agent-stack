#!/usr/bin/env python3
"""Host entry point for compose/email-imap/mail-index.py, which holds the code
and its documentation. The email-imap image runs the same file."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[1] / "compose" / "email-imap" / "mail-index.py"),
               run_name="__main__")
