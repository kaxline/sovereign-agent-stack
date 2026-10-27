#!/usr/bin/env python3
"""Checks for the IMAP source, against synthetic mail only. See docs/email.md.

Unit checks run everywhere. A small fake IMAP server in this file checks
STARTTLS and the login methods (LOGIN, AUTHENTICATE PLAIN, LOGINDISABLED).
The end-to-end checks start a throwaway GreenMail server in Docker, load the
synthetic specs into it, and index it, in the clear and over TLS with a
certificate made for this run. They're skipped (with a note) when Docker
isn't running. On macOS with swiftc, the mail apps are built against a
throwaway keychain file to check the Keychain handoff. No real account or
keychain is used: the servers, users, passwords and certificates exist only
for this run.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import base64
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import mail_credentials  # noqa: E402
import mail_imap  # noqa: E402
import mail_imap_conn  # noqa: E402
import mail_index as mi  # noqa: E402
import synthetic_mail as sm  # noqa: E402

IMAGE = "greenmail/standalone:2.1.3"
PASSWORD = "synthetic-pw"  # a throwaway user on a throwaway server
USERS = {"personal": sm.ME[1], "work": sm.WORK_ME[1]}
PASSWORD_ENV = "SYNTHETIC_IMAP_PASSWORD"
KEYSTORE_PASSWORD = "synthetic-keystore"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


# --------------------------------------------------------------------------- unit checks

def test_mutf7() -> None:
    for text in ("INBOX", "Café", "日本語/Notes", "R&D", "Ünïcödé & more", "~peter/mail/台北"):
        enc = mail_imap.encode_mutf7(text)
        expect(enc.isascii() and mail_imap.decode_mutf7(enc) == text, f"modified UTF-7 round trip: {text!r}")
    expect(mail_imap.decode_mutf7("Caf&AOk-") == "Café", "decodes a known name")
    expect(mail_imap.encode_mutf7("R&D") == "R&-D", "& is escaped as &-")


def test_parse_list() -> None:
    data = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        b'(\\HasNoChildren \\Trash) "." "Papierkorb"',
        b'(\\HasNoChildren) "." "Projects.Alpha"',
        b'(\\HasNoChildren) "/" Archive',
        (b'(\\HasNoChildren) "/" {11}', b'Caf&AOk-/Q4'),
        b'(\\HasNoChildren) "/" "Quote \\"me\\""',
    ]
    boxes = {b.path: b for b in mail_imap.parse_list(data)}
    expect(set(boxes) == {"INBOX", "[Gmail]", "[Gmail]/All Mail", "[Gmail]/Spam", "Papierkorb",
                          "Projects/Alpha", "Archive", "Café/Q4", 'Quote "me"'}, f"LIST parsed: {sorted(boxes)}")
    expect(boxes["Projects/Alpha"].raw == "Projects.Alpha", "keeps the server's own name for EXAMINE")
    expect(not boxes["[Gmail]"].selectable, "\\Noselect isn't selectable")
    star = ["*", "[Gmail]/*"]
    picked = {p for p, b in boxes.items() if mail_imap.in_scope(b, star)}
    expect(picked == {"INBOX", "Archive", 'Quote "me"'},
           f"patterns skip junk, trash and virtual mailboxes by flag: {sorted(picked)}")
    expect(mail_imap.in_scope(boxes["Papierkorb"], ["Papierkorb"]), "naming trash includes it")
    expect(not mail_imap.in_scope(boxes["[Gmail]"], ["**"]), "unselectable mailboxes are never in scope")


def test_config() -> None:
    base = Path("/nonexistent")
    good = {"id": "work", "mailboxes": ["INBOX"], "source": "imap", "host": "imap.example.com",
            "user": "sam@example.com", "address": "sam@example.com"}
    s = mail_imap.account_settings(good, base, where="a")
    expect(s["port"] == 993 and s["security"] == "tls" and s["addresses"] == ["sam@example.com"], f"defaults: {s}")
    expect(mail_imap.account_settings(good | {"security": "starttls"}, base, where="a")["port"] == 143, "starttls port")
    bad = {
        "no host": {k: v for k, v in good.items() if k != "host"},
        "no user": {k: v for k, v in good.items() if k != "user"},
        "plain to a remote host": good | {"security": "plain"},
        "unknown security": good | {"security": "ssl3"},
        "password in config": good | {"password": "hunter2"},
        "bad port": good | {"port": 70000},
        "display name address": good | {"address": "Sam <sam@example.com>"},
        "bad env name": good | {"password_env": "lower case"},
    }
    for name, item in bad.items():
        try:
            mail_imap.account_settings(item, base, where="a")
        except mi.ConfigError:
            continue
        fail(f"imap account accepted: {name}")
    for host in ("127.0.0.1", "localhost", "::1"):
        mail_imap.account_settings(good | {"host": host, "security": "plain"}, base, where="a")


def test_reader_refuses_writes() -> None:
    conn = mail_imap.ReadOnlyImap("x", {})
    for items in ("(BODY[])", "(UID BODY[HEADER])", "(FLAGS) +FLAGS (\\Deleted)", "(UID FLAGS) STORE",
                  "(RFC822)", "(BODY.PEEK[]) (BODY[])"):
        try:
            conn.uid_fetch([1], items)
        except mail_imap.Refused:
            continue
        fail(f"reader allowed FETCH items {items!r}")
    for name in ("store", "append", "expunge", "copy", "move", "delete", "create", "rename", "uid"):
        expect(not hasattr(mail_imap.ReadOnlyImap, name), f"reader exposes {name}()")


def test_reader_source_reads_only() -> None:
    """A test, as for mail_script.js: the reader's code must not gain a write."""
    code = (LIB / "mail_imap.py").read_text()
    calls = r"\b_?imap\.(store|append|expunge|copy|move|delete|create|rename|subscribe|setacl|setquota|xatom)\("
    for pattern in (calls,
                    r"""['"](STORE|COPY|MOVE|EXPUNGE|APPEND|DELETE)['"]""", r"BODY\[\]"):
        hit = re.search(pattern, code)
        expect(hit is None, f"mail_imap.py gained a write: {hit.group(0) if hit else ''}")
    for line in code.splitlines():
        if ".select(" in line:
            expect("readonly=True" in line, f"SELECT without readonly=True: {line.strip()}")


