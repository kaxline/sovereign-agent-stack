"""Write mapped Bitwarden items into the stack's secret files (bitwarden-secrets-sync).

Reads ``GET /secrets`` from the running broker over ``BITWARDEN_SYNC_SOCKET``
and maps each item by name:

=========================================  ==============================================
``twenty/<slug>/key-ro``, ``.../key-rw``    ``TWENTY_KEY_RO`` / ``TWENTY_KEY_RW`` in
                                            ``compose/twenty/workspaces/<slug>.env``
``twenty/encryption-key``                   the file ``TWENTY_ENCRYPTION_KEY_FILE`` names
``twenty/db-password``                      the file ``TWENTY_DB_PASSWORD_FILE`` names
``postgres/<role>``                         ``DB_SERVICE_PASSWORD`` in
                                            ``compose/postgres/services/<role>.env``
``caldav/<slug>``                           ``CALDAV_PASSWORD`` in
                                            ``compose/caldav-mcp/accounts/<slug>.env``
=========================================  ==============================================

The target folders are mounted under ``BITWARDEN_SYNC_TARGETS`` (default
``/targets``). The two Twenty files must sit in ``TWENTY_SECRETS_DIR``, which
compose mounts at ``<targets>/twenty-secrets``; the ``*_FILE`` variables carry
their host paths, and only the file name is used here.

A ``postgres/<role>`` value also replaces the password in ``DATABASE_URL`` and
in any ``*_DATABASE_PASSWORD`` line that held the old ``DB_SERVICE_PASSWORD``,
since those lines carry the same secret.

A file is rewritten only when a value differs, through a temp file in the same
folder with the old file's mode, and never created. Output names files and
items, never values. Exit codes: 0 done, 1 the broker is unreachable, refused
the call or sent a malformed response (nothing written), or a file could not be
written; 3 a file Hermes reads changed (Twenty workspaces, CalDAV accounts),
which takes precedence so the caller still reloads Hermes.
"""

from __future__ import annotations

import http.client
import json
import os
import posixpath
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, unquote

from broker.unlock import DEFAULT_SOCKET, TIMEOUT_SECONDS, UnixHTTPConnection

MAX_RESPONSE = 4 * 1024 * 1024
DEFAULT_TARGETS = "/targets"

# Root key -> (folder under the targets mount, display prefix, Hermes reads it).
FOLDERS = {
    "twenty-workspaces": ("twenty-workspaces", "compose/twenty/workspaces", True),
    "postgres-services": ("postgres-services", "compose/postgres/services", False),
    "caldav-accounts": ("caldav-accounts", "compose/caldav-mcp/accounts", True),
}
# Root key -> the variable naming the host file inside TWENTY_SECRETS_DIR.
TWENTY_FILES = {
    "twenty-encryption-key": "TWENTY_ENCRYPTION_KEY_FILE",
    "twenty-db-password": "TWENTY_DB_PASSWORD_FILE",
}
HERMES_ROOTS = {key for key, (_, _, hermes) in FOLDERS.items() if hermes}

SLUG = r"[a-z0-9-]+"
ROLE = r"[a-z][a-z0-9_]{0,57}"
# Item name pattern -> (root key, file name template or None, key or None).
RULES = (
    (re.compile(rf"twenty/({SLUG})/key-ro"), "twenty-workspaces", "{}.env", "TWENTY_KEY_RO"),
    (re.compile(rf"twenty/({SLUG})/key-rw"), "twenty-workspaces", "{}.env", "TWENTY_KEY_RW"),
    (re.compile(r"twenty/encryption-key"), "twenty-encryption-key", None, None),
    (re.compile(r"twenty/db-password"), "twenty-db-password", None, None),
    (re.compile(rf"postgres/({ROLE})"), "postgres-services", "{}.env", "DB_SERVICE_PASSWORD"),
    (re.compile(rf"caldav/({SLUG})"), "caldav-accounts", "{}.env", "CALDAV_PASSWORD"),
)
KEY_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)")
DATABASE_URL = re.compile(r"(postgres(?:ql)?://[^:/@]+:)([^@]*)(@.*)")


class Write(NamedTuple):
    path: Path
    key: str | None  # None: the whole file is the value
    value: str


class Malformed(Exception):
    pass


