#!/usr/bin/env python3
"""Scrub broker-filled values from model output by digest.

Compatibility overlay (not upstream). The Bitwarden broker fills passwords
itself, so the gateway never holds the value core's exact pass needs. The
broker returns a digest entry instead (see digest_redaction.py, copied here
into the agent package). This adds agent.redact.register_vault_redaction_digest,
which stores normalized entries per _vault_scope() under core's lock and
per-profile cap, and agent.redact.vault_redaction_base, the per-profile
fingerprint base the broker builds entries with.
redact_registered_vault_values runs the digest scrub after core's exact pass,
and clear_vault_redaction_values drops digests and the base with values.

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
    "_VAULT_REDACTION_BASES: dict = {}  # profile home → Rabin-Karp base the broker builds entries with\n"
    "_VAULT_REDACTION_DIGESTS: dict = {}  # profile home → ordered {entry key: normalized entry}\n"
    "_VAULT_REDACTION_TABLES: dict = {}  # profile home → lookup table built from its digests\n"
    "\n"
    "\n"
    "def vault_redaction_base() -> int:\n"
    "    \"\"\"The current profile's fingerprint base, created on first use.\"\"\"\n"
    "    with _VAULT_REDACTION_LOCK:\n"
    "        return _VAULT_REDACTION_BASES.setdefault(_vault_scope(), _digest_redaction.new_base())\n"
    "\n"
    "\n"
    "def register_vault_redaction_digest(entry) -> None:\n"
    "    \"\"\"Register a broker fill's digest entry.\n"
    "\n"
    "    Malformed entries, and entries built with a base other than the profile's\n"
    "    current one, are dropped: the shared pass could never match them.\n"
    "    \"\"\"\n"
    "    parsed = _digest_redaction.normalize_entry(entry)\n"
    "    if parsed is None:\n"
    "        return\n"
    "    with _VAULT_REDACTION_LOCK:\n"
    "        scope = _vault_scope()\n"
    "        if parsed[1] != _VAULT_REDACTION_BASES.get(scope):\n"
    "            return\n"
    "        bucket = _VAULT_REDACTION_DIGESTS.setdefault(scope, {})\n"
    "        bucket.pop(parsed[0], None)  # re-registering refreshes recency\n"
    "        bucket[parsed[0]] = parsed\n"
    "        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:\n"
    "            del bucket[next(iter(bucket))]\n"
    "        _VAULT_REDACTION_TABLES[scope] = _digest_redaction.build_table(bucket.values())\n"
    "\n"
    "\n"
)

CLEAR_ANCHOR = (
    "    with _VAULT_REDACTION_LOCK:\n"
    "        _VAULT_REDACTION_VALUES.pop(_vault_scope(), None)\n"
)
CLEAR = CLEAR_ANCHOR + (
    "        _VAULT_REDACTION_DIGESTS.pop(_vault_scope(), None)\n"
    "        _VAULT_REDACTION_TABLES.pop(_vault_scope(), None)\n"
    "        _VAULT_REDACTION_BASES.pop(_vault_scope(), None)\n"
)

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
    "        scope = _vault_scope()\n"
    "        table = _VAULT_REDACTION_TABLES.get(scope)\n"
    "        base = _VAULT_REDACTION_BASES.get(scope)\n"
    "    if table and base is not None:\n"
    f"        text = _digest_redaction.scrub_table(text, base, table, \"{REDACTION_MARKER}\")\n"
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
