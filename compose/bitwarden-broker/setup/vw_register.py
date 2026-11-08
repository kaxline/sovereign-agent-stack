"""Bitwarden identity calls that bw has no command for. Python stdlib only.

``token`` signs in with the password grant and returns an access token for the
organization API. The master key is PBKDF2-SHA256 over the password with the
lower-cased email as salt, at the iterations prelogin reports; the hash sent is
one more PBKDF2-SHA256 round of that key over the password. Neither the
password, the hash nor the token is written anywhere or put in an exception.

TLS trusts the system CAs plus ``NODE_EXTRA_CA_CERTS`` when that file exists,
the same CA bw gets in local mode.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

TIMEOUT_SECONDS = 30
KDF_PBKDF2 = 0
KDF_ARGON2ID = 1
DEVICE_TYPE_LINUX_CLI = "25"


class TokenError(Exception):
    """The server refused the grant or could not be reached. The text is a fixed reason, never a value."""


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


def _post(url: str, body: bytes, content_type: str) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type, "Accept": "application/json", "Device-Type": DEVICE_TYPE_LINUX_CLI})
    failure = None
    data = None
    try:
        with opener().open(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read(1024 * 1024) or b"{}")
    except urllib.error.HTTPError as exc:
        failure = "redirect refused" if 300 <= exc.code < 400 else f"refused (HTTP {exc.code})"
        exc.close()
    except (urllib.error.URLError, OSError, ValueError):
        failure = "unreachable or not JSON"
    # Raised outside the except blocks so no chained exception carries the request.
    if failure is not None:
        raise TokenError(failure)
    if not isinstance(data, dict):
        raise TokenError("unexpected response")
    return data


def master_password_hash(email: str, password: str, iterations: int) -> str:
    salt = email.strip().lower().encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)
    try:
        return base64.b64encode(hashlib.pbkdf2_hmac("sha256", key, password.encode("utf-8"), 1, 32)).decode("ascii")
    finally:
        key = None


def token(base: str, email: str, password: str) -> str:
    """Return an access token for ``email`` on the server at ``base``, or raise TokenError."""
    identity, _ = endpoints(base)
    pre = _post(f"{identity}/accounts/prelogin", json.dumps({"email": email}).encode("utf-8"),
                "application/json")
    kdf = pre.get("kdf", pre.get("Kdf"))
    iterations = pre.get("kdfIterations", pre.get("KdfIterations"))
    if kdf == KDF_ARGON2ID:
        raise TokenError("the account uses Argon2id, which this setup cannot compute")
    if kdf != KDF_PBKDF2:
        raise TokenError("prelogin reported an unknown KDF")
    if not isinstance(iterations, int) or iterations < 1:
        raise TokenError("prelogin returned no iteration count")
    form = {
        "grant_type": "password", "username": email,
        "password": master_password_hash(email, password, iterations),
        "scope": "api", "client_id": "cli",
        "deviceType": DEVICE_TYPE_LINUX_CLI, "deviceName": "linux",
        "deviceIdentifier": str(uuid.uuid4()),
    }
    password = None
    body = urllib.parse.urlencode(form).encode("utf-8")
    form.clear()
    try:
        data = _post(f"{identity}/connect/token", body, "application/x-www-form-urlencoded")
    finally:
        body = None
    access = data.get("access_token")
    data.clear()
    if not isinstance(access, str) or not access:
        raise TokenError("no access token in the response")
    return access
