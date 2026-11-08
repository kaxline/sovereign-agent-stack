#!/usr/bin/env python3
"""Checks for digest redaction (broker/digest_redaction.py, patch-digest-redaction.py).

Host part is stdlib-only and uses generated passwords. With the pinned Hermes
image present (or --image), the patch is applied in the image twice, an entry
is registered through agent.redact, and the scrub and clear paths are checked
next to core's exact pass.
"""

from __future__ import annotations

import base64
import logging
import random
import secrets
import string
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "bitwarden-broker"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from broker import digest_redaction as dr  # noqa: E402
import hermes_image  # noqa: E402

MARKER = "«redacted-vault-secret»"
MODULE = ROOT / "compose" / "bitwarden-broker" / "broker" / "digest_redaction.py"
RNG = random.Random(20261108)
BASE = dr.new_base()
PRINTABLE = string.ascii_letters + string.digits + string.punctuation + " \n"


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def password(n: int = 24) -> str:
    # The leading "+" keeps the percent form distinct from the raw one.
    return "+" + "".join(secrets.choice(string.ascii_letters + string.digits + "+/%=!") for _ in range(n - 1))


def filler(n: int) -> str:
    return "".join(RNG.choice(PRINTABLE) for _ in range(n))


def encodings(value: str) -> dict[str, str]:
    raw = value.encode("utf-8")
    return {
        "raw": value,
        "base64": base64.b64encode(raw).decode("ascii"),
        "hex": raw.hex(),
        "percent": quote(value, safe=""),
        "reversed": value[::-1],
    }