def show(name: str) -> str:
    """An item name as printed: names are not secret, but may hold anything."""
    if len(name) <= 200 and name.isprintable():
        return name
    return "(an item with an unprintable name)"


def parse_items(raw: bytes) -> list[dict]:
    try:
        items = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Malformed from None
    if not isinstance(items, list):
        raise Malformed
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "value"} \
                or not isinstance(item["name"], str) or not isinstance(item["value"], str):
            raise Malformed
    return items


def plan(items: list[dict], roots: dict[str, Path],
         unavailable: dict[str, str] | None = None) -> tuple[list[Write], list[str]]:
    """Map items to writes. Problems name items and files, never values.

    ``roots`` maps each root key to its folder (``FOLDERS``) or file
    (``TWENTY_FILES``); ``unavailable`` maps a missing root key to the reason.
    """
    unavailable = unavailable or {}
    problems: list[str] = []
    counts: dict[str, int] = {}
    for item in items:
        counts[item["name"]] = counts.get(item["name"], 0) + 1
    targets: dict[tuple[Path, str | None], list[str]] = {}
    mapped: dict[str, Write] = {}
    for name in sorted(n for n, c in counts.items() if c > 1):
        problems.append(f"{show(name)}: served more than once; refused")
    for item in items:
        name, value = item["name"], item["value"]
        if counts[name] > 1:
            continue
        rule = next(((r, m) for r in RULES if (m := r[0].fullmatch(name))), None)
        if rule is None:
            problems.append(f"{show(name)}: no stack secret file has this name; skipped")
            continue
        (_, root, template, key), match = rule
        if root not in roots:
            problems.append(f"{show(name)}: {unavailable.get(root, 'target not mounted')}; skipped")
            continue
        if not value:
            problems.append(f"{show(name)}: empty value; skipped")
            continue
        if "\0" in value or (key is not None and ("\n" in value or "\r" in value)):
            problems.append(f"{show(name)}: the value has a character the file cannot hold; skipped")
            continue
        path = roots[root] / template.format(match.group(1)) if template else roots[root]
        mapped[name] = Write(path, key, value)
        targets.setdefault((path, key), []).append(name)
    writes = []
    for name, write in mapped.items():
        if len(targets[(write.path, write.key)]) > 1:
            problems.append(f"{show(name)}: maps to the same file as another item; refused")
        else:
            writes.append(write)
    return writes, problems


