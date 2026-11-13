"""Replace held secret values, in the five broker encodings, with a marker.

The encodings are a copy of broker/digest_redaction.py (FORMS and _encode); the
signer image cannot import the broker. scripts/test-bitwarden-signer.py checks
the two stay equal. Each value is also held with CR and LF removed, which is
what a text input keeps.
"""

from __future__ import annotations

import base64
from urllib.parse import quote

MARKER = "«redacted-vault-secret»"
FORMS = ("raw", "base64", "hex", "percent", "reversed")


def _encode(value: str, form: str) -> bytes:
    raw = value.encode("utf-8")
    if form == "raw":
        return raw
    if form == "base64":
        return base64.b64encode(raw)
    if form == "hex":
        return raw.hex().encode("ascii")
    if form == "percent":
        return quote(value, safe="").encode("ascii")
    if form == "reversed":
        return value[::-1].encode("utf-8")
    raise ValueError(form)


class Scrubber:
    def __init__(self) -> None:
        self._patterns: set[bytes] = set()

    def add(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            return
        stripped = value.replace("\r", "").replace("\n", "")
        for source in (value, stripped):
            if source:
                self._patterns.update(_encode(source, f) for f in FORMS)

    def clear(self) -> None:
        self._patterns = set()

    def patterns(self) -> set[bytes]:
        return set(self._patterns)

    def apply(self, text: str) -> str:
        """Replace every occurrence of each held encoding, longest first."""
        if not isinstance(text, str) or not text or not self._patterns:
            return text
        data = text.encode("utf-8", "surrogatepass")
        mark = MARKER.encode("utf-8")
        for pattern in sorted(self._patterns, key=len, reverse=True):
            if pattern in data:
                data = data.replace(pattern, mark)
        return data.decode("utf-8", "surrogatepass")
