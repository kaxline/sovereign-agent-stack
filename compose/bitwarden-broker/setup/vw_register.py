"""Bitwarden and Vaultwarden calls that bw has no command for.

``token`` signs in with the password grant and returns an access token for the
organization API. The master key is PBKDF2-SHA256 over the password with the
lower-cased email as salt, at the iterations prelogin reports; the hash sent is
one more PBKDF2-SHA256 round of that key over the password.

Local mode (spike 2) adds:

``admin_invite``   Signs in to Vaultwarden's admin page with the plaintext admin
                   token and invites an address to the instance, so it can
                   register although sign-ups are off.
``register``       Client-side registration through the send-verification-email
                   and register/finish endpoints. With no SMTP, Vaultwarden
                   returns the verification token directly.
``create_org``     Creates an organization with its first collection.
``api_key``        Fetches the account's personal API key for ``bw login --apikey``.
``invite_member``  Invites an address to an organization; ``api`` is the generic call.

Crypto follows the Bitwarden clients: the master key is stretched with
HKDF-Expand SHA-256 (``enc`` and ``mac``); EncStrings are type 2 (AES-256-CBC
with HMAC-SHA256); key pairs are RSA-2048, public SPKI and private PKCS#8; the
organization key is wrapped for its creator as type 4 (RSA-OAEP with SHA-1).
That part needs ``cryptography`` and imports it on first use, so ``token``
stays stdlib-only.

Every call goes through ``opener()``, which refuses redirects. No password,
hash, key, token or cookie is written anywhere or put in an exception; errors
carry a fixed reason and the HTTP status.

TLS trusts the system CAs plus ``NODE_EXTRA_CA_CERTS`` when that file exists,
the same CA bw gets in local mode.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 1024 * 1024
KDF_PBKDF2 = 0
KDF_ARGON2ID = 1
REGISTER_ITERATIONS = 600_000
DEVICE_TYPE_LINUX_CLI = "25"
PLAN_FREE = 0
ADMIN_COOKIE = "VW_ADMIN"


class VwError(Exception):
    """A refused or failed call. ``failure`` is a fixed reason and ``status`` the HTTP status, if any."""

    def __init__(self, failure: str, status: int | None = None, what: str | None = None):
        super().__init__(f"{what}: {failure}" if what else failure)
        self.failure = failure
        self.status = status


class TokenError(VwError):
    """The server refused the grant or could not be reached."""


@dataclass(frozen=True)
class Keys:
    """What ``register`` made: the 64-byte user key (encryption then MAC half) and the SPKI public key."""

    user_key: bytes = field(repr=False)
    public_key: bytes = field(repr=False)


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra = os.environ.get("NODE_EXTRA_CA_CERTS")
    if extra and os.path.isfile(extra):
        ctx.load_verify_locations(cafile=extra)
    return ctx


# bitwarden.com and .eu serve identity and the API from their own hosts.
_CLOUD = {
    "https://vault.bitwarden.com": ("https://identity.bitwarden.com", "https://api.bitwarden.com"),
    "https://vault.bitwarden.eu": ("https://identity.bitwarden.eu", "https://api.bitwarden.eu"),
}


def endpoints(base: str) -> tuple[str, str]:
    """``(identity, api)`` base URLs for a server URL."""
    base = base.rstrip("/")
    return _CLOUD.get(base, (f"{base}/identity", f"{base}/api"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turns any redirect into an HTTPError, so a body or Authorization header is never sent twice."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl_context()))


def _call(method: str, url: str, *, body=None, form: dict | None = None, token: str | None = None,
          cookie: str | None = None, what: str | None = None, error: type[VwError] = VwError):
    """Send one request; return the JSON response, its text when it is not JSON, or None when empty."""
    headers = {"Accept": "application/json", "Device-Type": DEVICE_TYPE_LINUX_CLI}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    failure = status = raw = None
    try:
        with opener().open(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        status = exc.code
        failure = "redirect refused" if 300 <= exc.code < 400 else f"refused (HTTP {exc.code})"
        exc.close()
    except (urllib.error.URLError, OSError):
        failure = "unreachable"
    finally:
        req = data = None
        headers.clear()
    # Raised outside the except blocks so no chained exception carries the request.
    if failure is not None:
        raise error(failure, status, what)
    text = raw.decode("utf-8", "replace") if raw else ""
    raw = None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def api(base: str, token: str, method: str, path: str, body=None):
    """Call the API at ``base`` with the bearer ``token``; raises VwError naming the path's status."""
    _, api_base = endpoints(base)
    return _call(method, f"{api_base}{path}", body=body, token=token, what=f"{method} {path.split('?')[0]}")


# Keys and hashes

def _master_key(email: str, password: str, iterations: int) -> bytes:
    salt = email.strip().lower().encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)