def test_credentials_handoff() -> None:
    """The launcher writes {account: password} to stdin and sets MAIL_CREDENTIALS_FD=0."""
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import mail_credentials as mc, os\n"
             "print(mc.password('Work', None) == 'pw-1', 'MAIL_CREDENTIALS_FD' not in os.environ)\n"
             "try:\n    mc.password('other', None)\nexcept mc.MissingPassword as e:\n    print('missing', 'pw-1' not in str(e))\n")
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(LIB)], input='{"work": "pw-1"}',
                         capture_output=True, text=True, env={"MAIL_CREDENTIALS_FD": "0", "PATH": "/usr/bin"})
    expect(out.stdout.split() == ["True", "True", "missing", "True"],
           f"password handoff over stdin: {out.stdout!r} {out.stderr!r}")
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(LIB)], input="", capture_output=True, text=True,
                         env={"PATH": "/usr/bin"})
    expect("MissingPassword" in out.stderr or "Error" in out.stderr, "no launcher, no password_env: refused")


def imap_calls(path: Path) -> set[str]:
    """Every imaplib method a module calls on its connection (self._imap.x / imap.x)."""
    return set(re.findall(r"\b(?:self\._imap|imap)\.(\w+)\(", path.read_text()))


def test_shared_and_drafts_code() -> None:
    calls = imap_calls(LIB / "mail_imap_conn.py")
    expect(calls <= {"authenticate", "list", "login", "logout", "starttls", "shutdown"},
           f"shared IMAP code gained {calls}")
    code = (LIB / "mail_imap_drafts.py").read_text()
    calls = imap_calls(LIB / "mail_imap_drafts.py")
    expect(calls <= {"select", "response", "uid", "append"}, f"drafts IMAP code gained {calls}")
    for m in re.finditer(r"\._imap\.uid\(\s*['\"](\w+)['\"],\s*\w+,\s*(\w+)", code):
        expect(m.group(1) == "FETCH" and m.group(2) == "REPLY_HEADERS", f"drafts UID {m.group(0)}")
    expect(re.search(r'REPLY_HEADERS = "\(UID BODY\.PEEK\[HEADER\.FIELDS \([A-Z -]+\)\]\)"', code),
           "drafts fetch only named headers, with PEEK")
    for line in code.splitlines():
        if ".select(" in line:
            expect("readonly=True" in line, f"drafts SELECT without readonly=True: {line.strip()}")
        if ".append(" in line and "_imap" in line:
            expect("box.raw" in line, "drafts append only to the Drafts box")
    expect("smtplib" not in code and "smtplib" not in (LIB / "mail_drafts.py").read_text(), "no SMTP anywhere")
    for m in re.finditer(r"\._imap\.uid\(\s*['\"](\w+)", code):
        expect(m.group(1) in ("SEARCH", "FETCH"), f"UID {m.group(1)} in the reader")


# --------------------------------------------------------------------------- certificates

def make_certs(out: Path) -> Path | None:
    """A throwaway CA and a server certificate for localhost and 127.0.0.1:
    ca.pem, server.pem, server.key, and server.p12 for GreenMail. None
    (checks skipped) without the openssl command."""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    out.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run([openssl, *args], check=True, capture_output=True, cwd=out)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=Synthetic Test CA", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=localhost")
    (out / "server.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                    "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
                                    "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
        "-out", "server.pem", "-extfile", "server.ext")
    run("pkcs12", "-export", "-in", "server.pem", "-inkey", "server.key", "-name", "greenmail",
        "-out", "server.p12", "-passout", f"pass:{KEYSTORE_PASSWORD}")
    for name in ("server.p12", "ca.pem"):
        (out / name).chmod(0o644)  # GreenMail runs as another user in the container
    return out


# --------------------------------------------------------------------------- fake IMAP server

