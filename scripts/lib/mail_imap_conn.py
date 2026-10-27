"""What the IMAP reader and the IMAP drafts outbox share. See docs/email.md.

Account settings, connecting and logging in, modified UTF-7 mailbox names,
LIST and FETCH parsing. It sends no command of its own beyond STARTTLS,
LOGIN or AUTHENTICATE PLAIN, LIST and LOGOUT: the reader (mail_imap.ReadOnlyImap) and the outbox
(mail_imap_drafts.DraftsImap) each add only the commands they need. The
reader and drafts apps each bundle this file and their own module, never the
other's.

The password comes from the Keychain through the mail apps' launcher, or, for
terminal runs, from the environment variable the account's `password_env`
names (see mail_credentials.py). It never comes from the config file.
"""

from __future__ import annotations

import base64
import imaplib
import ipaddress
import os
import re
import socket
import ssl
import tomllib
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Iterator

import mail_credentials

SECURITY = ("tls", "starttls", "plain")
DEFAULT_PORT = {"tls": 993, "starttls": 143, "plain": 143}
ACCOUNT_KEYS = {"id", "mailboxes", "source", "host", "port", "security", "user", "address", "addresses",
                "password_env", "tls_ca", "timeout", "drafts_mailbox"}
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


class ConfigError(ValueError):
    pass


class ImapError(RuntimeError):
    """The server refused something or the connection failed."""


class AuthFailed(ImapError):
    """The server rejected the user name and password."""


class Refused(RuntimeError):
    """This code asked for something it must never do. Always a bug."""


# --------------------------------------------------------------------------- config

def is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _address(value, where: str) -> str:
    _, addr = parseaddr(value if isinstance(value, str) else "")
    if not addr or "@" not in addr or addr != value.strip():
        raise ConfigError(f"{where}: {value!r} isn't a plain name@domain address")
    return addr


def _path(value, where: str, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a path")
    p = Path(os.path.expandvars(value)).expanduser()
    return (base / p).resolve() if not p.is_absolute() else p.resolve()


def account_settings(item: dict, base: Path, *, where: str) -> dict:
    """The IMAP keys of one [[accounts]] entry, checked. Raises ConfigError."""
    unknown = set(item) - ACCOUNT_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys for an imap account: {', '.join(sorted(unknown))}")
    host = item.get("host")
    if not isinstance(host, str) or not host.strip() or any(c.isspace() for c in host.strip()):
        raise ConfigError(f"{where}: an imap account needs `host`")
    host = host.strip()
    security = item.get("security", "tls")
    if security not in SECURITY:
        raise ConfigError(f"{where}: `security` must be \"tls\" (the default) or \"starttls\"")
    if security == "plain" and not is_loopback(host):
        raise ConfigError(f"{where}: security = \"plain\" is only allowed for a server on this machine"
                          " (localhost); use \"tls\" or \"starttls\"")
    port = item.get("port", DEFAULT_PORT[security])
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ConfigError(f"{where}: `port` must be a port number")
    user = item.get("user")
    if not isinstance(user, str) or not user.strip():
        raise ConfigError(f"{where}: an imap account needs `user`")
    addresses = []
    if "address" in item:
        addresses.append(_address(item["address"], f"{where}.address"))
    extra = item.get("addresses", [])
    if not isinstance(extra, list):
        raise ConfigError(f"{where}: `addresses` must be a list")
    addresses += [_address(a, f"{where}.addresses") for a in extra]
    password_env = item.get("password_env")
    if password_env is not None and (not isinstance(password_env, str) or not _ENV_NAME.fullmatch(password_env)):
        raise ConfigError(f"{where}: `password_env` must be an environment variable name like WORK_IMAP_PASSWORD")
    tls_ca = _path(item["tls_ca"], f"{where}.tls_ca", base) if "tls_ca" in item else None
    timeout = item.get("timeout", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"{where}: `timeout` must be a positive number of seconds")
    drafts_mailbox = item.get("drafts_mailbox")
    if drafts_mailbox is not None and (not isinstance(drafts_mailbox, str) or not drafts_mailbox.strip()):
        raise ConfigError(f"{where}: `drafts_mailbox` must be a mailbox path like \"Drafts\"")
    return {"host": host, "port": port, "security": security, "user": user.strip(),
            "addresses": list(dict.fromkeys(a.lower() for a in addresses)),
            "password_env": password_env, "tls_ca": str(tls_ca) if tls_ca else None, "timeout": timeout,
            "drafts_mailbox": drafts_mailbox}


def imap_accounts(config_path: Path) -> dict[str, tuple[str, dict]]:
    """{ID: (id, settings)} for the config's imap accounts, without the rest
    of the indexer config. For the drafts helper, which has no index."""
    path = Path(config_path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config is not valid TOML: {exc}") from None
    default = raw.get("source", "script")
    out = {}
    for i, item in enumerate(raw.get("accounts") or []):
        if not isinstance(item, dict) or item.get("source", default) != "imap":
            continue
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ConfigError(f"accounts[{i}] needs an `id`")
        out[item["id"].strip().upper()] = (item["id"].strip(),
                                           account_settings(item, path.resolve().parent, where=f"accounts[{i}]"))
    return out


# --------------------------------------------------------------------------- mailbox names

def decode_mutf7(name: str) -> str:
    """IMAP's modified UTF-7 (RFC 3501 5.1.3) to text."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "&":
            end = name.find("-", i)
            if end == -1:
                raise ValueError("unterminated shift in mailbox name")
            chunk = name[i + 1:end]
            if not chunk:
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = end + 1
        else:
            out.append(name[i])
            i += 1
    return "".join(out)


def encode_mutf7(text: str) -> str:
    out, pending = [], []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def quote(name: str) -> str:
    """A mailbox name as an IMAP quoted string."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Mailbox:
    path: str           # "/"-separated, decoded
    raw: str            # the server's encoded name
    flags: set[str]     # lowercased, e.g. {"\\hasnochildren", "\\junk"}

    @property
    def selectable(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.flags)

    @property
    def special(self) -> set[str]:
        return self.flags & {"\\junk", "\\trash", "\\all", "\\flagged", "\\drafts", "\\sent", "\\archive"}


_LIST = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.*)', re.S)


def _unquote(raw: bytes) -> bytes:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        return re.sub(rb"\\(.)", rb"\1", raw[1:-1])
    return raw


def parse_list(data: list) -> list[Mailbox]:
    """Mailboxes from imaplib's LIST response, with literal names joined back up."""
    boxes = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):  # {n} literal: (b'(\\Flags) "/" {n}', b'name')
            m = _LIST.match(item[0])
            name_raw = item[1]
        else:
            m = _LIST.match(item)
            name_raw = _unquote(m.group("name")) if m else b""
        if not m:
            continue
        delim = _unquote(m.group("delim")).decode() if m.group("delim") != b"NIL" else ""
        raw = name_raw.decode("ascii", "replace")
        try:
            decoded = decode_mutf7(raw)
        except (ValueError, UnicodeDecodeError):
            decoded = raw
        path = "/".join(decoded.split(delim)) if delim and delim != "/" else decoded
        flags = {f.lower() for f in m.group("flags").decode("ascii", "replace").split()}
        boxes.append(Mailbox(path=path, raw=raw, flags=flags))
    return boxes


