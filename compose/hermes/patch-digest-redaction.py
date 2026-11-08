#!/usr/bin/env python3
"""Scrub broker-filled values from model output by digest.

Compatibility overlay (not upstream). The Bitwarden broker fills passwords
itself, so the gateway never holds the value core's exact pass needs. The
broker returns a digest entry instead (see digest_redaction.py, copied here
into the agent package). This adds agent.redact.register_vault_redaction_digest,
which stores entries per _vault_scope() under core's lock and per-profile cap;
redact_registered_vault_values runs the digest scrub after core's exact pass,
and clear_vault_redaction_values drops digests with values.

Run scripts/test-digest-redaction.py on Hermes bumps.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
BOOTSTRAP = Path(os.environ.get("DIGEST_REDACTION_BOOTSTRAP", "/bootstrap"))
TAG = "[patch-digest-redaction]"
MARKER = "# assistant-stack: digest redaction"
REDACTION_MARKER = "«redacted-vault-secret»"

REGISTRY_ANCHOR = "def clear_vault_redaction_values() -> None:\n"
REGISTRY = (
    f"{MARKER}\n"
    "from agent import digest_redaction as _digest_redaction\n"
    "\n"
    "_VAULT_REDACTION_DIGESTS: dict = {}  # profile home → ordered {entry key: normalized entry}\n"
    "\n"
    "\n"
    "def register_vault_redaction_digest(entry) -> None:\n"
    "    \"\"\"Register a broker fill's digest entry; malformed entries are dropped.\"\"\"\n"
    "    if _digest_redaction.normalize_entry(entry) is None:\n"
    "        return\n"
    "    with _VAULT_REDACTION_LOCK:\n"
    "        bucket = _VAULT_REDACTION_DIGESTS.setdefault(_vault_scope(), {})\n"
    "        bucket.pop(entry[\"key\"], None)  # re-registering refreshes recency\n"
    "        bucket[entry[\"key\"]] = entry\n"
    "        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:\n"
    "            del bucket[next(iter(bucket))]\n"
    "\n"
    "\n"
)

CLEAR_ANCHOR = (
    "    with _VAULT_REDACTION_LOCK:\n"
    "        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)\n"
)
CLEAR = CLEAR_ANCHOR + "        _VAULT_REDACTION_DIGESTS.pop(_vault_scope(), None)\n"

SCRUB_ANCHOR = (
    "    for value in values:\n"
    "        if value in text:\n"
    f"            text = text.replace(value, \"{REDACTION_MARKER}\")\n"
    "    return text\n"
)
SCRUB = (
    "    for value in values:\n"
    "        if value in text:\n"
    f"            text = text.replace(value, \"{REDACTION_MARKER}\")\n"
    "    with _VAULT_REDACTION_LOCK:\n"
    "        digests = list(_VAULT_REDACTION_DIGESTS.get(_vault_scope(), {}).values())\n"
    "    if digests:\n"
    f"        text = _digest_redaction.scrub(text, digests, \"{REDACTION_MARKER}\")\n"
    "    return text\n"
)

# Names the inserted code reads from agent/redact.py.
NEEDED = (
    "def redact_registered_vault_values(text: str) -> str:\n",
    "def clear_vault_redaction_values() -> None:\n",
    "def _vault_scope() -> str:\n",
    "_VAULT_REDACTION_LOCK = threading.Lock()\n",
    "_VAULT_REDACTION_MAX_PER_PROFILE = ",
    REDACTION_MARKER,
)


def main() -> None:
    src = BOOTSTRAP / "digest_redaction.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, AGENT / "digest_redaction.py")
    print(f"{TAG} installed {AGENT / 'digest_redaction.py'}")

    target = AGENT / "redact.py"
    text = target.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TAG} already applied")
        return
    missing = [n.strip() for n in NEEDED if n not in text]
    anchors = {"registry": REGISTRY_ANCHOR, "clear": CLEAR_ANCHOR, "scrub": SCRUB_ANCHOR}
    bad = [name for name, anchor in anchors.items() if text.count(anchor) != 1]
    if missing or bad:
        raise SystemExit(
            f"{TAG} expected code missing in {target}: {missing + bad}. "
            "Hermes image may have changed — update this patch."
        )
    text = text.replace(CLEAR_ANCHOR, CLEAR, 1)
    text = text.replace(SCRUB_ANCHOR, SCRUB, 1)
    text = text.replace(REGISTRY_ANCHOR, REGISTRY + REGISTRY_ANCHOR, 1)
    target.write_text(text, encoding="utf-8")
    print(f"{TAG} patched {target}")


if __name__ == "__main__":
    main()