class FakeImap:
    """Just enough IMAP to check connecting and logging in: CAPABILITY,
    STARTTLS, LOGIN, AUTHENTICATE PLAIN, LIST and LOGOUT. Records each
    command with whether TLS was on when it arrived."""

    def __init__(self, caps: str, *, tls_caps: str | None = None, certs: Path | None = None,
                 user: str = "sam@example.com", password: str = "pw-1") -> None:
        self.caps, self.tls_caps, self.user, self.password = caps, tls_caps, user, password
        self.ctx = None
        if certs:
            self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.ctx.load_cert_chain(certs / "server.pem", certs / "server.key")
        self.log: list[tuple[bool, str]] = []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        tls, caps = False, self.caps
        f = conn.makefile("rwb")

        def say(line: str) -> None:
            f.write(line.encode() + b"\r\n")
            f.flush()

        say("* OK fake IMAP ready")
        while line := f.readline():
            tag, _, rest = line.decode().rstrip("\r\n").partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            self.log.append((tls, cmd))
            if cmd == "CAPABILITY":
                say(f"* CAPABILITY {caps}")
                say(f"{tag} OK done")
            elif cmd == "STARTTLS" and "STARTTLS" in caps.split() and self.ctx:
                say(f"{tag} OK begin TLS")
                try:
                    conn = self.ctx.wrap_socket(conn, server_side=True)
                except ssl.SSLError:  # the client refused our certificate
                    break
                f = conn.makefile("rwb")
                tls, caps = True, self.tls_caps or self.caps
            elif cmd == "LOGIN":
                ok = "LOGINDISABLED" not in caps.split() and args == f'{self.user} "{self.password}"'
                say(f"{tag} OK logged in" if ok else f"{tag} NO refused")
            elif cmd == "AUTHENTICATE" and args.upper() == "PLAIN" and "AUTH=PLAIN" in caps.split():
                say("+ ")
                token = base64.b64decode(f.readline().strip())
                ok = token == b"\0" + self.user.encode() + b"\0" + self.password.encode()
                say(f"{tag} OK authenticated" if ok else f"{tag} NO refused")
            elif cmd == "LIST":
                say('* LIST (\\HasNoChildren) "/" "INBOX"')
                say(f"{tag} OK done")
            elif cmd == "LOGOUT":
                say("* BYE")
                say(f"{tag} OK bye")
                break
            else:
                say(f"{tag} BAD not here")
        conn.close()

    def commands(self) -> list[tuple[bool, str]]:
        return [c for c in self.log if c[1] != "CAPABILITY"]


def fake_login(server: FakeImap, *, security: str = "plain", password: str = "pw-1", certs: Path | None = None):
    """Open, LIST and close an account on `server`. Returns the mailboxes or the exception raised."""
    item = {"id": "fake", "mailboxes": ["INBOX"], "source": "imap", "host": "127.0.0.1", "port": server.port,
            "security": security, "user": "sam@example.com", "password_env": "FAKE_IMAP_PASSWORD", "timeout": 10}
    if certs:
        item["tls_ca"] = str(certs / "ca.pem")
    settings = mail_imap_conn.account_settings(item, Path("/nonexistent"), where="fake")
    os.environ["FAKE_IMAP_PASSWORD"] = password
    try:
        with mail_imap_conn.Connection("fake", settings) as conn:
            return [b.path for b in conn.list_mailboxes()]
    except (mail_imap_conn.ImapError, mail_credentials.MissingPassword) as exc:
        return exc
    finally:
        del os.environ["FAKE_IMAP_PASSWORD"]