def check_scrubbed(value: str, entry: dict, size: int, label: str) -> None:
    for form, encoded in encodings(value).items():
        for where in ("start", "middle", "end"):
            body = filler(size)
            at = {"start": 0, "middle": size // 2, "end": size}[where]
            text = body[:at] + encoded + body[at:]
            out = dr.scrub(text, [entry], MARKER)
            expect(out == body[:at] + MARKER + body[at:], f"{label}: {form} at {where} in {size} chars")


def test_entry_shape() -> None:
    expect(dr.FORMS == ("raw", "base64", "hex", "percent", "reversed"), "FORMS")
    entry = dr.build_entry(password(), BASE)
    expect(len(base64.b64decode(entry["key"])) == 32, "32-byte key")
    expect(256 <= entry["base"] < 2**61 - 1, "base range")
    expect(len(entry["forms"]) == 5, f"one form per FORMS item: {len(entry['forms'])}")
    for form in entry["forms"]:
        expect(set(form) == {"len", "fp", "mac"}, f"form keys {set(form)}")
        expect(len(form["mac"]) == 64 and int(form["mac"], 16) >= 0, "hex mac")
    # Both newline forms differ from the raw ones, so ten forms; identical ones are dropped.
    expect(len(dr.build_entry("a+\nb", BASE)["forms"]) == 10, "CR/LF-stripped forms added")
    expect(len(dr.build_entry("a+a", BASE)["forms"]) == 4, "raw and reversed of a palindrome de-duplicated")
    expect(entry["base"] == BASE, "entry carries the base it was built with")
    for bad in (0, 255, 2**61 - 1, "300", 300.0, None):
        try:
            dr.build_entry("x", bad)
        except ValueError:
            continue
        fail(f"build_entry accepted base {bad!r}")


def test_each_form_scrubbed() -> None:
    value = password()
    entry = dr.build_entry(value, BASE)
    for size in (1024, 200 * 1024):
        check_scrubbed(value, entry, size, "generated password")


def test_no_false_positives() -> None:
    text = filler(200 * 1024)
    entries = [dr.build_entry(password(), BASE) for _ in range(10)]
    expect(dr.scrub(text, entries, MARKER) == text, "random text unchanged")


def test_unicode_and_newline_values() -> None:
    for value in ("pässwörd+/%x", "line1\nline2"):
        entry = dr.build_entry(value, BASE)
        check_scrubbed(value, entry, 1024, repr(value))
        stripped = value.replace("\r", "").replace("\n", "")
        if stripped != value:
            check_scrubbed(stripped, entry, 1024, f"{value!r} stripped")
    # Every occurrence goes, and the marker sits where each one was.
    value = "pässwörd+/%x"
    text = f"a{value}b{base64.b64encode(value.encode()).decode()}c{value}"
    expect(dr.scrub(text, [dr.build_entry(value, BASE)], MARKER) == f"a{MARKER}b{MARKER}c{MARKER}", "repeats")


def test_malformed_entries_ignored() -> None:
    value = password()
    good = dr.build_entry(value, BASE)
    junk = [None, 3, "x", {}, {"key": 1}, {"key": good["key"], "base": "7", "forms": []},
            {"key": "!!", "base": 300, "forms": [{"len": 3, "fp": 1, "mac": "zz"}]},
            {"key": good["key"], "base": good["base"], "forms": [None, {"len": -1, "fp": 0, "mac": ""}]}]
    text = f"before {value} after"
    expect(dr.scrub(text, junk + [good], MARKER) == f"before {MARKER} after", "bad entries skipped")
    expect(dr.scrub(text, "not a list", MARKER) == text, "non-list entries")
    expect(dr.scrub("", [good], MARKER) == "", "empty text")
    expect(dr.normalize_entry(good) is not None, "good entry accepted")
    for bad in junk:
        expect(dr.normalize_entry(bad) is None, f"rejected {bad!r:.60}")


def test_overlapping_and_adjacent() -> None:
    # Two entries whose raw forms overlap: "abc+def" and "def+ghi" in "abc+def+ghi".
    a, b = dr.build_entry("abc+def", BASE), dr.build_entry("def+ghi", BASE)
    expect(dr.scrub("x abc+def+ghi y", [a, b], MARKER) == f"x {MARKER} y", "overlapping spans merge")
    value = password()
    entry = dr.build_entry(value, BASE)
    expect(dr.scrub(f"<{value}{value}>", [entry], MARKER) == f"<{MARKER}{MARKER}>", "back-to-back matches")


def test_lone_surrogates() -> None:
    value = "pässwörd+/%x"
    entry = dr.build_entry(value, BASE)
    text = f"\ud800{value}\udfff"
    expect(dr.scrub(text, [entry], MARKER) == f"\ud800{MARKER}\udfff", "lone surrogates kept around a match")


def test_chunk_boundary() -> None:
    value = password()
    entry = dr.build_entry(value, BASE)
    for form, encoded in encodings(value).items():
        for offset in (1, len(encoded) // 2, len(encoded) - 1):
            at = dr.CHUNK_BYTES - offset
            body = "a" * (3 * dr.CHUNK_BYTES)
            text = body[:at] + encoded + body[at:]
            out = dr.scrub(text, [entry], MARKER)
            expect(out == body[:at] + MARKER + body[at:], f"{form} straddling a chunk boundary at -{offset}")
    # A match that starts exactly on the boundary is counted once.
    text = "a" * dr.CHUNK_BYTES + value + "a" * 10
    expect(dr.scrub(text, [entry], MARKER) == "a" * dr.CHUNK_BYTES + MARKER + "a" * 10, "match at boundary")


def test_fails_closed() -> None:
    value = password()
    entry = dr.build_entry(value, BASE)
    text = f"before {value} after"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    logging.getLogger(dr.__name__).addHandler(handler)
    original = dr._spans

    def broken(*args):
        raise RuntimeError(f"boom {value}")

    dr._spans = broken
    try:
        out = dr.scrub(text, [entry], MARKER)
        out_table = dr.scrub_table(text, BASE, dr.build_table([dr.normalize_entry(entry)]), MARKER)
    finally:
        dr._spans = original
        logging.getLogger(dr.__name__).removeHandler(handler)
    expect(out == dr.FAILED == "«redaction failed»" and out_table == dr.FAILED, f"fixed output: {out!r:.40}")
    expect(len(records) == 2, f"one log line per failure: {len(records)}")
    expect(all(value not in r and "boom" not in r and "before" not in r for r in records), "log holds no text")


def timed(fn, repeat_count: int = 1) -> float:
    start = time.perf_counter()
    for _ in range(repeat_count):
        fn()
    return (time.perf_counter() - start) * 1000 / repeat_count


def test_timing() -> None:
    ten = [dr.build_entry(password(), BASE) for _ in range(10)]
    # Mixed lengths, as real vaults have, so the table has many distinct form lengths.
    full = [dr.build_entry(password(RNG.randint(12, 40)), BASE) for _ in range(64)]
    table = dr.build_table([dr.normalize_entry(e) for e in full])
    big, mid, short = filler(200 * 1024), filler(100 * 1024), filler(60)
    print(f"timing: 200 kB, 10 entries: {timed(lambda: dr.scrub(big, ten, MARKER)):.0f} ms")
    print(f"timing: 100 kB, 64 entries: {timed(lambda: dr.scrub_table(mid, BASE, table, MARKER)):.0f} ms")
    print(f"timing: 60 chars, 64 entries: {timed(lambda: dr.scrub_table(short, BASE, table, MARKER), 200):.3f} ms per call")


def test_overlay_is_wired() -> None:
    runner = (ROOT / "compose" / "hermes" / "05-patch-tool-calling.sh").read_text()
    expect("run_patch /bootstrap/patch-digest-redaction.py" in runner, "cont-init runs the patch")
    compose = (ROOT / "docker-compose.yml").read_text()
    expect("./compose/bitwarden-broker/broker/digest_redaction.py:/bootstrap/digest_redaction.py:ro" in compose,
           "module is mounted")
    expect("./compose/hermes/patch-digest-redaction.py:/bootstrap/patch-digest-redaction.py:ro" in compose,
           "patch is mounted")


# The module is passed on stdin: hermes_image mounts only compose/hermes at /bootstrap.
IMAGE_DRIVER = r'''
import base64, os, subprocess, sys
os.makedirs("/tmp/dr", exist_ok=True)
with open("/tmp/dr/digest_redaction.py", "w") as fh:
    fh.write(sys.stdin.read())
env = dict(os.environ, DIGEST_REDACTION_BOOTSTRAP="/tmp/dr")
for _ in range(2):
    out = subprocess.run([sys.executable, "/bootstrap/patch-digest-redaction.py"], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
assert "already applied" in out.stdout, out.stdout
src = open("/opt/hermes/agent/redact.py").read()
assert src.count("# assistant-stack: digest redaction") == 1, "one registry block"

sys.path.insert(0, "/opt/hermes")
from agent import redact
from agent.digest_redaction import build_entry

value = "pässwörd+/%x"
b64 = base64.b64encode(value.encode()).decode()
text = f"field={b64};"
assert redact.redact_registered_vault_values(text) == text, "nothing registered yet"
base = redact.vault_redaction_base()
assert base == redact.vault_redaction_base(), "one base per profile"
other = base + 1 if base + 1 < 2**61 - 1 else base - 1
redact.register_vault_redaction_digest(build_entry(value, other))  # wrong base: dropped
assert redact.redact_registered_vault_values(text) == text, "entry with another base dropped"
entry = build_entry(value, base)
redact.register_vault_redaction_digest(entry)
entry["forms"].clear()  # the stored copy is the normalized one
redact.register_vault_redaction_digest({"key": "junk"})  # dropped, no exception
redact.register_vault_redaction_digest(None)
assert redact.redact_registered_vault_values(text) == "field=«redacted-vault-secret»;", "base64 scrubbed"
assert redact.redact_sensitive_text(text) == "field=«redacted-vault-secret»;", "through redact_sensitive_text"
redact.clear_vault_redaction_values()
assert redact.redact_registered_vault_values(text) == text, "clear drops digests"
assert redact.vault_redaction_base() is not None and redact._VAULT_REDACTION_BASES, "base recreated lazily"

redact.register_vault_redaction_value("Plain-Fill-Value-42")
assert redact.redact_registered_vault_values("x Plain-Fill-Value-42 y") == "x «redacted-vault-secret» y", "exact pass"
redact.clear_vault_redaction_values()

for _ in range(redact._VAULT_REDACTION_MAX_PER_PROFILE + 5):
    redact.register_vault_redaction_digest(build_entry(os.urandom(12).hex(), redact.vault_redaction_base()))
bucket = redact._VAULT_REDACTION_DIGESTS[redact._vault_scope()]
assert len(bucket) == redact._VAULT_REDACTION_MAX_PER_PROFILE, len(bucket)
redact.clear_vault_redaction_values()
os.write(1, b"image ok\n")
'''


def main() -> None:
    test_entry_shape()
    test_each_form_scrubbed()
    test_no_false_positives()
    test_unicode_and_newline_values()
    test_malformed_entries_ignored()
    test_timing()
    test_overlay_is_wired()
    print("ok")
    hermes_image.maybe_run(IMAGE_DRIVER, stdin=MODULE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
