"""Passwords for IMAP accounts. See docs/email.md (IMAP source).

Installed as an app, the mail helpers are started by a launcher that reads
the passwords from the Keychain (service `<bundle prefix>.mail-imap`, account
= the config's account id) and writes them as one JSON object to a pipe. The
launcher puts the pipe's file descriptor in MAIL_CREDENTIALS_FD. This module
reads the pipe once, the first time a password is needed, and keeps the
passwords in memory only.

Run from a terminal there's no launcher, so an account names an environment
variable instead (`password_env`). Passwords never come from the config file
and are never printed.
"""

from __future__ import annotations

import json
import os

FD_VARIABLE = "MAIL_CREDENTIALS_FD"
_from_launcher: dict[str, str] | None = None


class MissingPassword(RuntimeError):
    pass


def _launcher_passwords() -> dict[str, str]:
    global _from_launcher
    if _from_launcher is None:
        _from_launcher = {}
        fd = os.environ.pop(FD_VARIABLE, None)
        if fd and fd.isdigit():
            chunks = []
            try:
                while chunk := os.read(int(fd), 65536):
                    chunks.append(chunk)
                os.close(int(fd))
            except OSError:
                chunks = []
            try:
                data = json.loads(b"".join(chunks) or b"{}")
            except ValueError:
                data = {}
            if isinstance(data, dict):
                _from_launcher = {k.upper(): v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    return _from_launcher


def password(account_id: str, password_env: str | None) -> str:
    if password_env:
        value = os.environ.get(password_env)
        if not value:
            raise MissingPassword(f"{account_id}: set {password_env} to the account's password for this run")
        return value
    value = _launcher_passwords().get(account_id.upper())
    if not value:
        raise MissingPassword(
            f"{account_id}: no password. Add it to the Keychain with"
            f" `macos/mail-apps/install.sh password {account_id}` and run the installed app,"
            " or set `password_env` for terminal runs")
    return value