def test_login_methods(certs: Path | None) -> None:
    server = FakeImap("IMAP4rev1")
    expect(fake_login(server) == ["INBOX"] and server.commands()[0] == (False, "LOGIN"), f"LOGIN: {server.log}")

    server = FakeImap("IMAP4rev1 AUTH=PLAIN")
    expect(fake_login(server) == ["INBOX"] and server.commands()[0] == (False, "AUTHENTICATE"),
           f"AUTHENTICATE PLAIN when offered: {server.log}")

    server = FakeImap("IMAP4rev1 AUTH=PLAIN", password="pässwörd-ü")
    expect(fake_login(server, password="pässwörd-ü") == ["INBOX"], "AUTHENTICATE PLAIN carries a UTF-8 password")

    server = FakeImap("IMAP4rev1 AUTH=PLAIN")
    got = fake_login(server, password="wrong")
    expect(isinstance(got, mail_imap_conn.AuthFailed) and "wrong" not in str(got), f"refused AUTHENTICATE: {got}")
    expect([c for _, c in server.commands()].count("AUTHENTICATE") == 1 and (False, "LOGIN") not in server.log,
           f"one login attempt, no LOGIN retry: {server.log}")

    server = FakeImap("IMAP4rev1 LOGINDISABLED")
    got = fake_login(server)
    expect(isinstance(got, mail_imap_conn.AuthFailed) and "neither" in str(got), f"LOGINDISABLED: {got}")
    expect(server.commands() == [], f"nothing sent when no login method is allowed: {server.log}")

    server = FakeImap("IMAP4rev1", password="pässwörd")
    got = fake_login(server, password="pässwörd")
    expect(isinstance(got, mail_imap_conn.AuthFailed) and "non-ASCII" in str(got) and "pässwörd" not in str(got),
           f"non-ASCII password over LOGIN: {got!r}")

    if not certs:
        print("note: openssl not found; STARTTLS checks skipped")
        return
    server = FakeImap("IMAP4rev1 STARTTLS LOGINDISABLED", tls_caps="IMAP4rev1 AUTH=PLAIN", certs=certs)
    expect(fake_login(server, security="starttls", certs=certs) == ["INBOX"], f"STARTTLS login: {server.log}")
    expect(server.commands()[0] == (False, "STARTTLS") and all(tls for tls, _ in server.commands()[1:]),
           f"everything after STARTTLS is encrypted: {server.log}")
    expect((True, "AUTHENTICATE") in server.log, "capabilities re-read after STARTTLS")

    server = FakeImap("IMAP4rev1 LOGINDISABLED", certs=certs)
    got = fake_login(server, security="starttls", certs=certs)
    expect(isinstance(got, mail_imap_conn.ImapError) and not isinstance(got, mail_imap_conn.AuthFailed),
           f"security = starttls refuses a server without STARTTLS: {got!r}")
    expect(server.commands() == [], f"no credentials sent in the clear: {server.log}")

    server = FakeImap("IMAP4rev1 STARTTLS", certs=certs)
    got = fake_login(server, security="starttls")
    expect(isinstance(got, mail_imap_conn.ImapError) and "CERTIFICATE_VERIFY_FAILED" in str(got),
           f"STARTTLS checks the certificate: {got!r}")
    expect((False, "LOGIN") not in server.log and (True, "LOGIN") not in server.log, "no login after a bad certificate")


# --------------------------------------------------------------------------- GreenMail

def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@contextmanager
def greenmail(certs: Path | None):
    """GreenMail with IMAP on 3143 and, given certs, IMAPS on 3993 with that
    certificate. Yields (imap port, imaps port or None)."""
    name = f"synthetic-mail-imap-{os.getpid()}"
    users = ",".join(f"{addr.split('@')[0]}:{PASSWORD}@{addr.split('@')[1]}" for addr in USERS.values())
    opts = (f"-Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users={users}"
            " -Dgreenmail.users.login=email")
    args = ["-p", "127.0.0.1::3143"]
    if certs:
        opts += (" -Dgreenmail.setup.test.imaps -Dgreenmail.tls.keystore.file=/certs/server.p12"
                 f" -Dgreenmail.tls.keystore.password={KEYSTORE_PASSWORD}")
        args += ["-p", "127.0.0.1::3993", "-v", f"{certs}:/certs:ro"]
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, *args,
                    "-e", f"GREENMAIL_OPTS={opts}", IMAGE], check=True, capture_output=True)

    def host_port(inner: int) -> int:
        return int(subprocess.run(["docker", "port", name, str(inner)], check=True, capture_output=True,
                                  text=True).stdout.strip().splitlines()[0].rsplit(":", 1)[1])

    try:
        port = host_port(3143)
        tls_port = host_port(3993) if certs else None
        deadline = time.time() + 60
        while True:
            try:
                imaplib.IMAP4("127.0.0.1", port, timeout=5).logout()
                break
            except (OSError, imaplib.IMAP4.error):
                if time.time() > deadline:
                    fail("GreenMail didn't start")
                time.sleep(0.5)
        yield port, tls_port
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


def admin(port: int, account: str) -> imaplib.IMAP4:
    """A full-access test connection, for seeding and checking the server."""
    conn = imaplib.IMAP4("127.0.0.1", port, timeout=10)
    conn.login(USERS[account], PASSWORD)
    return conn


def server_name(path: str) -> str:
    return mail_imap.encode_mutf7(path.replace("/", "."))  # GreenMail separates levels with "."


def appended_at(epoch: int) -> str:
    """APPEND's date. GreenMail 2.1 reads a 12:xx hour as 00:xx, so write the
    same instant in a zone where the hour isn't 12."""
    for hours in (0, 1):
        when = datetime.fromtimestamp(epoch, timezone(timedelta(hours=hours)))
        if when.hour != 12:
            return imaplib.Time2Internaldate(when)
    raise AssertionError("unreachable")


def seed(port: int) -> None:
    for account in USERS:
        conn = admin(port, account)
        for spec in sm.SPECS:
            if spec.account != account:
                continue
            box = server_name(spec.mailbox)
            if box != "INBOX":
                conn.create(mail_imap.quote(box))
            flags = [f for bit, f in ((sm.FLAG_READ, "\\Seen"), (sm.FLAG_FLAGGED, "\\Flagged"),
                                      (sm.FLAG_ANSWERED, "\\Answered"), (sm.FLAG_DELETED, "\\Deleted"))
                     if spec.flags & bit]
            raw = sm.build_message(spec).as_bytes(policy=sm.POLICY.clone(linesep="\r\n"))
            typ, _ = conn.append(mail_imap.quote(box), f"({' '.join(flags)})",
                                 appended_at(spec.epoch), raw)
            expect(typ == "OK", f"seeding {spec.rowid}")
        conn.logout()


