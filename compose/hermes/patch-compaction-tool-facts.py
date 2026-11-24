#!/usr/bin/env python3
"""Feed the compaction summarizer full tool results, not prune stubs.

Compatibility overlay (not upstream). Copies compaction_tool_facts.py into
the agent package and appends one install call to context_compressor.py.
See compaction_tool_facts.py for what the install wraps and why.
"""
from __future__ import annotations

from overlay_patch import BOOTSTRAP, ROOT, edit, fail, install

AGENT = ROOT / "agent"
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
    install(BOOTSTRAP / "compaction_tool_facts.py", AGENT / "compaction_tool_facts.py", TAG)
    compressor = AGENT / "context_compressor.py"
    # The wrapped methods may live in context_compressor_*.py split modules.
    sources = "".join(p.read_text(encoding="utf-8") for p in AGENT.glob("context_compressor_*.py"))
    if compressor.is_file():
        sources += compressor.read_text(encoding="utf-8")
    for needle in REQUIRED:
        if needle not in sources:
            fail(TAG, f"anchor missing in {AGENT}/context_compressor*.py: expected {needle!r}, found 0")
    edit(compressor, [], INSTALL_TAG, TAG, label="context_compressor.py", append=INSTALL,
         requires=["class ContextCompressor"])


if __name__ == "__main__":
    main()