def fetch_items(data: list) -> Iterator[tuple[bytes, bytes | None]]:
    """(metadata, literal) pairs from a FETCH response. Items without a body
    literal come back as plain bytes."""
    for item in data:
        if isinstance(item, tuple):
            yield item[0], item[1]
        elif isinstance(item, bytes) and re.match(rb"\d+ \(", item):
            yield item, None


def uid_of(meta: bytes) -> int | None:
    m = re.search(rb"\bUID (\d+)", meta)
    return int(m.group(1)) if m else None


def flags_of(meta: bytes) -> set[str]:
    m = re.search(rb"FLAGS \(([^)]*)\)", meta)
    return {f.lower() for f in m.group(1).decode("ascii", "replace").split()} if m else set()


# --------------------------------------------------------------------------- connection

class Connection:
    """Connects and logs in. Subclasses add the commands they're allowed."""

    def __init__(self, account_id: str, settings: dict) -> None:
        self.account_id = account_id
        self.settings = settings
        self.capabilities: set[str] = set()
        self._imap: imaplib.IMAP4 | None = None

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.settings.get("tls_ca"))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def open(self) -> "Connection":
        s = self.settings
        try:
            if s["security"] == "tls":
                imap = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=self._context(), timeout=s["timeout"])
            else:
                imap = imaplib.IMAP4(s["host"], s["port"], timeout=s["timeout"])
                if s["security"] == "starttls":
                    imap.starttls(ssl_context=self._context())
        except (OSError, socket.timeout, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise ImapError(f"{self.account_id}: can't connect to {s['host']}:{s['port']}"
                            f" ({type(exc).__name__}: {exc})") from None
        try:
            password = mail_credentials.password(self.account_id, s.get("password_env"))
        except mail_credentials.MissingPassword:
            self._shutdown(imap)
            raise
        try:
            self._login(imap, s["user"], password)
        except AuthFailed:
            self._shutdown(imap)
            raise
        except imaplib.IMAP4.error:
            self._shutdown(imap)
            raise AuthFailed(f"{self.account_id}: the server rejected the user name or password") from None
        finally:
            del password
        self._imap = imap
        self.capabilities = {c.upper() for c in imap.capabilities}
        return self

    def _login(self, imap: imaplib.IMAP4, user: str, password: str) -> None:
        """AUTHENTICATE PLAIN when the server offers it (it carries UTF-8),
        else LOGIN. Never both: a second try after a refusal counts as
        another failed login. Raises AuthFailed or imaplib.IMAP4.error."""
        caps = {c.upper() for c in imap.capabilities}
        if "AUTH=PLAIN" in caps:
            token = b"\0" + user.encode() + b"\0" + password.encode()
            imap.authenticate("PLAIN", lambda _challenge: token)
            return
        if "LOGINDISABLED" in caps:
            raise AuthFailed(f"{self.account_id}: the server accepts neither LOGIN nor AUTHENTICATE PLAIN"
                             " on this connection")
        try:
            imap.login(user, password)
        except UnicodeEncodeError:
            raise AuthFailed(f"{self.account_id}: the user name or password has non-ASCII characters,"
                             " which LOGIN can't carry, and the server doesn't offer AUTHENTICATE PLAIN") from None

    @staticmethod
    def _shutdown(imap: imaplib.IMAP4) -> None:
        try:
            imap.shutdown()
        except OSError:
            pass

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
            self._imap = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _check(self, typ: str, data, what: str):
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data and isinstance(data[0], bytes) else ""
            raise ImapError(f"{self.account_id}: {what} failed: {detail[:200]}")
        return data

    def list_mailboxes(self) -> list[Mailbox]:
        return parse_list(self._check(*self._imap.list(), "LIST"))
