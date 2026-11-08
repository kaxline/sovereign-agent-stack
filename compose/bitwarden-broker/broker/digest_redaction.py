"""Scrub a filled value from text using only digests of it.

The broker fills passwords into pages itself, so the Hermes gateway never holds
them. For each fill the broker calls ``build_entry`` with the profile's
Rabin-Karp base (the gateway creates it, one per profile): a fresh 32-byte HMAC
key and, for each encoding of the value (and of its CR/LF-stripped form, which
is what a text input keeps), the encoding's byte length, its fingerprint and
its HMAC-SHA256. The gateway scrubs with ``scrub`` or, holding entries already
normalized, with ``build_table`` and ``scrub_table``: one prefix-fingerprint
pass over the UTF-8 bytes of the text, one window pass per distinct form
length, and an HMAC only where a window's fingerprint is in the table.

Every encoding is a whole UTF-8 sequence, so a matched window starts and ends
on character boundaries and the result decodes cleanly.

Stdlib only, and no package-relative imports: patch-digest-redaction.py copies
this file into Hermes as agent/digest_redaction.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from itertools import accumulate, repeat
from urllib.parse import quote

FORMS = ("raw", "base64", "hex", "percent", "reversed")
MODULUS = 2**61 - 1
KEY_BYTES = 32
CHUNK_BYTES = 64 * 1024
FAILED = "«redaction failed»"

logger = logging.getLogger(__name__)


def new_base() -> int:
    return secrets.randbelow(MODULUS - 256) + 256


def _valid_base(base) -> bool:
    return type(base) is int and 256 <= base < MODULUS


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


def build_entry(value: str, base: int) -> dict:
    """Digests of ``value`` and its CR/LF-stripped form, one per FORMS item each.

    ``base`` is the profile's base from the gateway. Identical encodings appear
    once, so an entry holds between one and ten forms.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("value must be a non-empty string")
    if not _valid_base(base):
        raise ValueError("base must be an int with 256 <= base < 2**61 - 1")
    key = secrets.token_bytes(KEY_BYTES)
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


def normalize_entry(entry) -> tuple | None:
    """Return ``(key, base, ((len, fp, mac), ...))`` for a well-formed entry, else None.

    The result holds only immutable values, so a caller keeping it is not
    affected by later changes to ``entry``.
    """
    try:
        if not isinstance(entry, dict):
            return None
        key_b64, base, forms = entry["key"], entry["base"], entry["forms"]
        if not isinstance(key_b64, str) or not _valid_base(base) or not isinstance(forms, list) or not forms:
            return None
        key = base64.b64decode(key_b64.encode("ascii"), validate=True)
        if len(key) != KEY_BYTES:
            return None
        out = []
        for form in forms:
            if not isinstance(form, dict):
                return None
            n, fp, mac = form["len"], form["fp"], form["mac"]
            if type(n) is not int or type(fp) is not int or not isinstance(mac, str):
                return None
            if n <= 0 or not 0 <= fp < MODULUS or len(mac) != 64:
                return None
            int(mac, 16)
            out.append((n, fp, mac.lower()))
        return key, base, tuple(out)
    except Exception:
        return None


def build_table(normalized) -> dict[int, dict[int, list[tuple[bytes, str]]]]:
    """``{len: {fp: [(key, mac), ...]}}`` over normalized entries that share one base."""
    table: dict[int, dict[int, list[tuple[bytes, str]]]] = {}
    for key, _base, forms in normalized:
        for n, fp, mac in forms:
            table.setdefault(n, {}).setdefault(fp, []).append((key, mac))
    return table


def _windows(prefix: list[int], n: int, count: int, shift: int):
    """Fingerprints of the first ``count`` windows of length ``n``, computed in C."""
    return map(int.__mod__, map(int.__sub__, prefix[n:n + count], map(shift.__mul__, prefix[:count])),
               repeat(MODULUS))


def _spans(data: bytes, base: int, table: dict) -> list[tuple[int, int]]:
    """Byte spans of ``data`` whose fingerprint and HMAC match a form in ``table``.

    Chunks of CHUNK_BYTES overlap by the longest form length minus one. A
    window is counted only in the chunk where it starts, so a match across a
    boundary is found once.
    """
    size = len(data)
    lengths = [n for n in table if n <= size]
    if not lengths:
        return []
    m = MODULUS
    overlap = max(lengths) - 1
    shifts = {n: pow(base, n, m) for n in lengths}
    spans = []
    for start in range(0, size, CHUNK_BYTES):
        chunk = data[start:start + CHUNK_BYTES + overlap]
        # prefix[i] is the fingerprint of chunk[:i]; the window chunk[i:i+n]
        # has fingerprint (prefix[i+n] - prefix[i] * base**n) mod MODULUS.
        prefix = list(accumulate(chunk, lambda h, b: (h * base + b) % m, initial=0))
        for n in lengths:
            count = min(CHUNK_BYTES, len(chunk) - n + 1)
            if count <= 0:
                continue
            fps = table[n]
            # The map chain runs in C. Most texts hold no candidate window, so
            # the indexed pass below runs only when a fingerprint is present.
            if fps.keys().isdisjoint(_windows(prefix, n, count, shifts[n])):
                continue
            for i, fp in enumerate(_windows(prefix, n, count, shifts[n])):
                wanted = fps.get(fp)
                if not wanted:
                    continue
                window = chunk[i:i + n]
                for key, mac in wanted:
                    if hmac.compare_digest(hmac.new(key, window, hashlib.sha256).hexdigest(), mac):
                        spans.append((start + i, start + i + n))
                        break
    return spans


def _replace(data: bytes, spans: list[tuple[int, int]], marker: str) -> str:
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


def _failed() -> str:
    logger.error("digest redaction failed; tool output withheld")
    return FAILED


def scrub_table(text: str, base: int, table: dict, marker: str) -> str:
    """Replace every window of ``text`` that matches ``table`` with ``marker``.

    Fails closed: on any internal error the whole text is replaced by FAILED.
    """
    if not isinstance(text, str) or not text or not table:
        return text
    try:
        data = text.encode("utf-8", "surrogatepass")
        spans = _spans(data, base, table)
        return _replace(data, spans, marker) if spans else text
    except Exception:
        return _failed()


def scrub(text: str, entries: list[dict], marker: str) -> str:
    """Replace every window of ``text`` that matches a form in ``entries`` with ``marker``.

    Malformed entries are skipped. Entries with different bases are scanned in
    one pass per base. Fails closed like ``scrub_table``.
    """
    if not isinstance(text, str) or not text or not isinstance(entries, list) or not entries:
        return text
    try:
        by_base: dict[int, list[tuple]] = {}
        for entry in entries:
            parsed = normalize_entry(entry)
            if parsed is not None:
                by_base.setdefault(parsed[1], []).append(parsed)
        for base, group in by_base.items():
            text = scrub_table(text, base, build_table(group), marker)
            if text == FAILED:
                break
        return text
    except Exception:
        return _failed()