def master_password_hash(email: str, password: str, iterations: int) -> str:
    key = _master_key(email, password, iterations)
    try:
        return base64.b64encode(hashlib.pbkdf2_hmac("sha256", key, password.encode("utf-8"), 1, 32)).decode("ascii")
    finally:
        key = None


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _stretch(master_key: bytes) -> bytes:
    return _hkdf_expand(master_key, b"enc", 32) + _hkdf_expand(master_key, b"mac", 32)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def enc_string(data: bytes, key: bytes) -> str:
    """A type-2 EncString: AES-256-CBC under ``key[:32]``, HMAC-SHA256 under ``key[32:]``."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key[:32]), modes.CBC(iv)).encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    mac = hmac.new(key[32:], iv + ct, hashlib.sha256).digest()
    return f"2.{_b64(iv)}|{_b64(ct)}|{_b64(mac)}"


def _key_pair() -> tuple[bytes, bytes]:
    """A new RSA-2048 pair as ``(SPKI DER, PKCS#8 DER)``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_der = private.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    private_der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    return public_der, private_der


def _wrap_for(public_der: bytes, key: bytes) -> str:
    """``key`` encrypted to an SPKI public key as a type-4 EncString (RSA-OAEP, SHA-1)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public = serialization.load_der_public_key(public_der)
    return "4." + _b64(public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()),
                                                        algorithm=hashes.SHA1(), label=None)))


def _claims(access_token: str) -> dict:
    """The JWT's payload, unverified; {} if it is not a JWT."""
    try:
        part = access_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# Identity

def _prelogin(base: str, email: str) -> int:
    identity, _ = endpoints(base)
    pre = _call("POST", f"{identity}/accounts/prelogin", body={"email": email}, error=TokenError)
    if not isinstance(pre, dict):
        raise TokenError("not JSON")
    kdf = pre.get("kdf", pre.get("Kdf"))
    iterations = pre.get("kdfIterations", pre.get("KdfIterations"))
    if kdf == KDF_ARGON2ID:
        raise TokenError("the account uses Argon2id, which this setup cannot compute")
    if kdf != KDF_PBKDF2:
        raise TokenError("prelogin reported an unknown KDF")
    if not isinstance(iterations, int) or iterations < 1:
        raise TokenError("prelogin returned no iteration count")
    return iterations


def token(base: str, email: str, password: str) -> str:
    """Return an access token for ``email`` on the server at ``base``, or raise TokenError."""
    identity, _ = endpoints(base)
    iterations = _prelogin(base, email)
    form = {
        "grant_type": "password", "username": email,
        "password": master_password_hash(email, password, iterations),
        # Vaultwarden refuses any other scope ("Scope (api) not supported"). The refresh
        # token that offline_access adds is dropped with the rest of the response.
        "scope": "api offline_access", "client_id": "cli",
        "deviceType": DEVICE_TYPE_LINUX_CLI, "deviceName": "linux",
        "deviceIdentifier": str(uuid.uuid4()),
    }
    password = None
    try:
        data = _call("POST", f"{identity}/connect/token", form=form, error=TokenError)
    finally:
        form.clear()
    if not isinstance(data, dict):
        raise TokenError("not JSON")
    access = data.get("access_token")
    data.clear()
    if not isinstance(access, str) or not access:
        raise TokenError("no access token in the response")
    return access


def register(base: str, email: str, password: str) -> Keys:
    """Register ``email`` with a new user key and RSA pair, PBKDF2 at 600,000 iterations."""
    identity, _ = endpoints(base)
    master = _master_key(email, password, REGISTER_ITERATIONS)
    stretched = _stretch(master)
    user_key = os.urandom(64)
    public_der, private_der = _key_pair()
    body = None
    try:
        verify = _call("POST", f"{identity}/accounts/register/send-verification-email",
                       body={"email": email, "name": email.split("@")[0]}, what="registration")
        if not isinstance(verify, str) or not verify:
            raise VwError("no verification token in the response; Vaultwarden returns it only with no SMTP",
                          what="registration")
        body = {
            "email": email, "emailVerificationToken": verify,
            "kdf": KDF_PBKDF2, "kdfIterations": REGISTER_ITERATIONS,
            "masterPasswordHash": master_password_hash(email, password, REGISTER_ITERATIONS),
            "userSymmetricKey": enc_string(user_key, stretched),
            "userAsymmetricKeys": {"publicKey": _b64(public_der),
                                   "encryptedPrivateKey": enc_string(private_der, user_key)},
        }
        verify = None
        _call("POST", f"{identity}/accounts/register/finish", body=body, what="registration")
    finally:
        password = master = stretched = private_der = verify = None
        if body is not None:
            body.clear()
    return Keys(user_key=user_key, public_key=public_der)


def api_key(base: str, token: str, email: str, password: str) -> tuple[str, str]:
    """``(client_id, client_secret)`` of the account's personal API key."""
    iterations = _prelogin(base, email)
    body = {"masterPasswordHash": master_password_hash(email, password, iterations)}
    password = None
    try:
        data = api(base, token, "POST", "/accounts/api-key", body)
    finally:
        body.clear()
    secret = data.get("apiKey") if isinstance(data, dict) else None
    user_id = _claims(token).get("sub")
    if isinstance(data, dict):
        data.clear()
    if not isinstance(secret, str) or not secret or not isinstance(user_id, str) or not user_id:
        raise VwError("no API key in the response", what="API key")
    return f"user.{user_id}", secret


def public_key(base: str, token: str) -> Keys:
    """The token's account's public key, for ``create_org`` when ``register`` ran in an earlier run.

    ``create_org`` wraps the organization key with the public key only, so
    ``user_key`` is left empty.
    """
    user_id = _claims(token).get("sub")
    if not isinstance(user_id, str) or not user_id:
        raise VwError("the access token names no user", what="public key")
    data = api(base, token, "GET", f"/users/{urllib.parse.quote(user_id, safe='')}/public-key")
    value = data.get("publicKey") if isinstance(data, dict) else None
    try:
        der = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except ValueError:
        der = b""
    if not der:
        raise VwError("no public key in the response", what="public key")
    return Keys(user_key=b"", public_key=der)


# Organizations

def create_org(base: str, token: str, keys: Keys, name: str, first_collection: str) -> tuple[str, str]:
    """Create a Free organization owned by the token's account; ``(org_id, collection_id)``."""
    billing = _claims(token).get("email")
    if not isinstance(billing, str) or not billing:
        raise VwError("the access token names no email", what="organization")
    org_key = os.urandom(64)
    public_der, private_der = _key_pair()
    body = {
        "name": name, "billingEmail": billing, "planType": PLAN_FREE,
        "collectionName": enc_string(first_collection.encode("utf-8"), org_key),
        "key": _wrap_for(keys.public_key, org_key),
        "keys": {"publicKey": _b64(public_der), "encryptedPrivateKey": enc_string(private_der, org_key)},
    }
    org_key = private_der = None
    try:
        created = api(base, token, "POST", "/organizations", body)
    finally:
        body.clear()
    org_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(org_id, str) or not org_id:
        raise VwError("no organization id in the response", what="organization")
    # The first collection is the only one; its name is encrypted, so it is found by count.
    listed = api(base, token, "GET", f"/organizations/{org_id}/collections")
    rows = listed.get("data") if isinstance(listed, dict) else None
    ids = [c.get("id") for c in rows or [] if isinstance(c, dict)]
    if len(ids) != 1 or not isinstance(ids[0], str):
        raise VwError(f"expected one collection, found {len(ids)}", what="organization")
    return org_id, ids[0]


def invite_member(base: str, token: str, org_id: str, email: str, type: int, collections: list[dict]) -> None:
    """Invite ``email`` to the organization with member ``type`` (0 Owner, 1 Admin, 2 User)."""
    api(base, token, "POST", f"/organizations/{org_id}/users/invite",
        {"emails": [email], "groups": [], "type": type, "collections": collections, "permissions": {}})


# Vaultwarden admin

def _admin_login(base: str, admin_token: str) -> str:
    """The admin session cookie for the plaintext ``admin_token``."""
    form = urllib.parse.urlencode({"token": admin_token}).encode("utf-8")
    admin_token = None
    req = urllib.request.Request(f"{base}/admin", data=form, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    status = failure = headers = None
    try:
        with opener().open(req, timeout=TIMEOUT_SECONDS) as resp:
            status, headers = resp.status, resp.headers
            resp.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        # Success is a redirect to the admin page; the cookie comes with it and the redirect is not followed.
        status, headers = exc.code, exc.headers
        exc.close()
    except (urllib.error.URLError, OSError):
        failure = "unreachable"
    finally:
        req = form = None
    cookie = None
    for value in (headers.get_all("Set-Cookie") or []) if headers is not None else []:
        pair = value.split(";", 1)[0].strip()
        if pair.startswith(f"{ADMIN_COOKIE}=") and len(pair) > len(ADMIN_COOKIE) + 1:
            cookie = pair
    headers = None
    if cookie is None:
        raise VwError(failure or f"refused (HTTP {status})", status, "admin login")
    return cookie


def admin_invite(base: str, admin_token: str, email: str) -> None:
    """Invite ``email`` to the Vaultwarden instance through its admin page."""
    base = base.rstrip("/")
    cookie = _admin_login(base, admin_token)
    admin_token = None
    try:
        _call("POST", f"{base}/admin/invite", body={"email": email}, cookie=cookie, what="admin invite")
    finally:
        cookie = None
