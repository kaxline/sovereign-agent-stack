"""Scrub a filled value from text using only digests of it.

The broker fills passwords into pages itself, so the Hermes gateway never holds
them. For each fill the broker calls ``build_entry``: a fresh 32-byte HMAC key,
a random Rabin-Karp base, and for each encoding of the value (and of its
CR/LF-stripped form, which is what a text input keeps) the encoding's byte
length, its fingerprint and its HMAC-SHA256. The gateway calls ``scrub``: it
slides each fingerprint over the UTF-8 bytes of the text, computes the HMAC
only where a fingerprint matches, and replaces windows whose HMAC matches.

Every encoding is a whole UTF-8 sequence, so a matched window starts and ends
on character boundaries and the result decodes cleanly.

Stdlib only, and no package-relative imports: patch-digest-redaction.py copies
this file into Hermes as agent/digest_redaction.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from itertools import accumulate, repeat
from urllib.parse import quote

FORMS = ("raw", "base64", "hex", "percent", "reversed")
MODULUS = 2**61 - 1
KEY_BYTES = 32


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


def fingerprint(window: bytes, base: int) -> int:
    """``sum(w[i] * base**(n-1-i)) mod MODULUS`` over the bytes of ``window``."""
    h = 0
    for b in window:
        h = (h * base + b) % MODULUS
    return h


def build_entry(value: str) -> dict:
    """Digests of ``value`` and its CR/LF-stripped form, one per FORMS item each.

    Identical encodings appear once, so an entry holds between one and ten forms.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("value must be a non-empty string")
    key = secrets.token_bytes(KEY_BYTES)
    base = secrets.randbelow(MODULUS - 256) + 256
    seen: set[bytes] = set()
    forms = []
    stripped = value.replace("\r", "").replace("\n", "")
    for source in dict.fromkeys(v for v in (value, stripped) if v):
        for form in FORMS:
            encoded = _encode(source, form)
            if encoded in seen:
                continue
            seen.add(encoded)
            forms.append({
                "len": len(encoded),
                "fp": fingerprint(encoded, base),
                "mac": hmac.new(key, encoded, hashlib.sha256).hexdigest(),
            })
    return {"key": base64.b64encode(key).decode("ascii"), "base": base, "forms": forms}


def normalize_entry(entry) -> tuple[bytes, int, dict[int, dict[int, list[str]]]] | None:
    """Return ``(key, base, {len: {fp: [mac, ...]}})`` for a well-formed entry, else None."""
    try:
        if not isinstance(entry, dict):
            return None
        key_b64, base, forms = entry["key"], entry["base"], entry["forms"]
        if not isinstance(key_b64, str) or type(base) is not int or not isinstance(forms, list):
            return None
        key = base64.b64decode(key_b64.encode("ascii"), validate=True)
        if len(key) != KEY_BYTES or not 256 <= base < MODULUS or not forms:
            return None
        by_len: dict[int, dict[int, list[str]]] = {}
        for form in forms:
            if not isinstance(form, dict):
                return None
            n, fp, mac = form["len"], form["fp"], form["mac"]
            if type(n) is not int or type(fp) is not int or not isinstance(mac, str):
                return None
            if n <= 0 or not 0 <= fp < MODULUS or len(mac) != 64:
                return None
            int(mac, 16)
            by_len.setdefault(n, {}).setdefault(fp, []).append(mac.lower())
        return key, base, by_len
    except Exception:
        return None


def _matches(data: bytes, key: bytes, base: int, by_len: dict) -> list[tuple[int, int]]:
    """Byte spans of ``data`` whose fingerprint and HMAC match a form of one entry."""
    size = len(data)
    if not any(n <= size for n in by_len):
        return []
    m = MODULUS
    # prefix[i] is the fingerprint of data[:i]; the window data[i:i+n] has
    # fingerprint (prefix[i+n] - prefix[i] * base**n) mod MODULUS.
    prefix = list(accumulate(data, lambda h, b: (h * base + b) % m, initial=0))
    spans = []
    for n, fps in by_len.items():
        if n > size:
            continue
        shift = pow(base, n, m)
        # The map chain runs in C. Most texts hold no candidate window, so the
        # index lookup below only runs when the set intersection is non-empty.
        windows = list(map(int.__mod__, map(int.__sub__, prefix[n:], map(shift.__mul__, prefix)), repeat(m)))
        if fps.keys().isdisjoint(windows):
            continue
        for i, fp in enumerate(windows):
            if fp not in fps:
                continue
            mac = hmac.new(key, data[i:i + n], hashlib.sha256).hexdigest()
            if any(hmac.compare_digest(mac, want) for want in fps[fp]):
                spans.append((i, i + n))
    return spans


def scrub(text: str, entries: list[dict], marker: str) -> str:
    """Replace every window of ``text`` that matches a form in ``entries`` with ``marker``.

    Malformed entries are skipped. Any other failure returns ``text`` unchanged,
    because this runs on every tool result and must not raise.
    """
    if not isinstance(text, str) or not text or not isinstance(entries, list) or not entries:
        return text
    try:
        data = text.encode("utf-8", "surrogatepass")
        spans = []
        for entry in entries:
            parsed = normalize_entry(entry)
            if parsed is not None:
                spans.extend(_matches(data, *parsed))
        if not spans:
            return text
        spans.sort()
        merged = [list(spans[0])]
        for start, end in spans[1:]:
            if start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        mark = marker.encode("utf-8", "surrogatepass")
        out, pos = [], 0
        for start, end in merged:
            out.append(data[pos:start])
            out.append(mark)
            pos = end
        out.append(data[pos:])
        return b"".join(out).decode("utf-8", "surrogatepass")
    except Exception:
        return text
