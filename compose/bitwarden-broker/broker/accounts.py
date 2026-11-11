"""The accounts the broker signs in to.

Two sources:

- The add-on instance store, ``<data root>/addons/bitwarden/instances/<slug>.json``,
  one JSON object per account as ``scripts/addon.sh`` and ``scripts/vault.sh add``
  write it: ``mode`` (``cloud`` | ``selfhosted``), ``region`` (``us`` | ``eu``,
  cloud only, default ``us``), ``url`` (selfhosted), ``scope`` (default
  ``folder:Agent``) and ``unlock`` (default ``manual``). Unknown keys are
  ignored. The slug is the file name without ``.json`` and follows the store's
  rule; ``local`` is reserved.
- The local account (``make vault-init``), which stays out of the store:
  ``broker/local-account.json`` in the broker's data folder holds
  ``{"scope": "collection:<id>"}``. It is mode ``local`` on the stack's
  Vaultwarden with stored unlock.

The API key's ``client_id`` is ``secrets/<slug>/client_id``, beside
``client_secret`` and ``master_password``. A file that can't be used refuses
that account only, with a cause (``bad_file``, ``bad_slug``,
``missing_client_id``) and a message that names the file and the setting.
Messages never repeat a value from the file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# The add-on store's slug rule (scripts/lib/addon_store.py SLUG_RE).
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
LOCAL_SLUG = "local"
# Bitwarden item, folder and collection ids are GUIDs; this also keeps a flag-like value out of bw's argv.
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
MODES = ("cloud", "selfhosted")
UNLOCKS = ("stored", "manual")
REGIONS = {"us": "https://vault.bitwarden.com", "eu": "https://vault.bitwarden.eu"}
LOCAL_SERVER = "https://vaultwarden.vault.internal"
DEFAULT_SCOPE = "folder:Agent"
DEFAULT_UNLOCK = "manual"
DEFAULT_REGION = "us"
LOCAL_FILE = "local-account.json"
# How a refusal names the local account's file: apart from an instance file of the same name.
LOCAL_LABEL = f"broker/{LOCAL_FILE}"
CAUSES = ("bad_file", "bad_slug", "missing_client_id")


@dataclass(frozen=True)
class Account:
    slug: str
    mode: str  # "cloud" | "selfhosted" | "local"
    server: str
    scope_kind: str  # "folder" | "collection" | "all"
    scope_value: str | None
    unlock: str  # "stored" | "manual"
    client_id: str
    path: Path


class AccountError(ValueError):
    """An account that is refused. ``cause`` is one of CAUSES; ``file`` the file name."""

    def __init__(self, file: str, cause: str, message: str):
        super().__init__(f"{file}: {message}")
        self.file = file
        self.cause = cause


@dataclass(frozen=True)
class Refusal:
    file: str
    cause: str
    message: str


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


def _server(value: str) -> str | None:
    """An https URL with a host and no user, query or fragment; trailing slashes dropped."""
    server = value.rstrip("/")
    try:
        parts = urlsplit(server)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment or "?" in server or "#" in server:
        return None
    if any(c.isspace() for c in server):
        return None
    return server


def _read_json(path: Path, name: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise AccountError(name, "bad_file", "cannot be read as UTF-8 text") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise AccountError(name, "bad_file", "is not JSON") from None
    if not isinstance(data, dict):
        raise AccountError(name, "bad_file", "is not a JSON object")
    return data


def _text(data: dict, name: str, key: str, default: str | None = None) -> str | None:
    value = data.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountError(name, "bad_file", f"{key}: must be a string")
    return value


def read_client_id(secrets_dir: Path, slug: str, name: str) -> str:
    """``secrets/<slug>/client_id`` without surrounding whitespace."""
    try:
        value = (Path(secrets_dir) / slug / "client_id").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        value = ""
    if not value:
        raise AccountError(name, "missing_client_id",
                           f"client_id: secrets/{slug}/client_id is missing; connect the account again")
    if any(c.isspace() for c in value):
        raise AccountError(name, "bad_file", f"client_id: secrets/{slug}/client_id must not contain whitespace")
    return value


def load_instance(path: Path, secrets_dir: Path) -> Account:
    """One ``instances/<slug>.json``. Raises AccountError."""
    path = Path(path)
    name = path.name
    slug = name[: -len(".json")] if name.endswith(".json") else ""
    if not SLUG_RE.match(slug):
        raise AccountError(name, "bad_slug", "the file name must be <slug>.json, the slug a lowercase letter "
                           "then up to 30 of a-z, 0-9 and -")
    if slug == LOCAL_SLUG:
        raise AccountError(name, "bad_slug", "the slug local is reserved for make vault-init")
    data = _read_json(path, name)

    mode = _text(data, name, "mode")
    if mode not in MODES:
        raise AccountError(name, "bad_file", f"mode: must be one of {', '.join(MODES)}")
    if mode == "cloud":
        region = _text(data, name, "region", DEFAULT_REGION) or DEFAULT_REGION
        if region not in REGIONS:
            raise AccountError(name, "bad_file", f"region: must be one of {', '.join(REGIONS)}")
        server = REGIONS[region]
    else:
        url = _text(data, name, "url")
        server = _server(url) if url else None
        if server is None:
            raise AccountError(name, "bad_file", "url: a selfhosted account needs an https:// URL with no user, "
                               "query or fragment")
    scope = _scope(_text(data, name, "scope", DEFAULT_SCOPE) or DEFAULT_SCOPE, allow_all=True)
    if scope is None:
        raise AccountError(name, "bad_file", "scope: must be folder:<name>, collection:<id> or all")
    unlock = _text(data, name, "unlock", DEFAULT_UNLOCK) or DEFAULT_UNLOCK
    if unlock not in UNLOCKS:
        raise AccountError(name, "bad_file", f"unlock: must be one of {', '.join(UNLOCKS)}")
    client_id = read_client_id(secrets_dir, slug, name)
    return Account(slug=slug, mode=mode, server=server, scope_kind=scope[0], scope_value=scope[1],
                   unlock=unlock, client_id=client_id, path=path)


def load_local(broker_dir: Path, secrets_dir: Path) -> Account | None:
    """The local account from ``broker/local-account.json``; None when the file is absent."""
    path = Path(broker_dir) / LOCAL_FILE
    if not path.exists():
        return None
    data = _read_json(path, LOCAL_LABEL)
    raw = _text(data, LOCAL_LABEL, "scope")
    scope = _scope(raw, allow_all=False) if raw else None
    if scope is None or scope[0] != "collection":
        raise AccountError(LOCAL_LABEL, "bad_file", "scope: must be collection:<id>")
    client_id = read_client_id(secrets_dir, LOCAL_SLUG, LOCAL_LABEL)
    return Account(slug=LOCAL_SLUG, mode="local", server=LOCAL_SERVER, scope_kind=scope[0],
                   scope_value=scope[1], unlock="stored", client_id=client_id, path=path)


def load_account(slug: str, instances_dir: Path, broker_dir: Path, secrets_dir: Path) -> Account:
    """The account SLUG from either source, for bitwarden-setup. Raises AccountError."""
    if slug == LOCAL_SLUG:
        account = load_local(broker_dir, secrets_dir)
        if account is None:
            raise AccountError(LOCAL_LABEL, "bad_file", "missing; make vault-init writes it")
        return account
    if not SLUG_RE.match(slug or ""):
        raise AccountError(f"{slug}.json", "bad_slug", "not a valid slug")
    path = Path(instances_dir) / f"{slug}.json"
    if not path.is_file():
        raise AccountError(path.name, "bad_file", "no such instance; connect the account first")
    return load_instance(path, secrets_dir)


def load_all(instances_dir: Path, broker_dir: Path, secrets_dir: Path) -> tuple[list[Account], list[Refusal]]:
    """Every instance in ``instances_dir`` plus the local account. A refused one adds a Refusal and is skipped."""
    loaded: list[Account] = []
    refused: list[Refusal] = []
    instances_dir = Path(instances_dir)
    files: list[Path] = []
    if instances_dir.is_dir():
        try:
            files = sorted(instances_dir / n for n in os.listdir(instances_dir)
                           if n.endswith(".json") and not n.startswith("."))
        except OSError:
            # The container runs as HERMES_UID, which must own the store's 0700 instances/.
            refused.append(Refusal("instances/", "bad_file", "instances/: cannot be read; the broker runs as "
                                   "HERMES_UID:HERMES_GID, which must own the add-on store"))
    for file in files:
        if not file.is_file():
            continue
        try:
            loaded.append(load_instance(file, secrets_dir))
        except AccountError as exc:
            refused.append(Refusal(exc.file, exc.cause, str(exc)))
    try:
        local = load_local(broker_dir, secrets_dir)
    except AccountError as exc:
        refused.append(Refusal(exc.file, exc.cause, str(exc)))
    else:
        if local is not None:
            loaded.append(local)
    loaded.sort(key=lambda a: a.slug)
    return loaded, refused
