#!/usr/bin/env python3
"""setup/tls.py on the host: the names the local Vaultwarden's certificate and CA cover.

The browser reaches the web vault as https://localhost:<port> and bw as
https://vaultwarden.vault.internal, so the server certificate names both and the
CA's name constraint permits both. A CA made before localhost was added is
refused with a rotate hint; a server certificate without localhost is reissued
from a CA that covers it. Runs on the host with the cryptography package and
skips without it; test-vw-register.py covers the same code in the broker image.
"""

from __future__ import annotations

import datetime
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker" / "setup"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from testkit import expect  # noqa: E402

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:
    print("SKIP: the cryptography package is not installed on this host")
    sys.exit(77)

import tls  # noqa: E402

NAMES = {tls.LOCAL_NAME, "localhost"}


def cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def old_ca(dir: Path) -> None:
    """A CA and server pair as setup.py tls made them before localhost: one name only."""
    server = dir / tls.SERVER_DIR
    server.mkdir(parents=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls.CA_CN)])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(tls.LOCAL_NAME)],
                                              excluded_subtrees=None), critical=True)
          .sign(key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (dir / "ca.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
    (dir / "ca.crt").write_bytes(ca.public_bytes(pem))
    (server / "server.key").write_bytes(b"old key\n")
    (server / "server.crt").write_bytes(b"old crt\n")


def test_fresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dir = Path(tmp) / "tls"
        tls.make_local_tls(dir)
        ca, server = cert(dir / "ca.crt"), cert(dir / "server/server.crt")
        expect(tls._permitted_names(ca) == NAMES, f"CA constraint {tls._permitted_names(ca)}")
        expect(tls._san_names(server) == NAMES, f"server names {tls._san_names(server)}")
        server.verify_directly_issued_by(ca)
        expect(mode(dir / "ca.key") == 0o600 and mode(dir / "server/server.key") == 0o600, "keys are mode 600")
        expect(mode(dir / "ca.crt") == 0o644 and mode(dir / "server/server.crt") == 0o644, "certs are mode 644")
        before = {p: p.read_bytes() for p in dir.rglob("*") if p.is_file()}
        tls.make_local_tls(dir)
        expect({p: p.read_bytes() for p in dir.rglob("*") if p.is_file()} == before, "a second run changed files")


def test_old_ca_refused_until_rotate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dir = Path(tmp) / "tls"
        old_ca(dir)
        before = {p: p.read_bytes() for p in dir.rglob("*") if p.is_file()}
        try:
            tls.make_local_tls(dir)
            expect(False, "a CA without localhost was kept")
        except tls.TlsError as exc:
            expect("localhost" in str(exc) and "rotate" in str(exc), f"refusal says {exc}")
        expect({p: p.read_bytes() for p in dir.rglob("*") if p.is_file()} == before, "a refusal changed files")
        tls.make_local_tls(dir, rotate=True)
        expect(tls._permitted_names(cert(dir / "ca.crt")) == NAMES, "rotate makes a CA for both names")
        expect(tls._san_names(cert(dir / "server/server.crt")) == NAMES, "rotate issues for both names")


def test_server_without_localhost_reissued() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dir = Path(tmp) / "tls"
        tls.make_local_tls(dir)
        ca_before = (dir / "ca.crt").read_bytes()
        # A server certificate for the internal name only, from the same CA.
        ca_key = serialization.load_pem_private_key((dir / "ca.key").read_bytes(), password=None)
        ca = cert(dir / "ca.crt")
        now = datetime.datetime.now(datetime.timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        old = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls.LOCAL_NAME)]))
               .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(tls.LOCAL_NAME)]), critical=False)
               .sign(ca_key, hashes.SHA256()))
        (dir / "server/server.crt").write_bytes(old.public_bytes(serialization.Encoding.PEM))
        tls.make_local_tls(dir)
        expect((dir / "ca.crt").read_bytes() == ca_before, "the CA was replaced")
        expect(tls._san_names(cert(dir / "server/server.crt")) == NAMES, "the server pair was not reissued")


def main() -> int:
    test_fresh()
    test_old_ca_refused_until_rotate()
    test_server_without_localhost_reissued()
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
