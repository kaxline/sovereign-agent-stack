#!/usr/bin/env python3
"""Checks for digest redaction (broker/digest_redaction.py, patch-digest-redaction.py).

Host part is stdlib-only and uses generated passwords. With the pinned Hermes
image present (or --image), the patch is applied in the image twice, an entry
is registered through agent.redact, and the scrub and clear paths are checked
next to core's exact pass.
"""

from __future__ import annotations

import base64
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
    entry = dr.build_entry(password())
    expect(len(base64.b64decode(entry["key"])) == 32, "32-byte key")
    expect(256 <= entry["base"] < 2**61 - 1, "base range")
    expect(len(entry["forms"]) == 5, f"one form per FORMS item: {len(entry['forms'])}")
    for form in entry["forms"]:
        expect(set(form) == {"len", "fp", "mac"}, f"form keys {set(form)}")
        expect(len(form["mac"]) == 64 and int(form["mac"], 16) >= 0, "hex mac")
    # Both newline forms differ from the raw ones, so ten forms; identical ones are dropped.
    expect(len(dr.build_entry("a+\nb")["forms"]) == 10, "CR/LF-stripped forms added")
    expect(len(dr.build_entry("a+a")["forms"]) == 4, "raw and reversed of a palindrome de-duplicated")


def test_each_form_scrubbed() -> None:
    value = password()
    entry = dr.build_entry(value)
    for size in (1024, 200 * 1024):
        check_scrubbed(value, entry, size, "generated password")


def test_no_false_positives() -> None:
    text = filler(200 * 1024)
    entries = [dr.build_entry(password()) for _ in range(10)]
    expect(dr.scrub(text, entries, MARKER) == text, "random text unchanged")


def test_unicode_and_newline_values() -> None:
    for value in ("pässwörd+/%x", "line1\nline2"):
        entry = dr.build_entry(value)
        check_scrubbed(value, entry, 1024, repr(value))
        stripped = value.replace("\r", "").replace("\n", "")
        if stripped != value:
            check_scrubbed(stripped, entry, 1024, f"{value!r} stripped")
    # Every occurrence goes, and the marker sits where each one was.
    value = "pässwörd+/%x"
    text = f"a{value}b{base64.b64encode(value.encode()).decode()}c{value}"
    expect(dr.scrub(text, [dr.build_entry(value)], MARKER) == f"a{MARKER}b{MARKER}c{MARKER}", "repeats")


def test_malformed_entries_ignored() -> None:
    value = password()
    good = dr.build_entry(value)
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


def test_timing() -> None:
    text = filler(200 * 1024)
    entries = [dr.build_entry(password()) for _ in range(10)]
    start = time.perf_counter()
    dr.scrub(text, entries, MARKER)
    ms = (time.perf_counter() - start) * 1000
    print(f"timing: 200 kB, 10 entries: {ms:.0f} ms")


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
redact.register_vault_redaction_digest(build_entry(value))
redact.register_vault_redaction_digest({"key": "junk"})  # dropped, no exception
redact.register_vault_redaction_digest(None)
assert redact.redact_registered_vault_values(text) == "field=«redacted-vault-secret»;", "base64 scrubbed"
assert redact.redact_sensitive_text(text) == "field=«redacted-vault-secret»;", "through redact_sensitive_text"
redact.clear_vault_redaction_values()
assert redact.redact_registered_vault_values(text) == text, "clear drops digests"

redact.register_vault_redaction_value("Plain-Fill-Value-42")
assert redact.redact_registered_vault_values("x Plain-Fill-Value-42 y") == "x «redacted-vault-secret» y", "exact pass"
redact.clear_vault_redaction_values()

for _ in range(redact._VAULT_REDACTION_MAX_PER_PROFILE + 5):
    redact.register_vault_redaction_digest(build_entry(os.urandom(12).hex()))
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
