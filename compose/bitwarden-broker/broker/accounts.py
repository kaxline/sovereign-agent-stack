"""Account files: ``compose/bitwarden/accounts/<slug>.env``, one per Bitwarden account.

Each file holds ``KEY=value`` lines. People edit these by hand, so the parser
accepts CRLF line endings, whitespace around keys and values, blank lines,
``#`` comment lines, a comment after an unquoted value when whitespace precedes
the ``#``, and one pair of matching single or double quotes around a value. A
quoted value keeps its ``#`` characters. Anything else (an unknown key, an
unknown value, a repeated key, a missing required key, a bad slug) refuses the
whole file with an error naming the file and the key. Error text never repeats
a value from the file.

Credentials are not in these files; bwcli reads them from ``secrets/<slug>/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SLUG_RE = re.compile(r"[a-z0-9-]+")
# Bitwarden item, folder and collection ids are GUIDs; this also keeps a flag-like value out of bw's argv.
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
MODES = ("cloud", "selfhosted", "local")
UNLOCKS = ("stored", "manual")
CLOUD_SERVERS = ("https://vault.bitwarden.com", "https://vault.bitwarden.eu")
LOCAL_SERVER = "https://vaultwarden.vault.internal"
DEFAULT_SCOPE = "folder:Agent"
KEYS = ("BW_MODE", "BW_SERVER", "BW_SCOPE", "BW_UNLOCK", "BW_CLIENTID")
# Read by an earlier version (the stack secrets sync, since removed). Files that still set it load;
# the value is not checked or used.
IGNORED_KEYS = ("BW_SECRETS_ITEM_SCOPE",)
REQUIRED = ("BW_MODE", "BW_SERVER", "BW_UNLOCK", "BW_CLIENTID")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


@dataclass(frozen=True)
class Account:
    slug: str
    mode: str
    server: str
    scope_kind: str  # "folder" | "collection" | "all"
    scope_value: str | None
    unlock: str  # "stored" | "manual"
    client_id: str
    path: Path


class AccountError(ValueError):
    pass


def _value(raw: str) -> str | None:
    """Unquote or strip a trailing comment from a raw value; None if the quoting is bad."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end < 0:
            return None
        rest = raw[end + 1:].strip()
        if rest and not rest.startswith("#"):
            return None
        return raw[1:end]
    if raw.startswith("#"):
        return ""
    return _INLINE_COMMENT.sub("", raw)


def _scope(value: str, allow_all: bool) -> tuple[str, str | None] | None:
    if value == "all":
        return ("all", None) if allow_all else None
    kind, sep, rest = value.partition(":")
    rest = rest.strip()
    if not sep or not rest:
        return None
    if kind == "folder" or (kind == "collection" and ID_RE.fullmatch(rest)):
        return kind, rest
    return None


def _server(mode: str, value: str) -> str | None:
    server = value.rstrip("/")
    try:
        parts = urlsplit(server)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment or "?" in server or "#" in server:
        return None
    if mode == "cloud" and server not in CLOUD_SERVERS:
        return None
    if mode == "local" and server != LOCAL_SERVER:
        return None
    return server


def parse_file(path: Path) -> Account:
    name = path.name
    slug = name[: -len(".env")] if name.endswith(".env") else ""
    if not SLUG_RE.fullmatch(slug):
        raise AccountError(f"{name}: slug: the file name must be <slug>.env with slug of a-z, 0-9 and -")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise AccountError(f"{name}: cannot be read as UTF-8 text") from None

    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep:
            raise AccountError(f"{name}: line {number}: expected KEY=value")
        if key not in KEYS and key not in IGNORED_KEYS:
            raise AccountError(f"{name}: {key}: unknown key")
        if key in values:
            raise AccountError(f"{name}: {key}: set more than once")
        value = _value(raw)
        if value is None:
            raise AccountError(f"{name}: {key}: unmatched quote")
        values[key] = value

    for key in REQUIRED:
        if not values.get(key):
            raise AccountError(f"{name}: {key}: required")

    mode = values["BW_MODE"]
    if mode not in MODES:
        raise AccountError(f"{name}: BW_MODE: must be one of {', '.join(MODES)}")
    server = _server(mode, values["BW_SERVER"])
    if server is None:
        allowed = {"cloud": " ".join(CLOUD_SERVERS), "local": LOCAL_SERVER}.get(mode, "an https:// URL")
        raise AccountError(f"{name}: BW_SERVER: for mode {mode} must be {allowed}, with no query or fragment")
    scope = _scope(values.get("BW_SCOPE") or DEFAULT_SCOPE, allow_all=True)
    if scope is None:
        raise AccountError(f"{name}: BW_SCOPE: must be folder:<name>, collection:<id> or all")
    unlock = values["BW_UNLOCK"]
    if unlock not in UNLOCKS:
        raise AccountError(f"{name}: BW_UNLOCK: must be one of {', '.join(UNLOCKS)}")
    client_id = values["BW_CLIENTID"]
    if any(c.isspace() for c in client_id):
        raise AccountError(f"{name}: BW_CLIENTID: must not contain whitespace")

    return Account(
        slug=slug, mode=mode, server=server, scope_kind=scope[0], scope_value=scope[1], unlock=unlock,
        client_id=client_id, path=path,
    )


def load_dir(path: Path) -> tuple[list[Account], list[str]]:
    """Load every ``*.env`` file in ``path``; a refused file adds one error and skips that account."""
    path = Path(path)
    if not path.is_dir():
        return [], []
    loaded: list[Account] = []
    errors: list[str] = []
    for file in sorted(path.glob("*.env")):
        if not file.is_file():
            continue
        try:
            loaded.append(parse_file(file))
        except AccountError as exc:
            errors.append(str(exc))
    return loaded, errors