def _value_of(raw: str) -> str:
    raw = raw.rstrip("\r")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def edit_env(text: str, edits: dict[str, str]) -> str:
    """Replace each key's lines in a KEY=value file, keeping every other line."""
    old_pw = None
    if "DB_SERVICE_PASSWORD" in edits:
        for line in text.split("\n"):
            m = KEY_LINE.fullmatch(line.rstrip("\r"))
            if m and m.group(1) == "DB_SERVICE_PASSWORD":
                old_pw = _value_of(m.group(2))
    lines = text.split("\n")
    seen = set()
    for i, line in enumerate(lines):
        eol = "\r" if line.endswith("\r") else ""
        m = KEY_LINE.fullmatch(line[:-1] if eol else line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key in edits:
            lines[i] = f"{key}={edits[key]}{eol}"
            seen.add(key)
        elif old_pw and "DB_SERVICE_PASSWORD" in edits:
            new_pw = edits["DB_SERVICE_PASSWORD"]
            url = DATABASE_URL.fullmatch(_value_of(raw)) if key == "DATABASE_URL" else None
            if url and unquote(url.group(2)) == old_pw:
                lines[i] = f"{key}={url.group(1)}{quote(new_pw, safe='')}{url.group(3)}{eol}"
            elif key.endswith("_DATABASE_PASSWORD") and _value_of(raw) == old_pw:
                lines[i] = f"{key}={new_pw}{eol}"
    out = "\n".join(lines)
    missing = [k for k in edits if k not in seen]
    if missing:
        eol = "\r\n" if "\r\n" in text else "\n"
        if out and not out.endswith("\n"):
            out += eol
        out += "".join(f"{k}={edits[k]}{eol}" for k in missing)
    return out


def replace_file(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def apply(writes: list[Write]) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Apply writes per file. Returns (changed paths, [(path, problem)])."""
    by_path: dict[Path, list[Write]] = {}
    for w in writes:
        by_path.setdefault(w.path, []).append(w)
    changed, problems = [], []
    for path, group in by_path.items():
        try:
            st = os.stat(path)
        except FileNotFoundError:
            problems.append((path, "no such file; skipped"))
            continue
        except OSError as exc:
            problems.append((path, f"cannot read ({exc.strerror})"))
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append((path, "not a regular file; skipped"))
            continue
        try:
            old = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            problems.append((path, "not UTF-8; skipped"))
            continue
        except OSError as exc:
            problems.append((path, f"cannot read ({exc.strerror})"))
            continue
        if group[0].key is None:
            new = group[0].value + ("\n" if old.endswith("\n") else "")
        else:
            new = edit_env(old, {w.key: w.value for w in group})
        if new == old:
            continue
        try:
            replace_file(path, new.encode("utf-8"), stat.S_IMODE(st.st_mode))
        except OSError as exc:
            problems.append((path, f"could not write ({exc.strerror})"))
            continue
        changed.append(path)
    return changed, problems


def fetch(socket_path: str) -> bytes | int | None:
    """The response body, the status when it is not 200, or None when unreachable."""
    conn = UnixHTTPConnection(socket_path, timeout=TIMEOUT_SECONDS)
    try:
        conn.request("GET", "/secrets")
        resp = conn.getresponse()
        body = resp.read(MAX_RESPONSE + 1)
        if resp.status != 200:
            return resp.status
        return body
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def resolve_roots(env: dict[str, str]) -> tuple[dict[str, Path], dict[str, str], dict[Path, str]]:
    """Root paths, reasons for missing ones, and display names for the file roots."""
    base = Path(env.get("BITWARDEN_SYNC_TARGETS") or DEFAULT_TARGETS)
    roots = {key: base / folder for key, (folder, _, _) in FOLDERS.items()}
    unavailable: dict[str, str] = {}
    shown: dict[Path, str] = {}
    secrets_dir = (env.get("TWENTY_SECRETS_DIR") or "").strip()
    for key, var in TWENTY_FILES.items():
        host = (env.get(var) or "").strip()
        if not host:
            unavailable[key] = f"{var} is not set"
        elif not secrets_dir:
            unavailable[key] = f"{var} is set but TWENTY_SECRETS_DIR is not"
        elif posixpath.dirname(posixpath.normpath(host)) != posixpath.normpath(secrets_dir):
            unavailable[key] = f"{var} is not a file directly in TWENTY_SECRETS_DIR"
        else:
            roots[key] = base / "twenty-secrets" / posixpath.basename(posixpath.normpath(host))
            shown[roots[key]] = host
    return roots, unavailable, shown


def display(path: Path, roots: dict[str, Path], shown: dict[Path, str]) -> str:
    if path in shown:
        return shown[path]
    for key, (_, prefix, _) in FOLDERS.items():
        if path.parent == roots[key]:
            return f"{prefix}/{path.name}"
    return path.name


def is_hermes_side(path: Path, roots: dict[str, Path]) -> bool:
    return any(path.parent == roots[key] for key in HERMES_ROOTS)


def main() -> int:
    env = dict(os.environ)
    result = fetch(env.get("BITWARDEN_SYNC_SOCKET") or DEFAULT_SOCKET)
    if result is None:
        print("The Bitwarden broker is not reachable.", file=sys.stderr)
        return 1
    if isinstance(result, int):
        print(f"The Bitwarden broker refused the request for stack secrets (HTTP {result}).", file=sys.stderr)
        return 1
    try:
        if len(result) > MAX_RESPONSE:
            raise Malformed
        items = parse_items(result)
    except Malformed:
        print("The Bitwarden broker sent a malformed response; nothing was written.", file=sys.stderr)
        return 1
    result = None
    roots, unavailable, shown = resolve_roots(env)
    writes, problems = plan(items, roots, unavailable)
    items = None
    for problem in problems:
        print(problem)
    changed, failed = apply(writes)
    writes = None
    for path in changed:
        print(f"changed {display(path, roots, shown)}")
    write_error = False
    for path, problem in failed:
        is_error = not problem.endswith("skipped")
        write_error = write_error or is_error
        print(f"{display(path, roots, shown)}: {problem}", file=sys.stderr if is_error else sys.stdout)
    if any(is_hermes_side(path, roots) for path in changed):
        return 3
    return 1 if write_error else 0


if __name__ == "__main__":
    sys.exit(main())
