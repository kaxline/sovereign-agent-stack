"""TLS material for the local Vaultwarden's sidecar (Ruling R18).

``make_local_tls(dir)`` writes a private CA (``ca.crt``, ``ca.key``) and,
in ``dir/server/``, a server certificate for ``vaultwarden.vault.internal``
and ``vaultwarden.localhost`` signed by it (``server.crt``, ``server.key``).
The sidecar mounts only ``server/``. The broker and bitwarden-setup reach it
by the first name; the user's browser reaches the web vault at
``https://vaultwarden.localhost:<port>``, because the web vault refuses
plain-HTTP servers, and trusts the CA once. A name of its own keeps the vault
out of the cookie jar that every other ``localhost`` app shares.
Keys are EC P-256. The CA is valid for 10 years, is limited by a critical name
constraint to exactly those two DNS names (and their subdomains), and the
server certificate is valid for 825 days. Keys are mode 600 and certificates
mode 644.

A second run changes nothing. A missing server pair, or one whose names differ
from the two, is issued from the existing CA. A CA whose name constraint
permits any other set of names is refused, since only a new CA can change it
and whatever trusts the old one must then trust the new one; ``rotate=True``
replaces all four files. A pair with one file missing is refused, since
replacing the other file would break whatever trusts or serves it.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from broker.atomic import write_atomic

LOCAL_NAME = "vaultwarden.vault.internal"
# The name the user's browser uses for the web vault (127.0.0.1:VAULTWARDEN_PORT).
BROWSER_NAME = "vaultwarden.localhost"
CA_DAYS = 3650
SERVER_DAYS = 825
CA_CN = "Assistant local Vaultwarden CA"
SERVER_DIR = "server"


class TlsError(Exception):
    """A message for the user; nothing was written."""


def _write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same folder, with ``mode``."""
    write_atomic(path, data, mode)


def _pair(dir: Path, stem: str, rotate: bool) -> bool:
    """True when ``<stem>.crt`` and ``<stem>.key`` both exist and stay; raises when only one does."""
    crt, key = dir / f"{stem}.crt", dir / f"{stem}.key"
    if rotate:
        return False
    if crt.exists() and key.exists():
        return True
    if crt.exists() or key.exists():
        present = crt.name if crt.exists() else key.name
        missing = key.name if crt.exists() else crt.name
        raise TlsError(f"{dir / missing} is missing but {present} exists; rotate to replace both.")
    return False


def make_local_tls(dir: Path, name: str = LOCAL_NAME, rotate: bool = False) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    dir = Path(dir)
    server_dir = dir / SERVER_DIR
    for d in (dir, server_dir):
        if d.exists() and not d.is_dir():
            raise TlsError(f"{d} is not a folder.")
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for f in ("ca.crt", "ca.key"):
        if (dir / f).is_dir():
            raise TlsError(f"{dir / f} is a folder; remove it and run again.")
    for f in ("server.crt", "server.key"):
        if (server_dir / f).is_dir():
            raise TlsError(f"{server_dir / f} is a folder; remove it and run again.")
    names = list(dict.fromkeys([name, BROWSER_NAME]))
    keep_ca = _pair(dir, "ca", rotate)
    if keep_ca:
        permitted = _permitted_names(x509.load_pem_x509_certificate((dir / "ca.crt").read_bytes()))
        missing = [n for n in names if n not in permitted]
        extra = sorted(permitted - set(names))
        if missing or extra:
            problem = "; ".join(filter(None, (missing and f"does not cover {', '.join(missing)}",
                                              extra and f"also permits {', '.join(extra)}")))
            raise TlsError(f"The CA in {dir} {problem}; rotate to replace it, "
                           "then trust the new ca.crt in your browser again.")
    keep_server = keep_ca and _pair(server_dir, "server", rotate) \
        and set(names) == _san_names(x509.load_pem_x509_certificate((server_dir / "server.crt").read_bytes()))
    if keep_ca and keep_server:
        return
    if not keep_ca and not rotate and ((server_dir / "server.crt").exists() or (server_dir / "server.key").exists()):
        raise TlsError(f"{server_dir} has a server certificate but {dir} has no CA; rotate to replace both.")

    now = datetime.datetime.now(datetime.timezone.utc)
    pem = serialization.Encoding.PEM

    def key_bytes(key) -> bytes:
        return key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    if keep_ca:
        ca_cert = x509.load_pem_x509_certificate((dir / "ca.crt").read_bytes())
        ca_key = serialization.load_pem_private_key((dir / "ca.key").read_bytes(), password=None)
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_CN)])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name).issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         key_cert_sign=True, crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(n) for n in names],
                                                excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )

    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # Keys first, so a certificate on disk always has its key beside it.
    if not keep_ca:
        _write(dir / "ca.key", key_bytes(ca_key), 0o600)
        _write(dir / "ca.crt", ca_cert.public_bytes(pem), 0o644)
    _write(server_dir / "server.key", key_bytes(server_key), 0o600)
    _write(server_dir / "server.crt", server_cert.public_bytes(pem), 0o644)


def _permitted_names(ca_cert) -> set[str]:
    """The DNS names in the CA's name constraint; empty when it has none."""
    from cryptography import x509
    try:
        ext = ca_cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return set()
    return {t.value for t in ext.permitted_subtrees or [] if isinstance(t, x509.DNSName)}


def _san_names(cert) -> set[str]:
    """The DNS names in the certificate's subject alternative name; empty when it has none."""
    from cryptography import x509
    try:
        return set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                   .get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        return set()


def copy_ca(dir: Path, dest: Path) -> bool:
    """Copy ``dir/ca.crt`` to ``dest`` (mode 644) when it differs; True if it was written."""
    data = (Path(dir) / "ca.crt").read_bytes()
    dest = Path(dest)
    if dest.is_dir():
        raise TlsError(f"{dest} is a folder; remove it and run again.")
    if dest.exists() and dest.read_bytes() == data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(dest, data, 0o644)
    return True