def server_state(port: int) -> dict:
    """Every message's flags in every mailbox (without \\Recent, which is per session)."""
    state = {}
    for account in USERS:
        conn = admin(port, account)
        for box in mail_imap.parse_list(conn.list()[1]):
            typ, _ = conn.select(mail_imap.quote(box.raw), readonly=True)
            if typ != "OK":
                expect(not box.selectable, f"test connection can't open {box.path}")
                continue
            typ, data = conn.uid("FETCH", "1:*", "(UID FLAGS)")
            for meta, _ in mail_imap.fetch_items(data or []):
                if meta and b"UID" in meta:
                    flags = mail_imap.flags_of(meta) - {"\\recent"}
                    state[(account, box.path, mail_imap.uid_of(meta))] = sorted(flags)
        conn.logout()
    return state


def write_config(tmp: Path, port: int, accounts: dict[str, list[str]], *, index: Path, name: str,
                 bodies=True, attachments=True, store: Path | None = None, security: str = "plain",
                 tls_ca: Path | None = None, password_env: bool = True) -> Path:
    lines = [f'index = "{index}"', "since = 2026-01-01", f"bodies = {str(bodies).lower()}",
             f"attachments = {str(attachments).lower()}"]
    if store:
        lines.append(f'store = "{store}"')
    for key, boxes in accounts.items():
        if key.startswith("store:"):
            lines += ["", "[[accounts]]", f'id = "{key[6:]}"', 'source = "store"', f"mailboxes = {json.dumps(boxes)}"]
            continue
        lines += ["", "[[accounts]]", f'id = "{key}"', 'source = "imap"', 'host = "127.0.0.1"', f"port = {port}",
                  f'security = "{security}"', f'user = "{USERS[key]}"', f'address = "{USERS[key]}"',
                  f"mailboxes = {json.dumps(boxes)}"]
        if password_env:
            lines.append(f'password_env = "{PASSWORD_ENV}"')
        if tls_ca:
            lines.append(f'tls_ca = "{tls_ca}"')
    path = tmp / f"{name}.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def rows(index: Path) -> dict[str, sqlite3.Row]:
    db = sqlite3.connect(index)
    db.row_factory = sqlite3.Row
    out = {r["message_id"]: r for r in db.execute("SELECT * FROM messages")}
    db.close()
    return out


