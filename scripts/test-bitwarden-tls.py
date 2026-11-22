#!/usr/bin/env python3
"""setup/tls.py on the host: the names the local Vaultwarden's certificate and CA cover.

The browser reaches the web vault as https://vaultwarden.localhost:<port> and bw
as https://vaultwarden.vault.internal, so the server certificate names exactly
those two and the CA's name constraint permits exactly those two. A CA that
lacks one (made before the browser name was added) or permits more (made when
the browser name was bare localhost) is refused with a rotate hint; a server
certificate whose names differ is reissued from the CA. Runs on the host with the cryptography package and
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

NAMES = {tls.LOCAL_NAME, "vaultwarden.localhost"}


def cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def old_ca(dir: Path, names: list[str]) -> None:
    """A CA permitting ``names`` and a placeholder server pair, as an older setup.py tls left them."""
    server = dir / tls.SERVER_DIR
    server.mkdir(parents=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls.CA_CN)])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(n) for n in names],
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


def check_refused_until_rotate(names: list[str], says: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dir = Path(tmp) / "tls"
        old_ca(dir, names)
        before = {p: p.read_bytes() for p in dir.rglob("*") if p.is_file()}
        try:
            tls.make_local_tls(dir)
            expect(False, f"a CA for {names} was kept")
        except tls.TlsError as exc:
            expect(says in str(exc) and "rotate" in str(exc), f"refusal says {exc}")
        expect({p: p.read_bytes() for p in dir.rglob("*") if p.is_file()} == before, "a refusal changed files")
        tls.make_local_tls(dir, rotate=True)
        expect(tls._permitted_names(cert(dir / "ca.crt")) == NAMES, "rotate makes a CA for both names")
        expect(tls._san_names(cert(dir / "server/server.crt")) == NAMES, "rotate issues for both names")


def test_ca_missing_a_name_refused() -> None:
    check_refused_until_rotate([tls.LOCAL_NAME], "does not cover vaultwarden.localhost")


def test_ca_with_extra_name_refused() -> None:
    # As 519ac06 made it: bare localhost, which also permits every other *.localhost.
    check_refused_until_rotate([tls.LOCAL_NAME, "localhost"], "also permits localhost")
    check_refused_until_rotate([tls.LOCAL_NAME, "vaultwarden.localhost", "localhost"], "also permits localhost")


def check_server_reissued(sans: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dir = Path(tmp) / "tls"
        tls.make_local_tls(dir)
        ca_before = (dir / "ca.crt").read_bytes()
        # A server certificate for other names, from the same CA.
        ca_key = serialization.load_pem_private_key((dir / "ca.key").read_bytes(), password=None)
        ca = cert(dir / "ca.crt")
        now = datetime.datetime.now(datetime.timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        old = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls.LOCAL_NAME)]))
               .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False)
               .sign(ca_key, hashes.SHA256()))
        (dir / "server/server.crt").write_bytes(old.public_bytes(serialization.Encoding.PEM))
        tls.make_local_tls(dir)
        expect((dir / "ca.crt").read_bytes() == ca_before, "the CA was replaced")
        expect(tls._san_names(cert(dir / "server/server.crt")) == NAMES, "the server pair was not reissued")


def test_server_names_differ_reissued() -> None:
    check_server_reissued([tls.LOCAL_NAME])
    check_server_reissued([tls.LOCAL_NAME, "localhost"])
    check_server_reissued([tls.LOCAL_NAME, "vaultwarden.localhost", "localhost"])


def main() -> int:
    test_fresh()
    test_ca_missing_a_name_refused()
    test_ca_with_extra_name_refused()
    test_server_names_differ_reissued()
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
