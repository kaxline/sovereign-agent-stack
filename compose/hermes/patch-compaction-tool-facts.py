#!/usr/bin/env python3
"""Feed the compaction summarizer full tool results, not prune stubs.

Compatibility overlay (not upstream). Copies compaction_tool_facts.py into
the agent package and appends one install call to context_compressor.py.
See compaction_tool_facts.py for what the install wraps and why.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
AGENT = ROOT / "agent"
BOOTSTRAP = Path(os.environ.get("COMPACTION_TOOL_FACTS_BOOTSTRAP", "/bootstrap"))
TAG = "[patch-compaction-tool-facts]"

INSTALL_TAG = "# assistant-stack: compaction tool facts"
INSTALL = f'''

{INSTALL_TAG}
try:
    from agent.compaction_tool_facts import install as _tool_facts_install
    _tool_facts_install(ContextCompressor)
except Exception:
    logger.exception("compaction tool facts overlay failed to install")
'''

# Methods the overlay wraps; a rename upstream must fail the patch loudly.
REQUIRED = (
    "def _plan_compaction_window(",
    "def _serialize_records_for_summary(",
    "def _summary_template_sections(",
    "def _is_summary_stub(",
    "_PRUNED_TOOL_PLACEHOLDER =",
    "## Active State",
)


def main() -> None:
    src = BOOTSTRAP / "compaction_tool_facts.py"
    if not src.is_file():
        raise SystemExit(f"{TAG} missing {src}")
    shutil.copyfile(src, AGENT / "compaction_tool_facts.py")
    print(f"{TAG} installed {AGENT / 'compaction_tool_facts.py'}")

    compressor = AGENT / "context_compressor.py"
    text = compressor.read_text()
    sources = text + "".join(p.read_text() for p in AGENT.glob("context_compressor_*.py"))
    missing = [needle for needle in REQUIRED if needle not in sources]
    if missing or "class ContextCompressor" not in text:
        raise SystemExit(
            f"{TAG} expected code missing: {missing or ['class ContextCompressor']}. "
            "Hermes image may have changed — update this patch."
        )
    if INSTALL_TAG in text:
        print(f"{TAG} already applied")
        return
    compressor.write_text(text.rstrip("\n") + "\n" + INSTALL)
    print(f"{TAG} patched context_compressor.py")


if __name__ == "__main__":
    main()