def test_end_to_end(tmp: Path, port: int) -> None:
    os.environ[PASSWORD_ENV] = PASSWORD
    seed(port)
    before = server_state(port)

    # Same scope through the IMAP source and through the synthetic Mail store.
    store = tmp / "synthetic-mail-store"
    sm.build(store)
    scope = {"personal": ["*"], "work": ["**"]}
    imap_cfg = mi.load_config(write_config(tmp, port, scope, index=tmp / "imap" / "index.sqlite", name="imap"))
    stats = mi.run(imap_cfg)
    store_cfg_path = tmp / "store.toml"
    store_cfg_path.write_text(
        f'source = "store"\nstore = "{store}"\nindex = "{tmp / "store" / "index.sqlite"}"\nsince = 2026-01-01\n'
        "bodies = true\nattachments = true\n"
        f'[[accounts]]\nid = "{sm.ACCOUNTS["personal"].id}"\nmailboxes = ["*"]\n'
        f'[[accounts]]\nid = "{sm.ACCOUNTS["work"].id}"\nmailboxes = ["**"]\n')
    mi.run(mi.load_config(store_cfg_path))
    got, want = rows(imap_cfg.index), rows(tmp / "store" / "index.sqlite")
    expect(set(got) == set(want), f"IMAP and store index the same messages: {sorted(set(got) ^ set(want))}")
    expect(stats.added == len(want) and stats.missing_files == 0 and stats.unreadable == 0, f"stats {stats}")
    for mid, w in want.items():
        g = got[mid]
        for col in ("mailbox", "subject", "from_addr", "from_name", "to_addrs", "cc_addrs", "is_read",
                    "is_flagged", "date_received", "date_sent", "in_reply_to", "body"):
            expect(g[col] == w[col], f"{mid} {col}: imap {g[col]!r} != store {w[col]!r}")
        names = [[a["name"] for a in json.loads(r["attachments"] or "[]")] for r in (g, w)]
        expect(names[0] == names[1], f"{mid}: attachment names {names}")
        expect(re.fullmatch(r"\d+\.\d+", g["source_id"]), f"source id is UIDVALIDITY.UID: {g['source_id']}")
    expect(got["<synthetic-106@example.net>"]["thread_id"] == got["<synthetic-1205@example.net>"]["thread_id"],
           "a reply joins its parent's thread")
    expect("Projects/Alpha" in {r["mailbox"] for r in got.values()}, "nested mailbox path uses /")
    expect(server_state(port) == before, "indexing changed nothing on the server (no flags, no deletes)")

    # Incremental: nothing new, then one new, one re-flagged, one deleted.
    stats = mi.run(imap_cfg)
    expect((stats.added, stats.updated, stats.removed) == (0, 0, 0), f"second run is a no-op: {stats}")
    top = max(r["rowid"] for r in rows(imap_cfg.index).values())
    conn = admin(port, "personal")
    new = sm.build_message(sm.Spec(rowid=999, account="personal", mailbox="INBOX", sender=("Avery Kim", "avery@example.com"),
                                   to=[sm.ME], subject="New on the server", date="Fri, 09 Oct 2026 09:00:00 +0000",
                                   text="Brand new."))
    conn.append("INBOX", "()", imaplib.Time2Internaldate(time.time()), new.as_bytes(policy=sm.POLICY.clone(linesep="\r\n")))
    conn.select("INBOX")
    _, data = conn.uid("SEARCH", "HEADER", "Message-ID", "<synthetic-23456@example.net>")
    conn.uid("STORE", data[0].split()[0], "+FLAGS", "(\\Seen)")
    _, data = conn.uid("SEARCH", "HEADER", "Message-ID", "<synthetic-23458@example.net>")
    conn.uid("STORE", data[0].split()[0], "+FLAGS", "(\\Deleted)")
    conn.expunge()
    conn.logout()
    stats = mi.run(imap_cfg)
    expect((stats.added, stats.updated, stats.removed) == (1, 1, 1), f"incremental changes: {stats}")
    now = rows(imap_cfg.index)
    expect(now["<synthetic-999@example.net>"]["rowid"] > top, "new mail gets a higher id")
    expect(now["<synthetic-23456@example.net>"]["is_read"] == 1, "read flag picked up")
    expect("<synthetic-23458@example.net>" not in now, "expunged message removed")

    # A mailbox recreated on the server gets a new UIDVALIDITY: re-indexed, not confused.
    conn = admin(port, "personal")
    conn.delete("Archive")
    conn.create("Archive")
    spec = next(s for s in sm.SPECS if s.mailbox == "Archive")
    conn.append("Archive", "(\\Seen)", appended_at(spec.epoch),
                sm.build_message(spec).as_bytes(policy=sm.POLICY.clone(linesep="\r\n")))
    conn.logout()
    old_sid = now[spec.message_id]["source_id"]
    stats = mi.run(imap_cfg)
    later = rows(imap_cfg.index)
    expect(stats.added == 1 and stats.removed == 1 and later[spec.message_id]["source_id"] != old_sid,
           f"UIDVALIDITY change re-indexes the mailbox: {stats}")

    # Mixed sources in one index: personal from the Mail store, work over IMAP.
    mixed = mi.load_config(write_config(
        tmp, port, {f"store:{sm.ACCOUNTS['personal'].id}": ["INBOX"], "work": ["**"]},
        index=tmp / "mixed" / "index.sqlite", name="mixed", store=store))
    stats = mi.run(mixed)
    m = rows(mixed.index)
    sources = {r["account"]: r["source_id"] for r in m.values()}
    expect(stats.added == len(m) == 9, f"mixed index: {stats}")
    expect(sources["work"].count(".") == 1 and sources[sm.ACCOUNTS["personal"].id].isdigit(),
           f"each account keeps its own source ids: {sources}")
    expect(len({r["rowid"] for r in m.values()}) == len(m), "ids unique across sources")

    # Switching an account to another source re-indexes just that account.
    db = sqlite3.connect(mixed.index)
    db.execute("UPDATE meta SET value = ? WHERE key = 'sources'",
               (json.dumps({sm.ACCOUNTS["personal"].id.upper(): "store", "WORK": "script"}),))
    db.commit()
    db.close()
    stats = mi.run(mixed)
    expect(stats.removed == 2 and stats.added == 2, f"only the switched account is rebuilt: {stats}")

    # Doctor and the CLI print counts, never content.
    report = mi.doctor(imap_cfg)
    expect(report["source"] == "imap" and report["accounts"][0]["details_readable"] > 0, f"doctor: {report}")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "run"], capture_output=True, text=True,
                         env={**os.environ, PASSWORD_ENV: PASSWORD})
    expect(out.returncode == 0, f"CLI run: {out.stderr}")
    for spec in sm.SPECS:
        expect(spec.subject not in out.stdout + out.stderr, "CLI output leaked a subject")
    expect(PASSWORD not in out.stdout + out.stderr, "CLI output leaked the password")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "run"], capture_output=True, text=True,
                         env={**os.environ, PASSWORD_ENV: "wrong"})
    expect(out.returncode == 4 and "rejected" in out.stderr and "wrong" not in out.stderr,
           f"a wrong password exits 4 without echoing it: {out.returncode} {out.stderr}")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config",
                          str(tmp / "imap.toml"), "mailboxes"], capture_output=True, text=True)
    listing = json.loads(out.stdout)
    expect({"account": "work", "mailbox": "Projects/Alpha", "messages": 1, "special_use": None} in listing,
           f"mailboxes lists IMAP mailboxes with counts: {listing}")


