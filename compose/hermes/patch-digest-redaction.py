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
from pathlib import Path

from overlay_patch import ROOT, FileEdit, edit_files, install

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
    "def register_vault_redaction_digest(entry) -> bool:\n"
    "    \"\"\"Register a broker fill's digest entry; False when it was dropped.\n"
    "\n"
    "    Malformed entries, and entries built with a base other than the profile's\n"
    "    current one, are dropped: the shared pass could never match them.\n"
    "    \"\"\"\n"
    "    parsed = _digest_redaction.normalize_entry(entry)\n"
    "    if parsed is None:\n"
    "        return False\n"
    "    with _VAULT_REDACTION_LOCK:\n"
    "        scope = _vault_scope()\n"
    "        if parsed[1] != _VAULT_REDACTION_BASES.get(scope):\n"
    "            return False\n"
    "        bucket = _VAULT_REDACTION_DIGESTS.setdefault(scope, {})\n"
    "        bucket.pop(parsed[0], None)  # re-registering refreshes recency\n"
    "        bucket[parsed[0]] = parsed\n"
    "        while len(bucket) > _VAULT_REDACTION_MAX_PER_PROFILE:\n"
    "            del bucket[next(iter(bucket))]\n"
    "        _VAULT_REDACTION_TABLES[scope] = _digest_redaction.build_table(bucket.values())\n"
    "    return True\n"
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

# Names the inserted code reads from the module that holds the registry.
NEEDED = (
    "def redact_registered_vault_values(text: str) -> str:\n",
    "def clear_vault_redaction_values() -> None:\n",
    "def _vault_scope() -> str:\n",
    "_VAULT_REDACTION_LOCK = threading.Lock()\n",
    "_VAULT_REDACTION_MAX_PER_PROFILE = ",
    REDACTION_MARKER,
)

# Since v0.23.2 the registry lives in agent/redact_vault_values.py and
# agent/redact.py re-exports it. Callers (patch-bitwarden-broker-fill.py)
# import the two new names from agent.redact, so they are re-exported too.
VAULT_MODULE = "redact_vault_values.py"
REEXPORT_ANCHOR = (
    "from agent.redact_vault_values import (  # re-exported: callers import these from agent.redact\n"
)
REEXPORT = (
    "from agent.redact_vault_values import register_vault_redaction_digest, vault_redaction_base"
    f"  {MARKER}\n"
)


def main() -> None:
    install(BOOTSTRAP / "digest_redaction.py", AGENT / "digest_redaction.py", TAG)
    redact = AGENT / "redact.py"
    split = (AGENT / VAULT_MODULE).is_file()
    files = [FileEdit(AGENT / VAULT_MODULE if split else redact,
                      [(CLEAR_ANCHOR, CLEAR), (SCRUB_ANCHOR, SCRUB), (REGISTRY_ANCHOR, REGISTRY + REGISTRY_ANCHOR)],
                      requires=NEEDED)]
    if split:
        files.append(FileEdit(redact, [(REEXPORT_ANCHOR, REEXPORT + REEXPORT_ANCHOR)]))
    # Every anchor in both files is checked before either is written.
    edit_files(files, MARKER, TAG)


if __name__ == "__main__":
    main()