def test_drafts(tmp: Path, port: int) -> None:
    """IMAP drafts: new and reply drafts land in Drafts, flagged \\Draft;
    nothing else on the server changes."""
    sys.path.insert(0, str(HERE.parent / "compose" / "mail-drafts-mcp"))
    sys.path.insert(0, str(HERE.parent / "compose" / "mail-mcp"))
    import drafts
    import mail_drafts
    import mail_imap_conn
    import mail_query as mq

    conn = admin(port, "personal")
    conn.create("Drafts")
    conn.logout()
    before = server_state(port)

    accounts = mail_imap_conn.imap_accounts(tmp / "imap.toml")
    expect(set(accounts) == {"PERSONAL", "WORK"}, f"drafts helper finds the IMAP accounts: {set(accounts)}")
    db = mq.connect(tmp / "imap" / "index.sqlite")
    hit = mq.search(db, "lease", sender="priya")["results"][0]
    db.close()
    root = tmp / "drafts"
    me = USERS["personal"]
    new_id = drafts.enqueue(root, {"to": ["alex.chen@example.com"], "subject": "Saturday", "body": "See you then.",
                                   "from": me})
    reply_id = drafts.enqueue(root, {"reply_to": hit["reply_ref"], "body": "Signed and attached."})
    stranger = drafts.enqueue(root, {"to": ["alex.chen@example.com"], "subject": "x", "body": "x",
                                     "from": "someone.else@example.com"})
    gone = drafts.enqueue(root, {"reply_to": {**hit["reply_ref"], "source_id": "1.999999"}, "body": "x"})
    helper = mail_drafts.Helper(root, imap_accounts=accounts, mail_app=False)
    stats = helper.process()
    expect((stats.created, stats.rejected, stats.failed) == (2, 1, 1), f"drafts stats: {stats}")
    expect(drafts.read_status(root, stranger)["reason"] == "from_not_an_account", "unknown sender rejected")
    expect(drafts.read_status(root, gone)["reason"] == "reply_message_not_found", "missing original fails")
    for rid in (new_id, reply_id, stranger, gone):
        text = (root / "status" / f"{rid}.json").read_text()
        expect("alex.chen" not in text and "Saturday" not in text, "status files hold no recipients or text")

    conn = admin(port, "personal")
    conn.select("Drafts", readonly=True)
    _, data = conn.uid("FETCH", "1:*", "(UID FLAGS BODY.PEEK[])")
    saved = {}
    for meta, raw in mail_imap_conn.fetch_items(data):
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        saved[str(msg["Subject"])] = (mail_imap_conn.flags_of(meta), msg)
    conn.logout()
    expect(set(saved) == {"Saturday", f"Re: {hit['subject']}"}, f"drafts saved: {sorted(saved)}")
    flags, msg = saved["Saturday"]
    expect("\\draft" in flags and str(msg["From"]) == me and str(msg["To"]) == "alex.chen@example.com",
           f"new draft: {flags} {msg['From']} {msg['To']}")
    flags, msg = saved[f"Re: {hit['subject']}"]
    original = f"<synthetic-1205@example.net>"
    expect(str(msg["In-Reply-To"]) == original and original in str(msg["References"]), "reply is threaded")
    expect(hit["from"]["address"] in str(msg["To"]), f"reply goes to the original sender: {msg['To']}")
    expect(msg.get_content().strip() == "Signed and attached.", "reply body is the request's body only")

    after = server_state(port)
    outside = {k: v for k, v in after.items() if k[1] != "Drafts"}
    expect(outside == {k: v for k, v in before.items() if k[1] != "Drafts"},
           "drafts changed nothing outside Drafts (the original isn't marked read or answered)")
    report = helper.doctor()
    expect(report["imap_accounts"][0]["drafts_mailbox_found"] and "sender_addresses" not in report,
           f"drafts doctor: {report}")


def test_tls(tmp: Path, tls_port: int, certs: Path) -> None:
    """The same mail over IMAPS, checked against the certificate's CA."""
    cfg = mi.load_config(write_config(tmp, tls_port, {"work": ["**"]}, index=tmp / "tls" / "index.sqlite",
                                      name="tls", security="tls", tls_ca=certs / "ca.pem"))
    stats = mi.run(cfg)
    plain = {mid: r for mid, r in rows(tmp / "imap" / "index.sqlite").items() if r["account"] == "work"}
    got = rows(cfg.index)
    expect(stats.added == len(plain) > 0 and set(got) == set(plain), f"IMAPS indexes the same mail: {stats}")
    for mid, r in got.items():
        expect((r["subject"], r["body"]) == (plain[mid]["subject"], plain[mid]["body"]), f"{mid} over IMAPS")

    write_config(tmp, tls_port, {"work": ["**"]}, index=tmp / "tls-noca" / "index.sqlite", name="tls-noca",
                 security="tls")
    out = subprocess.run([sys.executable, "-I", str(HERE / "mail-index.py"), "--config", str(tmp / "tls-noca.toml"),
                          "run"], capture_output=True, text=True, env={**os.environ, PASSWORD_ENV: PASSWORD})
    expect(out.returncode == 5 and "CERTIFICATE_VERIFY_FAILED" in out.stderr and PASSWORD not in out.stderr,
           f"without tls_ca the self-signed server is refused: {out.returncode} {out.stderr}")


def mac_build_ok() -> bool:
    return sys.platform == "darwin" and shutil.which("swiftc") is not None and shutil.which("security") is not None


def keychains() -> str:
    return subprocess.run(["security", "list-keychains", "-d", "user"], capture_output=True, text=True).stdout


def test_keychain_handoff(tmp: Path, port: int) -> None:
    """Build both apps against a throwaway keychain file, store the synthetic
    passwords there for those apps only, and run them with no password_env:
    the reader indexes and the drafts app saves a draft with passwords that
    reached them only through the launcher. Your keychains and search list
    aren't touched."""
    prefix = "test.synthetic"
    keychain = tmp / "synthetic-keychain" / "mail-test.keychain-db"
    keychain.parent.mkdir()
    keychain_pw = secrets.token_hex(16)
    search_list = keychains()
    sec = ["security"]
    subprocess.run([*sec, "create-keychain", "-p", keychain_pw, str(keychain)], check=True, capture_output=True)
    try:
        subprocess.run([*sec, "unlock-keychain", "-p", keychain_pw, str(keychain)], check=True, capture_output=True)
        apps = tmp / "apps"
        build = subprocess.run([str(HERE.parent / "macos" / "mail-apps" / "build.sh"), str(apps)], capture_output=True,
                               text=True, env={**os.environ, "MAIL_APPS_KEYCHAIN": str(keychain),
                                               "MAIL_APPS_BUNDLE_PREFIX": prefix})
        expect(build.returncode == 0, f"build: {build.stderr[-500:]}")
        reader, drafts_app = apps / "Assistant Mail Reader.app", apps / "Assistant Mail Drafts.app"
        for account in USERS:
            subprocess.run([*sec, "add-generic-password", "-U", "-s", f"{prefix}.mail-imap", "-a", account,
                            "-l", f"Assistant mail: {account}", "-T", str(reader), "-T", str(drafts_app),
                            "-w", PASSWORD, str(keychain)], check=True, capture_output=True)
        expect(keychains() == search_list, "creating the test keychain left the search list alone")

        cfg = write_config(tmp, port, {"personal": ["*"], "work": ["**"]}, index=tmp / "keychain" / "index.sqlite",
                           name="keychain", password_env=False)
        env = {k: v for k, v in os.environ.items() if k != PASSWORD_ENV}
        out = subprocess.run([str(reader / "Contents" / "MacOS" / "assistant-mail-reader"), "--config", str(cfg),
                              "run"], capture_output=True, text=True, env=env, timeout=120)
        expect(out.returncode == 0, f"reader app with Keychain passwords: {out.returncode} {out.stderr}")
        expect(PASSWORD not in out.stdout + out.stderr, "reader app output leaked the password")
        expect({r["account"] for r in rows(tmp / "keychain" / "index.sqlite").values()} == {"personal", "work"},
               "reader app logged in to both accounts")

        sys.path.insert(0, str(HERE.parent / "compose" / "mail-drafts-mcp"))
        import drafts
        conn = admin(port, "work")
        conn.create("Drafts")
        conn.logout()
        root = tmp / "keychain-drafts"
        rid = drafts.enqueue(root, {"to": ["alex.chen@example.com"], "subject": "From the drafts app",
                                    "body": "Saved with a Keychain password.", "from": USERS["work"]})
        out = subprocess.run([str(drafts_app / "Contents" / "MacOS" / "assistant-mail-drafts"), "--dir", str(root),
                              "--config", str(cfg), "--no-mail-app", "run"], capture_output=True, text=True,
                             env=env, timeout=120)
        status = drafts.read_status(root, rid)
        expect(out.returncode == 0 and status["state"] == "created",
               f"drafts app with Keychain passwords: {out.returncode} {status} {out.stderr}")
        conn = admin(port, "work")
        typ, data = conn.select("Drafts", readonly=True)
        _, found = conn.uid("SEARCH", "SUBJECT", '"From the drafts app"')
        conn.logout()
        expect(typ == "OK" and found[0].split(), "the drafts app's draft is in work's Drafts")
    finally:
        subprocess.run([*sec, "delete-keychain", str(keychain)], capture_output=True)
        expect(keychains() == search_list, "the keychain search list is as it was")


def main() -> int:
    test_mutf7()
    test_parse_list()
    test_config()
    test_reader_refuses_writes()
    test_reader_source_reads_only()
    test_shared_and_drafts_code()
    test_credentials_handoff()
    with tempfile.TemporaryDirectory() as d:
        certs = make_certs(Path(d) / "certs")
        test_login_methods(certs)
        if not docker_ok():
            print("OK mail IMAP (unit checks only: Docker isn't running, GreenMail checks skipped)")
            return 0
        with greenmail(certs) as (port, tls_port):
            test_end_to_end(Path(d), port)
            test_drafts(Path(d), port)
            if certs:
                test_tls(Path(d), tls_port, certs)
            if mac_build_ok():
                test_keychain_handoff(Path(d), port)
            else:
                print("note: not macOS with swiftc; Keychain handoff checks skipped")
    print("OK mail IMAP source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
